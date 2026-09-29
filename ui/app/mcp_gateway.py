from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .preference_policy import exclusive_content_conflict, exclusive_content_policy


@dataclass(frozen=True)
class MCPCall:
    tool: str
    arguments: dict[str, Any]
    result: Any
    duration_ms: float


class MCPGateway:
    """Warm Couchbase MCP Server client plus StreamAI business-tool adapter.

    The MCP server exposes database primitives. Agent Catalog contains the
    versioned business-tool definitions. This class maps those governed tools to
    MCP primitives while keeping one Streamable HTTP session alive for the whole
    application process. That avoids a new HTTP/MCP handshake per recommendation.
    """

    _ARGUMENT_ALIASES: dict[str, tuple[str, ...]] = {
        "bucket_name": ("bucket_name", "bucket", "bucketName"),
        "scope_name": ("scope_name", "scope", "scopeName"),
        "collection_name": ("collection_name", "collection", "collectionName"),
        "document_id": ("document_id", "id", "documentId", "document_key", "documentKey", "key"),
        "query": ("query", "query_string", "queryString", "statement", "sqlpp_query", "sqlppQuery", "sqlpp", "sql"),
        "named_parameters": (
            "named_parameters",
            "query_parameters",
            "parameters",
            "namedParameters",
            "queryParameters",
        ),
    }

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.enabled = bool(settings.mcp_enabled)
        self.required = bool(settings.mcp_required)
        self.url = str(settings.mcp_url)
        self.timeout_seconds = float(settings.mcp_timeout_seconds)
        self._stack: AsyncExitStack | None = None
        self._session: Any = None
        self._lock = asyncio.Lock()
        self._tools: list[dict[str, Any]] = []
        self._tool_map: dict[str, dict[str, Any]] = {}
        self._started_at: float | None = None
        self._last_error: str | None = None
        self._server_info: dict[str, Any] = {}
        self._business_call_count = 0
        self._primitive_call_count = 0

    async def start(self) -> None:
        if not self.enabled:
            return
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except Exception as exc:  # pragma: no cover - exercised in deployment
            self._last_error = f"MCP SDK unavailable: {type(exc).__name__}: {exc}"
            if self.required:
                raise RuntimeError(self._last_error) from exc
            return

        stack = AsyncExitStack()
        try:
            kwargs: dict[str, Any] = {}
            bearer = str(getattr(self.settings, "mcp_bearer_token", "") or "").strip()
            if bearer:
                kwargs["headers"] = {"Authorization": f"Bearer {bearer}"}
            transport = await stack.enter_async_context(
                streamable_http_client(self.url, **kwargs)
            )
            read_stream, write_stream = transport[0], transport[1]
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            init = await asyncio.wait_for(session.initialize(), timeout=self.timeout_seconds)
            self._stack = stack
            self._session = session
            self._started_at = time.time()
            self._last_error = None
            self._server_info = self._serialise(
                getattr(init, "serverInfo", None)
                or getattr(init, "server_info", None)
                or {}
            )
            await self.refresh_tools()
            self._assert_required_primitives()
        except Exception as exc:
            await stack.aclose()
            self._session = None
            self._last_error = f"{type(exc).__name__}: {exc}"
            if self.required:
                raise RuntimeError(
                    f"Couchbase MCP Server is required but unavailable at {self.url}: {self._last_error}"
                ) from exc

    async def close(self) -> None:
        stack, self._stack = self._stack, None
        self._session = None
        if stack is not None:
            await stack.aclose()

    @property
    def healthy(self) -> bool:
        return self.enabled and self._session is not None and self._last_error is None

    async def refresh_tools(self) -> list[dict[str, Any]]:
        if self._session is None:
            return []
        async with self._lock:
            result = await asyncio.wait_for(self._session.list_tools(), timeout=self.timeout_seconds)
        tools: list[dict[str, Any]] = []
        for tool in list(getattr(result, "tools", None) or []):
            item = {
                "name": str(getattr(tool, "name", "")),
                "description": str(getattr(tool, "description", "") or ""),
                "inputSchema": self._serialise(
                    getattr(tool, "inputSchema", None)
                    or getattr(tool, "input_schema", None)
                    or {}
                ),
            }
            tools.append(item)
        self._tools = tools
        self._tool_map = {item["name"]: item for item in tools if item.get("name")}
        return list(tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPCall:
        if self._session is None:
            raise RuntimeError("MCP session is not connected")
        if name not in self._tool_map:
            raise RuntimeError(f"Couchbase MCP Server does not expose required tool {name!r}")
        adapted = self._adapt_arguments(name, arguments)
        started = time.perf_counter()
        async with self._lock:
            response = await asyncio.wait_for(
                self._session.call_tool(name, arguments=adapted),
                timeout=self.timeout_seconds,
            )
        if bool(getattr(response, "isError", False) or getattr(response, "is_error", False)):
            detail = self.normalise_result(response)
            raise RuntimeError(f"MCP tool {name} failed: {detail}")
        self._primitive_call_count += 1
        return MCPCall(
            tool=name,
            arguments=adapted,
            result=self.normalise_result(response),
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )

    async def call_business_tool(self, name: str, inputs: dict[str, Any]) -> Any:
        """Execute an Agent Catalog business tool through MCP primitives."""
        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
            "get_viewer_context": self._business_get_viewer_context,
            "get_watch_history": self._business_get_watch_history,
            "get_catalogue_statistics": self._business_get_catalogue_statistics,
            "query_catalogue_analytics": self._business_query_catalogue_analytics,
            "resolve_catalogue_title": self._business_resolve_catalogue_title,
            "check_entitlement": self._business_check_entitlement,
            "inspect_streamai_schema": self._business_inspect_schema,
        }
        handler = handlers.get(name)
        if handler is None:
            raise KeyError(f"No MCP business-tool adapter registered for {name!r}")
        started = time.perf_counter()
        result = await handler(dict(inputs))
        self._business_call_count += 1
        if isinstance(result, dict):
            result.setdefault("_nativeExecution", {})
            if isinstance(result["_nativeExecution"], dict):
                result["_nativeExecution"].update(
                    {
                        "businessTool": name,
                        "governance": "Couchbase Agent Catalog",
                        "transport": "Couchbase MCP Server / Streamable HTTP",
                        "durationMs": round((time.perf_counter() - started) * 1000, 1),
                    }
                )
        return result

    async def _business_get_viewer_context(self, inputs: dict[str, Any]) -> dict[str, Any]:
        viewer_id = self._required_text(inputs, "viewer_id")
        call = await self._get_document(
            self.settings.viewers_scope,
            self.settings.profiles_collection,
            f"profile::{viewer_id}",
        )
        document = self._document_payload(call.result)
        document["_nativeExecution"] = self._primitive_trace([call])
        return document

    async def _business_get_watch_history(self, inputs: dict[str, Any]) -> list[dict[str, Any]]:
        viewer_id = self._required_text(inputs, "viewer_id")
        content_type = inputs.get("content_type")
        limit = min(max(int(inputs.get("limit") or 30), 1), 100)

        # MCP SQL++ queries are scoped by bucket and scope. Query the viewer
        # history collection first, then hydrate the matching catalogue titles
        # with one second scoped query. This avoids cross-scope qualification
        # and keeps the native path at two MCP calls rather than N KV reads.
        history_query = f"""
        SELECT h.titleId, h.progressPct, h.status AS watchStatus,
               h.lastWatchedAt, h.device AS watchDevice
        FROM `{self._identifier(self.settings.watch_history_collection)}` AS h
        WHERE h.viewerId=$viewerId AND h.progressPct > 0
        ORDER BY h.lastWatchedAt DESC
        LIMIT $candidateLimit
        """
        history_call = await self._run_query(
            history_query,
            {"viewerId": viewer_id, "candidateLimit": min(limit * 3, 300)},
            scope_name=self.settings.viewers_scope,
        )
        history_rows = self._rows(history_call.result)
        title_ids = [str(row.get("titleId") or "") for row in history_rows if row.get("titleId")]
        if not title_ids:
            return []

        title_query = f"""
        SELECT META(t).id AS id, t.*
        FROM `{self._identifier(self.settings.titles_collection)}` AS t
        WHERE META(t).id IN $titleIds
          AND ($contentType IS NULL OR t.contentType=$contentType)
        """
        title_call = await self._run_query(
            title_query,
            {"titleIds": title_ids, "contentType": content_type},
            scope_name=self.settings.catalogue_scope,
        )
        titles_by_id = {
            str(item.get("id") or ""): self._decorate_title(item)
            for item in self._rows(title_call.result)
            if item.get("id")
        }
        results: list[dict[str, Any]] = []
        for history in history_rows:
            title = titles_by_id.get(str(history.get("titleId") or ""))
            if not title:
                continue
            item = dict(title)
            item.update(
                {
                    "progressPct": history.get("progressPct"),
                    "watchStatus": history.get("watchStatus"),
                    "lastWatchedAt": history.get("lastWatchedAt"),
                    "watchDevice": history.get("watchDevice"),
                    "recommendationSource": "watch_history",
                    "viewerState": "watched",
                }
            )
            results.append(item)
            if len(results) >= limit:
                break
        return results

    async def _business_get_catalogue_statistics(
        self, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        content_type = str(inputs.get("content_type") or "").strip() or None
        genre = str(inputs.get("genre") or "").strip() or None
        query = f"""
        SELECT COUNT(1) AS count
        FROM `{self._identifier(self.settings.titles_collection)}` AS t
        WHERE ($contentType IS NULL OR t.contentType=$contentType)
          AND ($genre IS NULL OR ANY g IN t.genres SATISFIES LOWER(g)=LOWER($genre) END)
        """
        call = await self._run_query(
            query,
            {"contentType": content_type, "genre": genre},
            scope_name=self.settings.catalogue_scope,
        )
        rows = self._rows(call.result)
        count = int((rows[0] if rows else {}).get("count") or 0)
        return {
            "count": count,
            "contentType": content_type,
            "genre": genre,
            "queryMode": "agent_catalog_mcp_sqlpp_aggregate",
            "_nativeExecution": self._primitive_trace([call]),
        }

    async def _business_query_catalogue_analytics(
        self, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        dimension = str(inputs.get("dimension") or "")
        measure = str(inputs.get("measure") or "")
        dimensions = {
            "genre": ("genre", " UNNEST t.genres AS genre"),
            "release_year": ("t.releaseYear", ""),
            "original_language": ("t.originalLanguage", ""),
            "country": ("country", " UNNEST t.originCountries AS country"),
            "content_type": ("t.contentType", ""),
        }
        measures = {
            "count": "COUNT(1)",
            "average_rating": "AVG(t.voteAverage)",
            "average_runtime": "AVG(COALESCE(t.runtimeMinutes, t.episodeRuntimeMinutes))",
        }
        if dimension not in dimensions or measure not in measures:
            raise ValueError("Unsupported catalogue analytics schema")
        filters = dict(inputs.get("filters") or {})
        content_type = filters.get("contentType")
        genre = filters.get("genre")
        release_year = filters.get("releaseYear")
        field, unnest = dimensions[dimension]
        conditions = [f"{field} IS VALUED"]
        parameters: dict[str, Any] = {
            "contentType": content_type,
            "genre": genre,
            "releaseYear": int(release_year) if release_year is not None else None,
            "limit": min(max(int(inputs.get("limit") or 30), 1), 50),
        }
        conditions.extend(
            [
                "($contentType IS NULL OR t.contentType=$contentType)",
                "($genre IS NULL OR ANY g IN t.genres SATISFIES LOWER(g)=LOWER($genre) END)",
                "($releaseYear IS NULL OR t.releaseYear=$releaseYear)",
            ]
        )
        if measure == "average_rating":
            conditions.append("t.voteAverage IS VALUED")
        elif measure == "average_runtime":
            conditions.append(
                "(t.runtimeMinutes IS VALUED OR t.episodeRuntimeMinutes IS VALUED)"
            )
        direction = "ASC" if str(inputs.get("order") or "").lower() == "asc" else "DESC"
        query = (
            f"SELECT {field} AS label, {measures[measure]} AS metricValue "
            f"FROM `{self._identifier(self.settings.titles_collection)}` AS t{unnest} "
            f"WHERE {' AND '.join(conditions)} GROUP BY {field} "
            f"ORDER BY metricValue {direction}, label ASC LIMIT $limit"
        )
        call = await self._run_query(
            query, parameters, scope_name=self.settings.catalogue_scope
        )
        rows = [
            {"label": row.get("label"), "value": row.get("metricValue")}
            for row in self._rows(call.result)
        ]
        return {
            "measure": measure,
            "dimension": dimension,
            "filters": filters,
            "rows": rows,
            "queryMode": "agent_catalog_mcp_validated_analytics",
            "readOnly": True,
            "_nativeExecution": self._primitive_trace([call]),
        }

    async def _business_resolve_catalogue_title(self, inputs: dict[str, Any]) -> dict[str, Any]:
        title = self._required_text(inputs, "title")
        content_type = inputs.get("content_type")
        collection = self._identifier(self.settings.titles_collection)
        query = f"""
        SELECT META(t).id AS id, t.*
        FROM `{collection}` AS t
        WHERE (LOWER(t.title)=LOWER($title) OR LOWER(t.originalTitle)=LOWER($title))
          AND ($contentType IS NULL OR t.contentType=$contentType)
        ORDER BY CASE WHEN LOWER(t.title)=LOWER($title) THEN 0 ELSE 1 END,
                 t.popularity DESC
        LIMIT 5
        """
        call = await self._run_query(
            query,
            {"title": title, "contentType": content_type},
            scope_name=self.settings.catalogue_scope,
        )
        rows = [self._decorate_title(item) for item in self._rows(call.result)]
        return {
            "match": rows[0] if rows else None,
            "alternatives": rows[1:5],
            "trace": {
                "mode": "agent_catalog_mcp_exact_title",
                "transport": "Couchbase MCP Server",
                "returnedCount": len(rows),
                "mcpPrimitives": self._primitive_trace([call]),
            },
        }

    async def _business_check_entitlement(self, inputs: dict[str, Any]) -> dict[str, Any]:
        viewer_id = self._required_text(inputs, "viewer_id")
        title_id = self._required_text(inputs, "title_id")
        # These are independent KV reads. They are intentionally kept as MCP
        # primitives so the inspector can prove the database boundary.
        title_call = await self._get_document(
            self.settings.catalogue_scope, self.settings.titles_collection, title_id
        )
        profile_call = await self._get_document(
            self.settings.viewers_scope,
            self.settings.profiles_collection,
            f"profile::{viewer_id}",
        )
        entitlement_call = await self._get_document(
            self.settings.operations_scope,
            self.settings.entitlements_collection,
            f"entitlement::{viewer_id}",
        )
        title = self._document_payload(title_call.result)
        profile = self._document_payload(profile_call.result)
        entitlement = self._document_payload(entitlement_call.result)
        availability = dict(title.get("availability") or {})
        regions = [str(value).upper() for value in availability.get("regions", ["GB", "IE"])]
        region = str(entitlement.get("regionCode") or "GB").upper()
        viewer_tier = str(entitlement.get("subscriptionTier") or "standard").lower()
        required_tier = str(availability.get("minimumTier") or "standard").lower()
        offer_type = str(availability.get("offerType") or "included").lower()
        reasons: list[str] = []
        if not bool(availability.get("available", True)):
            reasons.append("title is not currently available")
        if regions and region not in regions:
            reasons.append(f"not licensed in region {region}")
        if offer_type == "included" and self._tier_rank(viewer_tier) < self._tier_rank(required_tier):
            reasons.append(f"requires the {required_tier} subscription tier")
        max_rating = self._rating_value(entitlement.get("maxParentalRating"))
        title_rating = self._rating_value(title.get("ageRating") or title.get("certification"))
        if title_rating > max_rating:
            reasons.append("blocked by the viewer's parental policy")
        if profile.get("avoidAdultContent") and self._contains_adult_signal(title):
            reasons.append("blocked by the viewer's adult-content preference")
        exclusive_conflict = exclusive_content_conflict(title, profile)
        if exclusive_conflict is not None:
            reasons.append(str(exclusive_conflict["reason"]))
        allowed = not reasons
        included = allowed and offer_type == "included"
        calls = [title_call, profile_call, entitlement_call]
        return {
            "allowed": allowed,
            "decision": "allow" if allowed else "deny",
            "reason": (
                "Included in the current plan"
                if included
                else ("Available as a separate purchase" if allowed else "; ".join(reasons))
            ),
            "includedInPlan": included,
            "offerType": offer_type,
            "region": region,
            "subscriptionTier": viewer_tier,
            "requiredTier": required_tier,
            "allowedRegions": regions,
            "parentalRating": entitlement.get("maxParentalRating"),
            "titleRating": title.get("ageRating") or title.get("certification"),
            "titleId": title_id,
            "title": title.get("title"),
            "preferencePolicy": (
                exclusive_conflict["policy"]
                if exclusive_conflict is not None
                else exclusive_content_policy(profile)
            ),
            "preferenceConflict": exclusive_conflict is not None,
            "preferenceConflictCode": (
                exclusive_conflict["code"] if exclusive_conflict is not None else None
            ),
            "transport": "Couchbase MCP Server",
            "_nativeExecution": self._primitive_trace(calls),
        }

    async def _business_inspect_schema(self, inputs: dict[str, Any]) -> dict[str, Any]:
        del inputs
        bucket = self.settings.content_bucket
        call = await self.call_tool(
            "get_scopes_and_collections_in_bucket", {"bucket_name": bucket}
        )
        return {
            "bucket": bucket,
            "schema": call.result,
            "count": self._count_schema_collections(call.result),
            "mcpPrimitive": call.tool,
            "_nativeExecution": self._primitive_trace([call]),
        }

    async def _get_document(self, scope: str, collection: str, document_id: str) -> MCPCall:
        return await self.call_tool(
            "get_document_by_id",
            {
                "bucket_name": self.settings.content_bucket,
                "scope_name": scope,
                "collection_name": collection,
                "document_id": document_id,
            },
        )

    async def _run_query(
        self, query: str, named_parameters: dict[str, Any], *, scope_name: str
    ) -> MCPCall:
        arguments: dict[str, Any] = {
            "bucket_name": self.settings.content_bucket,
            "scope_name": scope_name,
            "query": query,
        }
        schema = dict(
            self._tool_map.get("run_sql_plus_plus_query", {}).get("inputSchema") or {}
        )
        properties = dict(schema.get("properties") or {})
        accepts_named = not properties or self._find_argument_name(
            "named_parameters", properties
        ) is not None
        if accepts_named:
            arguments["named_parameters"] = named_parameters
        elif named_parameters:
            # Some MCP Server releases expose only query/bucket/scope. Inline
            # parameters as JSON-compatible SQL++ literals so the application
            # remains compatible without unsafe string concatenation.
            arguments["query"] = self._inline_named_parameters(query, named_parameters)
        return await self.call_tool("run_sql_plus_plus_query", arguments)

    @staticmethod
    def _inline_named_parameters(query: str, parameters: dict[str, Any]) -> str:
        rendered = str(query)
        for name in sorted(parameters, key=len, reverse=True):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name)):
                raise ValueError(f"Unsafe SQL++ named-parameter identifier: {name!r}")
            literal = json.dumps(parameters[name], ensure_ascii=False, separators=(",", ":"))
            rendered = re.sub(rf"\${re.escape(str(name))}\b", literal, rendered)
        unresolved = sorted(set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", rendered)))
        if unresolved:
            raise ValueError("Unresolved SQL++ named parameters: " + ", ".join(unresolved))
        return rendered

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "required": self.required,
            "healthy": self.healthy,
            "package": "couchbase-mcp-server",
            "transport": "streamable_http",
            "url": self.url,
            "readOnly": bool(self.settings.mcp_read_only),
            "server": self._server_info,
            "toolCount": len(self._tools),
            "tools": self._tools,
            "requiredPrimitives": [
                "get_document_by_id",
                "run_sql_plus_plus_query",
                "get_scopes_and_collections_in_bucket",
            ],
            "businessCalls": self._business_call_count,
            "primitiveCalls": self._primitive_call_count,
            "connectedAt": self._started_at,
            "error": self._last_error,
        }

    def _assert_required_primitives(self) -> None:
        required = {
            "get_document_by_id",
            "run_sql_plus_plus_query",
            "get_scopes_and_collections_in_bucket",
        }
        missing = sorted(required - set(self._tool_map))
        if missing:
            raise RuntimeError("MCP Server is missing required tools: " + ", ".join(missing))

    def _adapt_arguments(self, tool_name: str, desired: dict[str, Any]) -> dict[str, Any]:
        schema = dict(self._tool_map.get(tool_name, {}).get("inputSchema") or {})
        properties = dict(schema.get("properties") or {})
        if not properties:
            return dict(desired)
        adapted: dict[str, Any] = {}
        for logical_name, value in desired.items():
            actual = self._find_argument_name(logical_name, properties)
            if actual is None:
                continue
            spec = properties.get(actual) or {}
            if logical_name == "named_parameters" and isinstance(value, dict):
                declared_type = str(spec.get("type") or "").lower()
                value = json.dumps(value) if declared_type == "string" else value
            adapted[actual] = value
        missing_required = [name for name in list(schema.get("required") or []) if name not in adapted]
        if missing_required:
            raise RuntimeError(
                f"Cannot map arguments for MCP tool {tool_name}: missing {missing_required}; schema={schema}"
            )
        return adapted

    def _find_argument_name(self, logical_name: str, properties: dict[str, Any]) -> str | None:
        for candidate in self._ARGUMENT_ALIASES.get(logical_name, (logical_name,)):
            if candidate in properties:
                return candidate
        return logical_name if logical_name in properties else None

    @classmethod
    def normalise_result(cls, response: Any) -> Any:
        structured = getattr(response, "structuredContent", None) or getattr(
            response, "structured_content", None
        )
        if structured is not None:
            return cls._unwrap(cls._serialise(structured))
        content = list(getattr(response, "content", None) or [])
        values: list[Any] = []
        for item in content:
            text = getattr(item, "text", None)
            if text is not None:
                values.append(cls._parse_text(str(text)))
            else:
                values.append(cls._serialise(item))
        if len(values) == 1:
            return cls._unwrap(values[0])
        return cls._unwrap(values)

    @staticmethod
    def _parse_text(text: str) -> Any:
        value = text.strip()
        if not value:
            return ""
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    @classmethod
    def _unwrap(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for key in ("structuredContent", "structured_content", "document"):
                if key in value and len(value) <= 4:
                    return cls._unwrap(value[key])
            for key in ("results", "rows", "result", "content", "data"):
                if key in value and len(value) <= 4:
                    return cls._unwrap(value[key])
            return {key: cls._unwrap(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._unwrap(item) for item in value]
        return value

    @staticmethod
    def _serialise(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(key): MCPGateway._serialise(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [MCPGateway._serialise(item) for item in value]
        if hasattr(value, "model_dump"):
            return MCPGateway._serialise(value.model_dump())
        if hasattr(value, "dict"):
            return MCPGateway._serialise(value.dict())
        return str(value)


    @classmethod
    def _count_schema_collections(cls, value: Any) -> int:
        """Best-effort count of collections in the MCP schema payload."""
        if isinstance(value, list):
            return sum(cls._count_schema_collections(item) for item in value)
        if not isinstance(value, dict):
            return 0
        total = 0
        collections = value.get("collections")
        if isinstance(collections, list):
            total += len(collections)
        elif isinstance(collections, dict):
            total += len(collections)
        for key, item in value.items():
            if key == "collections":
                continue
            total += cls._count_schema_collections(item)
        return total

    @staticmethod
    def _document_payload(value: Any) -> dict[str, Any]:
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if isinstance(value, dict):
            for key in ("document", "value", "data"):
                if isinstance(value.get(key), dict):
                    return dict(value[key])
            return dict(value)
        raise RuntimeError(f"MCP returned an unexpected document payload: {type(value).__name__}")

    @staticmethod
    def _rows(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for key in ("results", "rows", "data"):
                rows = value.get(key)
                if isinstance(rows, list):
                    return [dict(item) for item in rows if isinstance(item, dict)]
            return [dict(value)]
        return []

    def _decorate_title(self, document: dict[str, Any]) -> dict[str, Any]:
        value = dict(document)
        poster = value.get("posterPath")
        backdrop = value.get("backdropPath")
        value["posterUrl"] = f"{self.settings.tmdb_image_base}{poster}" if poster else None
        value["backdropUrl"] = f"{self.settings.tmdb_backdrop_base}{backdrop}" if backdrop else None
        for field in ("embedding", "embeddingText", "searchText"):
            value.pop(field, None)
        return value

    @staticmethod
    def _primitive_trace(calls: list[MCPCall]) -> dict[str, Any]:
        return {
            "mcpPrimitives": [
                {
                    "tool": call.tool,
                    "durationMs": call.duration_ms,
                    "arguments": {
                        key: ("[redacted]" if key.lower() in {"password", "token"} else value)
                        for key, value in call.arguments.items()
                    },
                }
                for call in calls
            ]
        }

    @staticmethod
    def _required_text(inputs: dict[str, Any], key: str) -> str:
        value = str(inputs.get(key) or "").strip()
        if not value:
            raise ValueError(f"{key} is required")
        return value

    @staticmethod
    def _identifier(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", str(value)):
            raise ValueError(f"Unsafe Couchbase identifier: {value!r}")
        return str(value)

    @staticmethod
    def _rating_value(value: Any) -> int:
        match = re.search(r"\d+", str(value or ""))
        return int(match.group(0)) if match else 0

    @staticmethod
    def _tier_rank(value: Any) -> int:
        return {"free": 0, "basic": 1, "standard": 2, "premium": 3}.get(
            str(value or "standard").lower(), 2
        )

    @staticmethod
    def _contains_adult_signal(title: dict[str, Any]) -> bool:
        if bool(title.get("adult")):
            return True
        text = " ".join(
            [str(title.get("overview") or ""), *[str(item) for item in title.get("keywords", [])]]
        ).lower()
        return any(
            term in text
            for term in ("explicit sexual", "adult content", "erotic", "pornograph", "nudity")
        )
