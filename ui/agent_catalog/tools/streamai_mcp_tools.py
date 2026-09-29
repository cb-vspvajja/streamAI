from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import agentc


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_ARGUMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "bucket_name": ("bucket_name", "bucket", "bucketName"),
    "scope_name": ("scope_name", "scope", "scopeName"),
    "collection_name": ("collection_name", "collection", "collectionName"),
    "document_id": (
        "document_id",
        "id",
        "documentId",
        "document_key",
        "documentKey",
        "key",
    ),
    "query": (
        "query",
        "query_string",
        "queryString",
        "statement",
        "sqlpp_query",
        "sqlppQuery",
        "sqlpp",
        "sql",
    ),
    "named_parameters": (
        "named_parameters",
        "query_parameters",
        "parameters",
        "namedParameters",
        "queryParameters",
    ),
}


def _env(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    if name.endswith(("BUCKET", "SCOPE", "COLLECTION")) and not _IDENTIFIER.match(value):
        raise ValueError(f"Unsafe Couchbase identifier configured for {name}")
    return value


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(str(value)):
        raise ValueError(f"Unsafe Couchbase identifier: {value!r}")
    return str(value)


def _normalise(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        for key in ("structuredContent", "structured_content", "document"):
            if key in value and len(value) <= 4:
                return _normalise(value[key])
        for key in ("results", "rows", "result", "content", "data"):
            if key in value and len(value) <= 4:
                return _normalise(value[key])
        return {str(key): _normalise(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if hasattr(value, "model_dump"):
        return _normalise(value.model_dump())
    text = getattr(value, "text", None)
    if text is not None:
        try:
            return _normalise(json.loads(str(text)))
        except json.JSONDecodeError:
            return str(text)
    return str(value)


def _find_argument_name(logical_name: str, properties: dict[str, Any]) -> str | None:
    for candidate in _ARGUMENT_ALIASES.get(logical_name, (logical_name,)):
        if candidate in properties:
            return candidate
    return None


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


def _adapt_arguments(
    tool_name: str,
    desired: dict[str, Any],
    tool_schemas: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    schema = dict(tool_schemas.get(tool_name) or {})
    properties = dict(schema.get("properties") or {})
    if not properties:
        return dict(desired)

    values = dict(desired)
    if tool_name == "run_sql_plus_plus_query" and values.get("named_parameters"):
        named_actual = _find_argument_name("named_parameters", properties)
        if named_actual is None:
            values["query"] = _inline_named_parameters(
                str(values["query"]), dict(values.pop("named_parameters"))
            )

    adapted: dict[str, Any] = {}
    for logical_name, value in values.items():
        actual = _find_argument_name(logical_name, properties)
        if actual is None:
            continue
        spec = dict(properties.get(actual) or {})
        if logical_name == "named_parameters" and isinstance(value, dict):
            value = json.dumps(value) if str(spec.get("type") or "").lower() == "string" else value
        adapted[actual] = value
    missing = [name for name in list(schema.get("required") or []) if name not in adapted]
    if missing:
        raise RuntimeError(
            f"Cannot map arguments for MCP tool {tool_name}: missing {missing}; schema={schema}"
        )
    return adapted


async def _call_mcp_async(tool_name: str, arguments: dict[str, Any]) -> Any:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = os.getenv("MCP_URL", "http://couchbase-mcp-server:8000/mcp")
    timeout = float(os.getenv("MCP_TIMEOUT_SECONDS", "8"))
    headers: dict[str, str] = {}
    bearer = os.getenv("MCP_BEARER_TOKEN", "").strip()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    kwargs: dict[str, Any] = {"headers": headers} if headers else {}
    async with streamable_http_client(url, **kwargs) as transport:
        read_stream, write_stream = transport[0], transport[1]
        async with ClientSession(read_stream, write_stream) as session:
            await asyncio.wait_for(session.initialize(), timeout=timeout)
            discovered = await asyncio.wait_for(session.list_tools(), timeout=timeout)
            schemas = {
                str(getattr(tool, "name", "")): _normalise(
                    getattr(tool, "inputSchema", None)
                    or getattr(tool, "input_schema", None)
                    or {}
                )
                for tool in list(getattr(discovered, "tools", None) or [])
            }
            if tool_name not in schemas:
                raise RuntimeError(f"Couchbase MCP Server does not expose {tool_name!r}")
            adapted = _adapt_arguments(tool_name, arguments, schemas)
            response = await asyncio.wait_for(
                session.call_tool(tool_name, arguments=adapted), timeout=timeout
            )
    if bool(getattr(response, "isError", False) or getattr(response, "is_error", False)):
        raise RuntimeError(f"MCP tool {tool_name} failed: {_normalise(response)}")
    structured = getattr(response, "structuredContent", None) or getattr(
        response, "structured_content", None
    )
    if structured is not None:
        return _normalise(structured)
    content = list(getattr(response, "content", None) or [])
    values = [_normalise(item) for item in content]
    return _normalise(values[0] if len(values) == 1 else values)


def _call_mcp(tool_name: str, arguments: dict[str, Any]) -> Any:
    return asyncio.run(_call_mcp_async(tool_name, arguments))


def _decorate_title(doc: dict[str, Any]) -> dict[str, Any]:
    value = dict(doc)
    poster = value.get("posterPath")
    backdrop = value.get("backdropPath")
    value["posterUrl"] = (
        f"{os.getenv('TMDB_IMAGE_BASE', 'https://image.tmdb.org/t/p/w342').rstrip('/')}{poster}"
        if poster
        else None
    )
    value["backdropUrl"] = (
        f"{os.getenv('TMDB_BACKDROP_BASE', 'https://image.tmdb.org/t/p/w1280').rstrip('/')}{backdrop}"
        if backdrop
        else None
    )
    for field in ("embedding", "embeddingText", "searchText"):
        value.pop(field, None)
    return value


def _rows(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        rows = result.get("results") or result.get("rows") or result.get("data")
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
        return [result]
    return []


def _query_rows(
    query: str,
    named_parameters: dict[str, Any],
    *,
    scope_name: str,
) -> list[dict[str, Any]]:
    result = _call_mcp(
        "run_sql_plus_plus_query",
        {
            "bucket_name": _env("CONTENT_BUCKET", "streaming"),
            "scope_name": scope_name,
            "query": query,
            "named_parameters": named_parameters,
        },
    )
    return _rows(result)


def _document(scope: str, collection: str, document_id: str) -> dict[str, Any]:
    result = _call_mcp(
        "get_document_by_id",
        {
            "bucket_name": _env("CONTENT_BUCKET", "streaming"),
            "scope_name": scope,
            "collection_name": collection,
            "document_id": document_id,
        },
    )
    if isinstance(result, list) and len(result) == 1 and isinstance(result[0], dict):
        result = result[0]
    if isinstance(result, dict):
        for key in ("document", "value", "data"):
            if isinstance(result.get(key), dict):
                return dict(result[key])
        return dict(result)
    raise RuntimeError(f"MCP get_document_by_id returned an unexpected payload for {document_id}")


@agentc.catalog.tool
def get_viewer_context(viewer_id: str) -> dict[str, Any]:
    """Read the signed-in viewer profile through the governed MCP data boundary."""
    return _document(
        _env("VIEWERS_SCOPE", "viewers"),
        _env("PROFILES_COLLECTION", "profiles"),
        f"profile::{viewer_id}",
    )


@agentc.catalog.tool
def get_watch_history(
    viewer_id: str, content_type: str | None = None, limit: int = 30
) -> list[dict[str, Any]]:
    """Read watch history and hydrate catalogue details through scoped MCP SQL++."""
    safe_limit = min(max(int(limit), 1), 100)
    history = _query_rows(
        f"""
        SELECT h.titleId, h.progressPct, h.status AS watchStatus,
               h.lastWatchedAt, h.device AS watchDevice
        FROM `{_identifier(_env('WATCH_HISTORY_COLLECTION', 'watch_history'))}` AS h
        WHERE h.viewerId=$viewerId AND h.progressPct > 0
        ORDER BY h.lastWatchedAt DESC
        LIMIT $candidateLimit
        """,
        {"viewerId": viewer_id, "candidateLimit": min(safe_limit * 3, 300)},
        scope_name=_env("VIEWERS_SCOPE", "viewers"),
    )
    title_ids = [str(row.get("titleId") or "") for row in history if row.get("titleId")]
    if not title_ids:
        return []
    titles = _query_rows(
        f"""
        SELECT META(t).id AS id, t.*
        FROM `{_identifier(_env('TITLES_COLLECTION', 'titles'))}` AS t
        WHERE META(t).id IN $titleIds
          AND ($contentType IS NULL OR t.contentType=$contentType)
        """,
        {"titleIds": title_ids, "contentType": content_type},
        scope_name=_env("CATALOGUE_SCOPE", "catalogue"),
    )
    by_id = {
        str(item.get("id") or ""): _decorate_title(item)
        for item in titles
        if item.get("id")
    }
    output: list[dict[str, Any]] = []
    for row in history:
        title = by_id.get(str(row.get("titleId") or ""))
        if not title:
            continue
        item = dict(title)
        item.update(
            {
                "progressPct": row.get("progressPct"),
                "watchStatus": row.get("watchStatus"),
                "lastWatchedAt": row.get("lastWatchedAt"),
                "watchDevice": row.get("watchDevice"),
                "recommendationSource": "watch_history",
                "viewerState": "watched",
            }
        )
        output.append(item)
        if len(output) >= safe_limit:
            break
    return output


@agentc.catalog.tool
def get_catalogue_statistics(
    content_type: str | None = None, genre: str | None = None
) -> dict[str, Any]:
    """Return exact catalogue title counts through scoped MCP SQL++."""
    rows = _query_rows(
        f"""
        SELECT COUNT(1) AS count
        FROM `{_identifier(_env('TITLES_COLLECTION', 'titles'))}` AS t
        WHERE ($contentType IS NULL OR t.contentType=$contentType)
          AND ($genre IS NULL OR ANY g IN t.genres SATISFIES LOWER(g)=LOWER($genre) END)
        """,
        {"contentType": content_type, "genre": genre},
        scope_name=_env("CATALOGUE_SCOPE", "catalogue"),
    )
    count = int((rows[0] if rows else {}).get("count") or 0)
    return {
        "count": count,
        "contentType": content_type,
        "genre": genre,
        "queryMode": "agent_catalog_mcp_sqlpp_aggregate",
    }


@agentc.catalog.tool
def query_catalogue_analytics(
    measure: str,
    dimension: str,
    filters: dict[str, Any] | None = None,
    limit: int = 30,
    order: str = "desc",
) -> dict[str, Any]:
    """Run a bounded, read-only catalogue aggregation through scoped MCP SQL++."""
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
    filters = dict(filters or {})
    field, unnest = dimensions[dimension]
    content_type = filters.get("contentType")
    genre = filters.get("genre")
    release_year = filters.get("releaseYear")
    conditions = [
        f"{field} IS VALUED",
        "($contentType IS NULL OR t.contentType=$contentType)",
        "($genre IS NULL OR ANY g IN t.genres SATISFIES LOWER(g)=LOWER($genre) END)",
        "($releaseYear IS NULL OR t.releaseYear=$releaseYear)",
    ]
    if measure == "average_rating":
        conditions.append("t.voteAverage IS VALUED")
    elif measure == "average_runtime":
        conditions.append(
            "(t.runtimeMinutes IS VALUED OR t.episodeRuntimeMinutes IS VALUED)"
        )
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    rows = _query_rows(
        f"""
        SELECT {field} AS label, {measures[measure]} AS metricValue
        FROM `{_identifier(_env('TITLES_COLLECTION', 'titles'))}` AS t{unnest}
        WHERE {' AND '.join(conditions)}
        GROUP BY {field}
        ORDER BY metricValue {direction}, label ASC
        LIMIT $limit
        """,
        {
            "contentType": content_type,
            "genre": genre,
            "releaseYear": int(release_year) if release_year is not None else None,
            "limit": min(max(int(limit), 1), 50),
        },
        scope_name=_env("CATALOGUE_SCOPE", "catalogue"),
    )
    return {
        "measure": measure,
        "dimension": dimension,
        "filters": filters,
        "rows": [
            {"label": row.get("label"), "value": row.get("metricValue")}
            for row in rows
        ],
        "queryMode": "agent_catalog_mcp_validated_analytics",
        "readOnly": True,
    }


@agentc.catalog.tool
def resolve_catalogue_title(
    title: str, content_type: str | None = None
) -> dict[str, Any]:
    """Resolve an exact catalogue title using scoped MCP SQL++ before Search fallback."""
    rows = [
        _decorate_title(item)
        for item in _query_rows(
            f"""
            SELECT META(t).id AS id, t.*
            FROM `{_identifier(_env('TITLES_COLLECTION', 'titles'))}` AS t
            WHERE (LOWER(t.title)=LOWER($title) OR LOWER(t.originalTitle)=LOWER($title))
              AND ($contentType IS NULL OR t.contentType=$contentType)
            ORDER BY CASE WHEN LOWER(t.title)=LOWER($title) THEN 0 ELSE 1 END,
                     t.popularity DESC
            LIMIT 5
            """,
            {"title": title.strip(), "contentType": content_type},
            scope_name=_env("CATALOGUE_SCOPE", "catalogue"),
        )
    ]
    return {
        "match": rows[0] if rows else None,
        "alternatives": rows[1:5],
        "trace": {
            "mode": "agent_catalog_mcp_exact_title",
            "transport": "Couchbase MCP Server",
            "returnedCount": len(rows),
        },
    }


def _rating_value(value: Any) -> int:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else 0


def _tier_rank(value: Any) -> int:
    return {"free": 0, "basic": 1, "standard": 2, "premium": 3}.get(
        str(value or "standard").lower(), 2
    )


def _normalised_values(values: list[Any]) -> set[str]:
    return {
        re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
        for value in values
        if value
    }


def _exclusive_content_policy(profile: dict[str, Any]) -> dict[str, Any] | None:
    if str(profile.get("exclusivePreferenceMode") or "").lower() != "content":
        return None
    allowed_genres = [
        str(value).strip() for value in profile.get("preferredGenres", []) if str(value).strip()
    ]
    allowed_themes = [
        str(value).strip() for value in profile.get("preferredThemes", []) if str(value).strip()
    ]
    if not allowed_genres and not allowed_themes:
        return None
    label = ", ".join([*allowed_genres, *allowed_themes])
    return {
        "active": True,
        "mode": "content",
        "allowedGenres": allowed_genres,
        "allowedThemes": allowed_themes,
        "label": label,
    }


def _exclusive_content_conflict(
    title: dict[str, Any], profile: dict[str, Any]
) -> dict[str, Any] | None:
    policy = _exclusive_content_policy(profile)
    if policy is None:
        return None
    title_genres = _normalised_values(list(title.get("genres", [])))
    title_themes = _normalised_values(list(title.get("keywords", [])))
    if title_genres.intersection(
        _normalised_values(list(policy["allowedGenres"]))
    ) or title_themes.intersection(
        _normalised_values(list(policy["allowedThemes"]))
    ):
        return None
    label = str(policy["label"])
    return {
        "code": "exclusive_content_preference",
        "reason": f"blocked by the viewer's exclusive {label} preference",
        "policy": policy,
    }


@agentc.catalog.tool
def check_entitlement(viewer_id: str, title_id: str) -> dict[str, Any]:
    """Read title, profile and entitlement documents through MCP and apply policy."""
    title = _document(
        _env("CATALOGUE_SCOPE", "catalogue"),
        _env("TITLES_COLLECTION", "titles"),
        title_id,
    )
    profile = get_viewer_context(viewer_id)
    entitlement = _document(
        _env("OPERATIONS_SCOPE", "operations"),
        _env("ENTITLEMENTS_COLLECTION", "entitlements"),
        f"entitlement::{viewer_id}",
    )
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
    if offer_type == "included" and _tier_rank(viewer_tier) < _tier_rank(required_tier):
        reasons.append(f"requires the {required_tier} subscription tier")
    max_rating = _rating_value(entitlement.get("maxParentalRating"))
    title_rating = _rating_value(title.get("ageRating") or title.get("certification"))
    if title_rating > max_rating:
        reasons.append("blocked by the viewer's parental policy")
    if profile.get("avoidAdultContent") and (
        bool(title.get("adult"))
        or any(
            term
            in " ".join(
                [
                    str(title.get("overview") or ""),
                    *[str(item) for item in title.get("keywords", [])],
                ]
            ).lower()
            for term in ("explicit sexual", "adult content", "erotic", "pornograph", "nudity")
        )
    ):
        reasons.append("blocked by the viewer's adult-content preference")
    exclusive_conflict = _exclusive_content_conflict(title, profile)
    if exclusive_conflict is not None:
        reasons.append(str(exclusive_conflict["reason"]))
    allowed = not reasons
    included = allowed and offer_type == "included"
    return {
        "allowed": allowed,
        "decision": "allow" if allowed else "deny",
        "reason": "Included in the current plan"
        if included
        else ("Available as a separate purchase" if allowed else "; ".join(reasons)),
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
            else _exclusive_content_policy(profile)
        ),
        "preferenceConflict": exclusive_conflict is not None,
        "preferenceConflictCode": (
            exclusive_conflict["code"] if exclusive_conflict is not None else None
        ),
        "transport": "Couchbase MCP Server",
    }


@agentc.catalog.tool
def inspect_streamai_schema() -> dict[str, Any]:
    """Use MCP Server to list the StreamAI bucket scopes and collections."""
    bucket = _env("CONTENT_BUCKET", "streaming")
    schema = _call_mcp(
        "get_scopes_and_collections_in_bucket",
        {"bucket_name": bucket},
    )
    return {"bucket": bucket, "schema": schema}
