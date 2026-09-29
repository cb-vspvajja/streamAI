from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    category: str
    mutating: bool = False
    confirmation_required: bool = False
    components: tuple[str, ...] = ()
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentTrace:
    trace_id: str
    viewer_id: str
    session_id: str | None
    user_message: str
    started_at: float
    prompt_version: str
    status: str = "running"
    plan: dict[str, Any] = field(default_factory=dict)
    spans: list[dict[str, Any]] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=dict)
    grounded_title_ids: list[str] = field(default_factory=list)
    response_mode: str | None = None
    assistant_response: str | None = None
    llm_invoked: bool = False
    completed_at: float | None = None
    response_ms: float | None = None
    native_trace_session: str | None = None
    agent_catalog_id: str | None = None
    mcp_transport: str | None = None

    def as_document(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = "streamai_agent_trace"
        data["timestamp"] = self.started_at
        data["traceId"] = data.pop("trace_id")
        data["viewerId"] = data.pop("viewer_id")
        data["sessionId"] = data.pop("session_id")
        data["userMessage"] = data.pop("user_message")
        data["startedAt"] = data.pop("started_at")
        data["promptVersion"] = data.pop("prompt_version")
        data["groundedTitleIds"] = data.pop("grounded_title_ids")
        data["responseMode"] = data.pop("response_mode")
        data["assistantResponse"] = data.pop("assistant_response")
        data["llmInvoked"] = data.pop("llm_invoked")
        data["completedAt"] = data.pop("completed_at")
        data["responseMs"] = data.pop("response_ms")
        data["nativeTraceSession"] = data.pop("native_trace_session")
        data["agentCatalogId"] = data.pop("agent_catalog_id")
        data["mcpTransport"] = data.pop("mcp_transport")
        return data


class GovernedAgentService:
    """Governed orchestration backed by native Agent Catalog, MCP and Tracer.

    Read tools are loaded from the published Couchbase Agent Catalog and execute
    through Couchbase MCP Server. Validated write actions stay in the application
    service boundary so a read-only MCP deployment cannot bypass business policy.
    """

    PROMPT_VERSION = "streaming-agent-system-v1.0.0"
    SYSTEM_PROMPT = (
        "You are a streaming assistant. Treat verified catalogue records, viewer "
        "state and entitlement decisions as authoritative. Never name, describe, "
        "recommend or act on a title unless a governed tool returned that title in "
        "the current turn. Never claim that a title is included in a subscription "
        "unless the entitlement tool approved it. Ask for confirmation only when a "
        "tool policy requires it. Catalogue analytics must use the bounded read-only "
        "analytics tool; never generate SQL or supply facts that its rows did not return."
        " A cached assistant plan is reusable control metadata, never catalogue "
        "evidence; execute it against current catalogue, viewer and entitlement "
        "data before naming any title."
    )

    CATALOG_MCP_TOOL_NAMES: tuple[str, ...] = (
        "get_viewer_context",
        "get_watch_history",
        "get_catalogue_statistics",
        "query_catalogue_analytics",
        "resolve_catalogue_title",
        "check_entitlement",
        "inspect_streamai_schema",
    )

    TOOL_SPECS: tuple[ToolSpec, ...] = (
        ToolSpec(
            name="get_viewer_context",
            description="Read the authoritative viewer profile, household context and safety preferences.",
            category="context",
            components=("Data/KV", "Agent Memory"),
            input_schema={"viewer_id": "string"},
        ),
        ToolSpec(
            name="update_viewer_preference",
            description="Persist an explicit durable preference or safety exclusion for the signed-in viewer.",
            category="action",
            mutating=True,
            components=("Data/KV", "Agent Memory"),
            input_schema={"viewer_id": "string", "preferences": "array"},
        ),
        ToolSpec(
            name="get_watch_history",
            description="Read structured watch history and playback progress for the signed-in viewer.",
            category="context",
            components=("Query", "Index", "Data/KV"),
            input_schema={"viewer_id": "string", "content_type": "movie|tv|null"},
        ),
        ToolSpec(
            name="update_viewer_entitlement",
            description="Update the signed-in viewer's authoritative region for availability and entitlement decisions.",
            category="action",
            mutating=True,
            components=("Data/KV", "Agent Memory"),
            input_schema={"viewer_id": "string", "region_code": "ISO-3166 alpha-2"},
        ),
        ToolSpec(
            name="get_catalogue_statistics",
            description="Return exact catalogue inventory counts using content-type and genre filters.",
            category="retrieval",
            components=("Query", "Index", "Data/KV"),
            input_schema={"content_type": "movie|tv|null", "genre": "string|null"},
        ),
        ToolSpec(
            name="query_catalogue_analytics",
            description="Run a bounded read-only catalogue aggregation using allowlisted measures, dimensions and filters.",
            category="retrieval",
            components=("Query", "Index", "Data/KV"),
            input_schema={
                "measure": "count|average_rating|average_runtime",
                "dimension": "genre|release_year|original_language|country|content_type",
                "filters": "object",
                "limit": "integer<=50",
                "order": "asc|desc",
            },
        ),
        ToolSpec(
            name="recall_previous_turn",
            description="Read the immediately preceding verified conversation turn from active or retained Agent Memory.",
            category="context",
            components=("Agent Memory",),
            input_schema={"viewer_id": "string"},
        ),
        ToolSpec(
            name="get_my_list",
            description="Read exact current My List title IDs and hydrate their catalogue records.",
            category="context",
            components=("Data/KV", "Query", "Index"),
            input_schema={"viewer_id": "string", "limit": "integer"},
        ),
        ToolSpec(
            name="get_recent_viewer_actions",
            description="Read completed viewer action receipts, ordered by event time.",
            category="context",
            components=("Query", "Index", "Data/KV"),
            input_schema={"viewer_id": "string", "action": "watchlist", "limit": "integer"},
        ),
        ToolSpec(
            name="filter_grounded_results",
            description="Intersect a follow-up condition with exact title IDs returned by the previous grounded turn.",
            category="policy",
            components=("Data/KV", "Query", "Index"),
            input_schema={"viewer_id": "string", "title_ids": "array", "filter": "string"},
        ),
        ToolSpec(
            name="find_similar_to_grounded_titles",
            description="Combine up to five exact catalogue title embeddings and retrieve similar governed candidates.",
            category="retrieval",
            components=("Search/Vector", "Data/KV"),
            input_schema={"viewer_id": "string", "title_ids": "array<=5", "limit": "integer"},
        ),
        ToolSpec(
            name="resolve_catalogue_title",
            description="Resolve a natural title reference to an exact catalogue record.",
            category="retrieval",
            components=("Search/FTS", "Data/KV"),
            input_schema={"title": "string", "content_type": "movie|tv|null"},
        ),
        ToolSpec(
            name="find_available_content",
            description="Retrieve catalogue candidates with lexical, vector or hybrid search and structured filters.",
            category="retrieval",
            components=("Search/FTS", "Search/Vector", "Query", "Data/KV"),
            input_schema={"query": "string", "mode": "fts|vector|hybrid", "limit": "integer"},
        ),
        ToolSpec(
            name="rank_personalised_candidates",
            description="Rank only candidates with auditable viewer-profile evidence and remove excluded titles.",
            category="ranking",
            components=("Data/KV", "Search/FTS"),
            input_schema={"viewer_id": "string", "limit": "integer"},
        ),
        ToolSpec(
            name="query_catalogue_ratings",
            description="Read the highest-rated catalogue title using authoritative rating fields.",
            category="retrieval",
            components=("Query", "Index", "Data/KV"),
            input_schema={"content_type": "movie|tv|null"},
        ),
        ToolSpec(
            name="query_catalogue_plan",
            description="Execute a validated structured catalogue plan using fresh Query, FTS, Vector and viewer data.",
            category="retrieval",
            components=("Data/KV", "Query", "Index", "Search/FTS", "Search/Vector"),
            input_schema={
                "viewer_id": "string",
                "plan": "validated catalogue plan",
                "search_mode": "fts|vector|hybrid",
            },
        ),
        ToolSpec(
            name="score_title_affinity",
            description="Evaluate one grounded catalogue title against verified viewer-profile evidence.",
            category="ranking",
            components=("Data/KV",),
            input_schema={"viewer_id": "string", "title_id": "string"},
        ),
        ToolSpec(
            name="check_entitlement",
            description="Check region, subscription tier, title availability and parental policy before a title is shown or acted on.",
            category="policy",
            components=("Data/KV",),
            input_schema={"viewer_id": "string", "title_id": "string"},
        ),
        ToolSpec(
            name="inspect_streamai_schema",
            description="Inspect the StreamAI bucket schema through the read-only Couchbase MCP Server.",
            category="operator",
            components=("MCP Server", "Data/KV", "Query"),
            input_schema={},
        ),
        ToolSpec(
            name="record_like",
            description="Persist an exact liked-title signal for the signed-in viewer using a resolved catalogue ID.",
            category="action",
            mutating=True,
            components=("Data/KV", "Agent Memory"),
            input_schema={"viewer_id": "string", "title_id": "string"},
        ),
        ToolSpec(
            name="update_watchlist",
            description="Add or remove an entitled catalogue title from the viewer's watchlist.",
            category="action",
            mutating=True,
            components=("Data/KV",),
            input_schema={"viewer_id": "string", "title_id": "string", "operation": "add|remove"},
        ),
        ToolSpec(
            name="record_not_interested",
            description="Persist a negative title signal and immediately suppress it from recommendations.",
            category="action",
            mutating=True,
            components=("Data/KV", "Agent Memory"),
            input_schema={"viewer_id": "string", "title_id": "string"},
        ),
        ToolSpec(
            name="start_or_resume_playback",
            description="Start or resume an entitled title and update cross-device playback state.",
            category="action",
            mutating=True,
            components=("Data/KV",),
            input_schema={"viewer_id": "string", "title_id": "string"},
        ),
    )

    ORDINALS: dict[str, int] = {
        "first": 0,
        "1st": 0,
        "one": 0,
        "second": 1,
        "2nd": 1,
        "two": 1,
        "third": 2,
        "3rd": 2,
        "three": 2,
        "fourth": 3,
        "4th": 3,
        "four": 3,
        "fifth": 4,
        "5th": 4,
        "five": 4,
    }
    BATCH_COUNTS: dict[str, int] = {
        "1": 1,
        "one": 1,
        "2": 2,
        "two": 2,
        "3": 3,
        "three": 3,
        "4": 4,
        "four": 4,
        "5": 5,
        "five": 5,
        "6": 6,
        "six": 6,
        "7": 7,
        "seven": 7,
        "8": 8,
        "eight": 8,
        "9": 9,
        "nine": 9,
        "10": 10,
        "ten": 10,
    }
    MAX_BATCH_ACTION_TITLES = 5
    MULTI_TITLE_ACTIONS = {
        "like_title",
        "watchlist_add",
        "watchlist_remove",
        "not_interested",
    }

    def __init__(
        self,
        catalogue: Any,
        *,
        agent_catalog_gateway: Any = None,
        mcp_gateway: Any = None,
    ) -> None:
        self.catalogue = catalogue
        self.agent_catalog_gateway = agent_catalog_gateway
        self.mcp_gateway = mcp_gateway
        self._tool_by_name = {item.name: item for item in self.TOOL_SPECS}
        native_prompt = (
            agent_catalog_gateway.prompt_content
            if agent_catalog_gateway is not None and agent_catalog_gateway.healthy
            else None
        )
        self.system_prompt = native_prompt or self.SYSTEM_PROMPT
        if agent_catalog_gateway is None or not agent_catalog_gateway.healthy:
            self.sync_catalog()

    def sync_catalog(self) -> None:
        documents = [
            {
                "type": "streamai_agent_tool",
                "name": item.name,
                "description": item.description,
                "category": item.category,
                "mutating": item.mutating,
                "confirmationRequired": item.confirmation_required,
                "components": list(item.components),
                "inputSchema": item.input_schema,
                "version": "1.0.0",
                "status": "active",
            }
            for item in self.TOOL_SPECS
        ]
        documents.append(
            {
                "type": "streamai_agent_prompt",
                "name": self.PROMPT_VERSION,
                "version": "1.0.0",
                "content": self.SYSTEM_PROMPT,
                "status": "active",
            }
        )
        try:
            self.catalogue.sync_agent_catalog(documents)
        except Exception:
            # Tool execution must remain available if the optional registry is
            # warming up. The status endpoint exposes registry health separately.
            pass

    def catalog_snapshot(self) -> dict[str, Any]:
        native = (
            self.agent_catalog_gateway.snapshot()
            if self.agent_catalog_gateway is not None
            else {"enabled": False, "healthy": False}
        )
        mcp = (
            self.mcp_gateway.snapshot()
            if self.mcp_gateway is not None
            else {"enabled": False, "healthy": False}
        )
        return {
            "prompt": {
                "name": (native.get("promptName") or self.PROMPT_VERSION),
                "version": native.get("catalogId") or "1.0.0",
                "content": self.system_prompt,
                "source": "Couchbase Agent Catalog" if native.get("healthy") else "embedded fallback",
            },
            "tools": [asdict(item) for item in self.TOOL_SPECS],
            "execution": (
                "agent_catalog_tools_over_couchbase_mcp"
                if native.get("healthy") and mcp.get("healthy")
                else "degraded_direct_sdk_fallback"
            ),
            "catalogMode": "native_agent_catalog" if native.get("healthy") else "fallback_registry",
            "mcpMode": "native_streamable_http" if mcp.get("healthy") else "unavailable",
            "agentCatalog": native,
            "mcpServer": mcp,
            "agentTracer": {
                "healthy": bool(native.get("healthy") and native.get("nativeTracerEnabled") and not native.get("nativeTraceError")),
                "source": "Agent Catalog Span API",
                "error": native.get("nativeTraceError"),
            },
        }

    def new_trace(
        self, *, viewer_id: str, session_id: str | None, user_message: str
    ) -> AgentTrace:
        trace = AgentTrace(
            trace_id=f"agent-trace::{int(time.time() * 1000)}::{uuid.uuid4().hex[:10]}",
            viewer_id=viewer_id,
            session_id=session_id,
            user_message=user_message,
            started_at=time.time(),
            prompt_version=(
                self.agent_catalog_gateway.settings.agent_catalog_prompt_name
                if self.agent_catalog_gateway is not None and self.agent_catalog_gateway.healthy
                else self.PROMPT_VERSION
            ),
            agent_catalog_id=(
                self.agent_catalog_gateway.catalog_id
                if self.agent_catalog_gateway is not None
                else None
            ),
            mcp_transport=(
                "streamable_http"
                if self.mcp_gateway is not None and self.mcp_gateway.healthy
                else None
            ),
            policy={
                "catalogueGroundingRequired": True,
                "entitlementRequiredForTitles": True,
                "unsupportedTitleClaimsAllowed": False,
                "writeActionsRequireResolvedTitle": True,
                "agentCatalogRequired": bool(
                    self.agent_catalog_gateway and self.agent_catalog_gateway.required
                ),
                "mcpRequired": bool(self.mcp_gateway and self.mcp_gateway.required),
            },
        )
        trace.native_trace_session = f"streamai::{viewer_id}::{session_id or trace.trace_id}"
        trace.policy["nativeAgentTracerLogged"] = False
        if self.agent_catalog_gateway is not None:
            try:
                native_span = self.agent_catalog_gateway.start_trace(
                    trace_id=trace.trace_id,
                    viewer_id=viewer_id,
                    session_id=session_id,
                    user_message=user_message,
                )
                setattr(trace, "_native_span", native_span)
            except Exception as exc:
                trace.policy["nativeTraceStartError"] = f"{type(exc).__name__}: {exc}"
        return trace

    def set_plan(
        self,
        trace: AgentTrace,
        *,
        intent: str,
        tools: list[str],
        entities: dict[str, Any] | None = None,
        deterministic: bool = True,
    ) -> None:
        trace.plan = {
            "intent": intent,
            "tools": tools,
            "entities": entities or {},
            "deterministic": deterministic,
        }

    def record_tool(
        self,
        trace: AgentTrace,
        *,
        name: str,
        started: float,
        status: str,
        inputs: dict[str, Any] | None = None,
        output: Any = None,
        error: str | None = None,
        execution_source: str = "application_sdk",
        catalog_id: str | None = None,
    ) -> None:
        spec = self._tool_by_name.get(name)
        safe_output = self._summarise_output(output)
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        completed_at = time.time()
        trace.spans.append(
            {
                "spanId": uuid.uuid4().hex[:12],
                "name": name,
                "type": "tool_call",
                "status": status,
                "startedAt": completed_at - (duration_ms / 1000.0),
                "completedAt": completed_at,
                "durationMs": duration_ms,
                "inputs": self._safe_inputs(inputs or {}),
                "output": safe_output,
                "error": error,
                "mutating": bool(spec.mutating) if spec else False,
                "components": list(spec.components) if spec else [],
                "executionSource": execution_source,
                "catalogId": catalog_id,
                "transport": "mcp" if execution_source == "agent_catalog_mcp" else "direct",
            }
        )

    async def try_catalog_tool(
        self,
        trace: AgentTrace,
        *,
        name: str,
        inputs: dict[str, Any],
        fallback_callable: Any,
    ) -> dict[str, Any]:
        gateway = self.agent_catalog_gateway
        if gateway is None or not gateway.healthy or not gateway.tool_available(name):
            return {"used": False}
        expected_functions = {
            "get_viewer_context": {"get_profile"},
            "get_watch_history": {"list_watch_history"},
            "get_catalogue_statistics": {"count_titles"},
            "query_catalogue_analytics": {"query_catalogue_analytics"},
            "resolve_catalogue_title": {"resolve_title"},
            "check_entitlement": {"check_entitlement"},
        }
        callable_name = str(getattr(fallback_callable, "__name__", ""))
        if callable_name not in expected_functions.get(name, set()):
            return {"used": False}
        runtime_executor = None
        if self.mcp_gateway is not None and self.mcp_gateway.healthy:
            runtime_executor = self.mcp_gateway.call_business_tool
        elif str(getattr(gateway.settings, "agent_catalog_execution_mode", "mcp")).lower() == "mcp":
            if gateway.required:
                raise RuntimeError("Agent Catalog tool is available but the required MCP runtime is unhealthy")
        execution = await gateway.execute_tool(
            name=name,
            inputs=inputs,
            root_span=getattr(trace, "_native_span", None),
            trace_id=trace.trace_id,
            runtime_executor=runtime_executor,
        )
        fallback = bool(
            name == "resolve_catalogue_title"
            and isinstance(execution.result, dict)
            and not execution.result.get("match")
        )
        return {
            "used": True,
            "result": execution.result,
            "durationMs": execution.duration_ms,
            "catalogId": execution.catalog_id,
            "fallback": fallback,
        }

    @staticmethod
    def _safe_inputs(values: dict[str, Any]) -> dict[str, Any]:
        safe: dict[str, Any] = {}
        for key, value in values.items():
            if key.lower() in {"pin", "password", "token", "embedding"}:
                safe[key] = "[redacted]"
            elif isinstance(value, str):
                safe[key] = value[:300]
            elif isinstance(value, (int, float, bool)) or value is None:
                safe[key] = value
            elif isinstance(value, list):
                safe[key] = value[:20]
            else:
                safe[key] = str(value)[:300]
        return safe

    @staticmethod
    def _summarise_output(output: Any) -> dict[str, Any]:
        if output is None:
            return {"result": None}
        if isinstance(output, dict):
            result: dict[str, Any] = {}
            if "match" in output:
                match = output.get("match") or {}
                result["matchedTitle"] = {
                    "id": match.get("id"),
                    "title": match.get("title"),
                } if match else None
            if "results" in output:
                docs = list(output.get("results") or [])
                result["resultCount"] = len(docs)
                result["titleIds"] = [item.get("id") for item in docs[:20]]
                result["titles"] = [item.get("title") for item in docs[:20]]
            if "rows" in output:
                rows = list(output.get("rows") or [])
                result["groupCount"] = len(rows)
                result["groups"] = rows[:20]
            if output.get("userMessage"):
                result["previousUserMessage"] = str(output.get("userMessage"))[:500]
                result["memorySource"] = output.get("source")
            if output.get("assistantMessage"):
                result["previousAssistantMessage"] = str(
                    output.get("assistantMessage")
                )[:500]
            for key in (
                "allowed",
                "decision",
                "reason",
                "action",
                "titleId",
                "title",
                "includedInPlan",
                "region",
                "subscriptionTier",
                "traceId",
            ):
                if key in output:
                    result[key] = output.get(key)
            if result:
                return result
            return {"keys": sorted(output.keys())[:30]}
        if isinstance(output, list):
            return {
                "resultCount": len(output),
                "titleIds": [item.get("id") for item in output[:20] if isinstance(item, dict)],
                "titles": [item.get("title") for item in output[:20] if isinstance(item, dict)],
            }
        return {"result": str(output)[:500]}

    def finish_trace(
        self,
        trace: AgentTrace,
        *,
        response_mode: str,
        assistant_response: str,
        grounded_title_ids: list[str],
        llm_invoked: bool,
        response_ms: float,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        trace.status = "completed"
        trace.response_mode = response_mode
        trace.assistant_response = assistant_response[:5000]
        trace.grounded_title_ids = list(dict.fromkeys(str(x) for x in grounded_title_ids if x))
        trace.llm_invoked = llm_invoked
        trace.response_ms = response_ms
        trace.completed_at = time.time()
        if policy:
            trace.policy.update(policy)
        if self.agent_catalog_gateway is not None:
            try:
                logged = self.agent_catalog_gateway.finish_trace(
                    getattr(trace, "_native_span", None),
                    assistant_response=assistant_response,
                    response_mode=response_mode,
                    response_ms=response_ms,
                    grounded_title_ids=trace.grounded_title_ids,
                )
                trace.policy["nativeAgentTracerLogged"] = bool(logged)
            except Exception as exc:
                trace.policy["nativeAgentTracerLogged"] = False
                trace.policy["nativeAgentTracerError"] = f"{type(exc).__name__}: {exc}"
        document = trace.as_document()
        self.catalogue.write_agent_trace(document)
        return document

    def fail_trace(self, trace: AgentTrace, exc: Exception) -> None:
        trace.status = "failed"
        trace.completed_at = time.time()
        trace.policy["failure"] = f"{type(exc).__name__}: {exc}"
        if self.agent_catalog_gateway is not None:
            try:
                logged = self.agent_catalog_gateway.fail_trace(getattr(trace, "_native_span", None), exc)
                trace.policy["nativeAgentTracerLogged"] = bool(logged)
            except Exception as trace_exc:
                trace.policy["nativeAgentTracerLogged"] = False
                trace.policy["nativeAgentTracerError"] = f"{type(trace_exc).__name__}: {trace_exc}"
        try:
            self.catalogue.write_agent_trace(trace.as_document())
        except Exception:
            pass


    @staticmethod
    def _trace_event_timestamp(value: Any, fallback: float = 0.0) -> float:
        try:
            return float(value or fallback)
        except (TypeError, ValueError):
            return float(fallback)

    @classmethod
    def build_session_thread(cls, traces: list[dict[str, Any]]) -> dict[str, Any]:
        """Build a redaction-safe, presenter-friendly session timeline.

        The persisted StreamAI trace is the local operational projection of the
        native Agent Tracer session. Tool inputs have already been redacted by
        ``_safe_inputs`` and tool outputs are deliberately summarised before
        persistence. The projection therefore exposes useful activity without
        leaking API keys, passwords, raw embeddings or unbounded result sets.
        """
        ordered = sorted(
            [dict(item) for item in traces],
            key=lambda item: cls._trace_event_timestamp(item.get("startedAt") or item.get("timestamp")),
        )
        if not ordered:
            return {
                "sessionId": None,
                "viewerId": None,
                "turnCount": 0,
                "eventCount": 0,
                "components": [],
                "catalogIds": [],
                "turns": [],
                "events": [],
            }

        session_id = ordered[0].get("sessionId")
        viewer_id = ordered[0].get("viewerId")
        all_events: list[dict[str, Any]] = []
        turns: list[dict[str, Any]] = []
        components: list[str] = []
        catalog_ids: list[str] = []

        def add_component(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in components:
                components.append(text)

        for turn_number, trace in enumerate(ordered, start=1):
            trace_id = str(trace.get("traceId") or f"turn-{turn_number}")
            started_at = cls._trace_event_timestamp(trace.get("startedAt") or trace.get("timestamp"))
            completed_at = cls._trace_event_timestamp(trace.get("completedAt"), started_at)
            plan = dict(trace.get("plan") or {})
            spans = list(trace.get("spans") or trace.get("toolCalls") or [])
            catalog_id = str(trace.get("agentCatalogId") or "").strip()
            if catalog_id and catalog_id not in catalog_ids:
                catalog_ids.append(catalog_id)

            turn_events: list[dict[str, Any]] = []

            def append_event(event: dict[str, Any]) -> None:
                event.setdefault("eventId", f"{trace_id}::{len(turn_events) + 1}")
                event.setdefault("traceId", trace_id)
                event.setdefault("turnNumber", turn_number)
                event.setdefault("sessionId", session_id)
                turn_events.append(event)
                all_events.append(event)

            append_event({
                "type": "user_message",
                "label": "User",
                "timestamp": started_at,
                "status": "completed",
                "content": str(trace.get("userMessage") or ""),
                "component": "Conversation",
            })
            add_component("Conversation")

            append_event({
                "type": "agent_plan",
                "label": "Router and agent plan",
                "timestamp": started_at + 0.0001,
                "status": "completed",
                "component": "Agent Catalog",
                "content": str(plan.get("intent") or trace.get("responseMode") or "agent_turn"),
                "details": {
                    "intent": plan.get("intent"),
                    "tools": list(plan.get("tools") or []),
                    "entities": dict(plan.get("entities") or {}),
                    "deterministic": plan.get("deterministic", True),
                    "promptVersion": trace.get("promptVersion"),
                    "agentCatalogId": trace.get("agentCatalogId"),
                    "nativeTraceSession": trace.get("nativeTraceSession"),
                },
            })
            add_component("Agent Catalog")

            for span_index, raw_span in enumerate(spans):
                span = dict(raw_span or {})
                span_components = [str(item) for item in span.get("components") or [] if str(item)]
                for component in span_components:
                    add_component(component)
                execution_source = str(span.get("executionSource") or "application_sdk")
                if execution_source == "agent_catalog_mcp":
                    add_component("MCP Server")
                append_event({
                    "type": "tool_call",
                    "label": str(span.get("name") or "tool"),
                    "timestamp": cls._trace_event_timestamp(span.get("startedAt"), started_at),
                    "completedAt": cls._trace_event_timestamp(span.get("completedAt"), completed_at),
                    "status": str(span.get("status") or "unknown"),
                    "durationMs": span.get("durationMs"),
                    "component": "Agent Catalog → MCP" if execution_source == "agent_catalog_mcp" else "Application SDK",
                    "components": span_components,
                    "spanId": span.get("spanId"),
                    "spanIndex": span_index,
                    "transport": span.get("transport"),
                    "catalogId": span.get("catalogId") or trace.get("agentCatalogId"),
                    "mutating": bool(span.get("mutating")),
                    "inputs": dict(span.get("inputs") or {}),
                    "output": span.get("output"),
                    "error": span.get("error"),
                })

            if trace.get("llmInvoked"):
                add_component("Model Runtime")
                append_event({
                    "type": "model_generation",
                    "label": "Model generation",
                    "timestamp": max(started_at, completed_at - 0.001),
                    "status": "completed" if trace.get("status") == "completed" else trace.get("status"),
                    "component": "Model Runtime",
                    "content": "The language model generated or refined the grounded response.",
                    "details": {"responseMode": trace.get("responseMode")},
                })

            if trace.get("assistantResponse"):
                append_event({
                    "type": "assistant_message",
                    "label": "Assistant",
                    "timestamp": completed_at,
                    "status": str(trace.get("status") or "completed"),
                    "content": str(trace.get("assistantResponse") or ""),
                    "component": "Conversation",
                    "details": {
                        "responseMode": trace.get("responseMode"),
                        "responseMs": trace.get("responseMs"),
                        "groundedTitleIds": list(trace.get("groundedTitleIds") or []),
                        "llmInvoked": bool(trace.get("llmInvoked")),
                        "policy": dict(trace.get("policy") or {}),
                    },
                })
            elif trace.get("status") == "failed":
                append_event({
                    "type": "error",
                    "label": "Turn failed",
                    "timestamp": completed_at,
                    "status": "failed",
                    "content": str((trace.get("policy") or {}).get("failure") or "Unknown failure"),
                    "component": "Agent Tracer",
                })

            turns.append({
                "turnNumber": turn_number,
                "traceId": trace_id,
                "sessionId": trace.get("sessionId"),
                "status": trace.get("status"),
                "startedAt": started_at,
                "completedAt": completed_at,
                "responseMs": trace.get("responseMs"),
                "intent": plan.get("intent") or trace.get("responseMode"),
                "llmInvoked": bool(trace.get("llmInvoked")),
                "groundedTitleIds": list(trace.get("groundedTitleIds") or []),
                "userMessage": trace.get("userMessage"),
                "assistantResponse": trace.get("assistantResponse"),
                "events": turn_events,
                "rawTrace": trace,
            })

        all_events.sort(key=lambda item: cls._trace_event_timestamp(item.get("timestamp")))
        return {
            "sessionId": session_id,
            "viewerId": viewer_id,
            "turnCount": len(turns),
            "eventCount": len(all_events),
            "startedAt": turns[0]["startedAt"],
            "completedAt": turns[-1]["completedAt"],
            "components": components,
            "catalogIds": catalog_ids,
            "turns": turns,
            "events": all_events,
        }

    @classmethod
    def extract_action_request(
        cls, message: str, previous: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        text = re.sub(r"\s+", " ", message.strip())
        lower = text.lower()
        missing_match = re.fullmatch(
            r"(?:please\s+)?(?:add|put|save)\s+(?:all\s+)?(?:the\s+)?"
            r"(?:ones?|titles?|movies?|films?|shows?|series)\s+"
            r"(?:that\s+are\s+)?(?:missing|not|not\s+already|remaining)\s+"
            r"(?:from|in|on)\s+(?:my\s+)?(?:watchlist|watch list|list)\??",
            lower,
        )
        if missing_match:
            return cls._collection_action_request(
                "watchlist_add",
                "the ones missing from My List",
                previous or {},
                selection_mode="previous_missing_from_watchlist",
            )
        patterns: tuple[tuple[str, str], ...] = (
            ("like_title", r"^(?:please\s+)?(?:like|favou?rite)\s+(.+?)\??$"),
            ("like_title", r"^(?:please\s+)?(?:add|put|save|mark)\s+(.+?)\s+(?:to|as)\s+(?:my\s+)?(?:likes?|liked\s+(?:titles?|movies?|films?|shows?|content|themes?|theames?)|favou?rites?)\??$"),
            ("watchlist_add", r"^(?:please\s+)?(?:add|put|save)\s+(.+?)\s+(?:to|on)\s+(?:my\s+)?(?:watchlist|watch list|list)\??$"),
            ("watchlist_remove", r"^(?:please\s+)?(?:remove|delete|take)\s+(.+?)\s+from\s+(?:my\s+)?(?:watchlist|watch list|list)\??$"),
            ("not_interested", r"^(?:please\s+)?(?:mark\s+)?(.+?)\s+(?:as\s+)?(?:not interested|not for me)\??$"),
            ("not_interested", r"^(?:please\s+)?(?:do not|don't)\s+recommend\s+(.+?)\??$"),
            ("play", r"^(?:please\s+)?(?:play|start|resume|continue)\s+(.+?)\??$"),
        )
        for action, pattern in patterns:
            match = re.match(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            reference = cls._clean_reference(match.group(1))
            selection = (
                cls._resolve_ordinal_selection(reference, previous or {})
                if action in cls.MULTI_TITLE_ACTIONS
                else {"matched": False, "items": []}
            )
            resolved_items = list(selection.get("items") or [])
            resolved = (
                resolved_items[0]
                if len(resolved_items) == 1
                else cls.resolve_previous_title_reference(reference, previous or {})
            )
            if selection.get("matched") or resolved:
                reference = re.sub(r"^the\s+", "", reference, flags=re.IGNORECASE)
            request = {
                "action": action,
                "reference": reference,
                "resolvedTitleId": resolved.get("id") if resolved else None,
                "resolvedTitle": resolved.get("title") if resolved else None,
                "source": (
                    "previous_results"
                    if selection.get("matched") or resolved
                    else "natural_title_reference"
                ),
            }
            if selection.get("selectionMode"):
                request["selectionMode"] = selection.get("selectionMode")
            if len(resolved_items) > 1:
                request.update(
                    {
                        "resolvedTitleIds": [item.get("id") for item in resolved_items],
                        "resolvedTitles": [item.get("title") for item in resolved_items],
                        "requestedCount": int(selection.get("requestedCount") or len(resolved_items)),
                    }
                )
            elif selection.get("selectionError"):
                request.update(
                    {
                        "requestedCount": int(selection.get("requestedCount") or 0),
                        "availableCount": int(selection.get("availableCount") or 0),
                        "selectionError": selection.get("selectionError"),
                    }
                )
            return request
        # Natural shorthand used after recommendations.
        ordinal_match = re.match(
            r"^(?:add|save)\s+the\s+(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)\s+(?:one\s+)?(?:to\s+)?(?:my\s+)?(?P<target>watchlist|watch list|list|likes?|liked\s+(?:titles?|movies?|films?|shows?|content|themes?|theames?)|favou?rites?)$",
            lower,
        )
        if ordinal_match:
            reference = ordinal_match.group(1)
            target = ordinal_match.group("target")
            resolved = cls._resolve_ordinal_reference(reference, previous or {})
            return {
                "action": "like_title" if re.search(r"likes?|liked|favou?rites?", target) else "watchlist_add",
                "reference": reference,
                "resolvedTitleId": resolved.get("id") if resolved else None,
                "resolvedTitle": resolved.get("title") if resolved else None,
                "source": "previous_results",
            }
        return None

    @classmethod
    def _collection_action_request(
        cls,
        action: str,
        reference: str,
        previous: dict[str, Any],
        *,
        selection_mode: str,
    ) -> dict[str, Any]:
        ids = [str(value) for value in previous.get("resultTitleIds", []) if value]
        titles = [str(value) for value in previous.get("resultTitles", []) if value]
        request: dict[str, Any] = {
            "action": action,
            "reference": reference,
            "source": "previous_results",
            "selectionMode": selection_mode,
            "requestedCount": len(ids),
            "availableCount": len(ids),
        }
        if not ids:
            request["selectionError"] = "no_previous_results"
            return request
        if (
            len(ids) > cls.MAX_BATCH_ACTION_TITLES
            and selection_mode != "previous_missing_from_watchlist"
        ):
            request["selectionError"] = "batch_limit_exceeded"
            return request
        request["resolvedTitleIds"] = ids
        request["resolvedTitles"] = [
            titles[index] if index < len(titles) else None for index in range(len(ids))
        ]
        return request

    @staticmethod
    def _clean_reference(value: str) -> str:
        text = re.sub(r"^(?:the\s+)?(?:movie|film|show|series|title)\s+", "", value.strip(), flags=re.IGNORECASE)
        return text.strip(" .?!\"'")

    @classmethod
    def _resolve_ordinal_reference(
        cls, reference: str, previous: dict[str, Any]
    ) -> dict[str, Any] | None:
        lower = reference.lower().strip()
        index = None
        for token, value in cls.ORDINALS.items():
            if re.fullmatch(rf"(?:the\s+)?{re.escape(token)}(?:\s+one)?", lower):
                index = value
                break
        if index is None:
            return None
        ids = list(previous.get("resultTitleIds") or [])
        titles = list(previous.get("resultTitles") or [])
        if index >= len(ids):
            return None
        return {
            "id": ids[index],
            "title": titles[index] if index < len(titles) else None,
        }

    @classmethod
    def resolve_previous_title_reference(
        cls, reference: str, previous: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Resolve singular conversational title references to one exact prior ID."""
        cleaned = cls._clean_reference(reference)
        lower = re.sub(r"\s+", " ", cleaned.lower()).strip()
        if re.fullmatch(
            r"(?:this|that|it|this one|that one|this title|that title|the title)",
            lower,
        ):
            title_id = str(
                previous.get("lastGroundedTitleId")
                or next(iter(previous.get("resultTitleIds") or []), "")
            )
            title = str(
                previous.get("lastGroundedTitle")
                or next(iter(previous.get("resultTitles") or []), "")
            )
            return {"id": title_id, "title": title} if title_id else None
        return cls._resolve_ordinal_reference(lower, previous)

    @classmethod
    def _resolve_ordinal_selection(
        cls, reference: str, previous: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve a bounded prefix of the immediately preceding result list.

        The reference is never treated as a catalogue search term. A matched
        request either resolves every requested exact ID or returns an explicit
        selection error without mutating viewer state.
        """
        lower = re.sub(r"\s+", " ", reference.lower().strip())
        count: int | None = None
        prefix_match = re.fullmatch(
            r"(?:the\s+)?(?:first|top)\s+"
            r"(?P<count>\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
            r"(?:\s+(?:ones?|titles?|movies?|films?|shows?|series))?"
            r"(?:\s+of\s+(?:them|those))?",
            lower,
        )
        if prefix_match:
            count_token = prefix_match.group("count")
            count = (
                int(count_token)
                if count_token.isdigit()
                else cls.BATCH_COUNTS.get(count_token)
            )
        elif re.fullmatch(r"(?:the\s+)?both(?:\s+of\s+(?:them|those))?", lower):
            count = 2
        elif re.fullmatch(
            r"(?:all(?:\s+of)?\s+)?(?:these|those|them|the\s+ones|the\s+titles|"
            r"these\s+ones|those\s+ones|all)",
            lower,
        ):
            ids = [str(item) for item in list(previous.get("resultTitleIds") or []) if item]
            titles = list(previous.get("resultTitles") or [])
            if not ids:
                return {
                    "matched": True,
                    "requestedCount": 0,
                    "availableCount": 0,
                    "items": [],
                    "selectionError": "no_previous_results",
                    "selectionMode": "previous_all",
                }
            if len(ids) > cls.MAX_BATCH_ACTION_TITLES:
                return {
                    "matched": True,
                    "requestedCount": len(ids),
                    "availableCount": len(ids),
                    "items": [],
                    "selectionError": "batch_limit_exceeded",
                    "selectionMode": "previous_all",
                }
            return {
                "matched": True,
                "requestedCount": len(ids),
                "availableCount": len(ids),
                "selectionMode": "previous_all",
                "items": [
                    {
                        "id": title_id,
                        "title": titles[index] if index < len(titles) else None,
                    }
                    for index, title_id in enumerate(ids)
                ],
            }

        if count is None:
            single = cls._resolve_ordinal_reference(reference, previous)
            return {
                "matched": bool(single),
                "requestedCount": 1 if single else 0,
                "availableCount": len(previous.get("resultTitleIds") or []),
                "items": [single] if single else [],
            }

        ids = [str(item) for item in list(previous.get("resultTitleIds") or []) if item]
        titles = list(previous.get("resultTitles") or [])
        if count < 1:
            return {
                "matched": True,
                "requestedCount": count,
                "availableCount": len(ids),
                "items": [],
                "selectionError": "invalid_batch_count",
            }
        if count > cls.MAX_BATCH_ACTION_TITLES:
            return {
                "matched": True,
                "requestedCount": count,
                "availableCount": len(ids),
                "items": [],
                "selectionError": "batch_limit_exceeded",
            }
        if count > len(ids):
            return {
                "matched": True,
                "requestedCount": count,
                "availableCount": len(ids),
                "items": [],
                "selectionError": "requested_more_than_available",
            }
        return {
            "matched": True,
            "requestedCount": count,
            "availableCount": len(ids),
            "items": [
                {
                    "id": ids[index],
                    "title": titles[index] if index < len(titles) else None,
                }
                for index in range(count)
            ],
        }

    @staticmethod
    def action_to_interaction(action: str) -> str:
        return {
            "like_title": "like",
            "watchlist_add": "watchlist",
            "watchlist_remove": "remove_watchlist",
            "not_interested": "dislike",
            "play": "play",
        }[action]

    @staticmethod
    def render_action_reply(action: str, title: dict[str, Any], decision: dict[str, Any]) -> str:
        title_name = str(title.get("title") or "the title")
        if action == "like_title":
            return f"Added {title_name} to your liked titles and updated your viewer profile."
        if action == "watchlist_add":
            if decision.get("includedInPlan"):
                return f"Added {title_name} to My List. It is included in your current plan."
            return (
                f"Added {title_name} to My List. It is in the catalogue but requires a separate "
                f"{decision.get('offerType') or 'purchase'}."
            )
        if action == "watchlist_remove":
            return f"Removed {title_name} from My List."
        if action == "not_interested":
            return f"Got it. I marked {title_name} as Not Interested and removed it from future recommendations."
        if action == "play":
            progress = decision.get("resumeProgressPct")
            if progress:
                return f"Resuming {title_name} from {round(float(progress))}% on this device."
            return f"Starting {title_name}. It is included in your current plan."
        return f"Updated {title_name}."

    @staticmethod
    def _join_title_names(values: list[str]) -> str:
        names = [str(value).strip() for value in values if str(value).strip()]
        if not names:
            return "the selected titles"
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + f" and {names[-1]}"

    @classmethod
    def render_batch_action_reply(
        cls, action: str, outcomes: list[dict[str, Any]]
    ) -> str:
        completed = [item for item in outcomes if item.get("status") == "completed"]
        failed = [item for item in outcomes if item.get("status") != "completed"]
        completed_names = cls._join_title_names(
            [str(item.get("title") or "") for item in completed]
        )
        failed_names = cls._join_title_names(
            [str(item.get("title") or "") for item in failed]
        )
        success_templates = {
            "like_title": f"Added {completed_names} to your liked titles and updated your viewer profile.",
            "watchlist_add": f"Added {completed_names} to My List.",
            "watchlist_remove": f"Removed {completed_names} from My List.",
            "not_interested": (
                f"Marked {completed_names} as Not Interested and removed them from future recommendations."
            ),
        }
        parts: list[str] = []
        if completed:
            parts.append(success_templates.get(action, f"Updated {completed_names}."))
        if failed:
            details = "; ".join(
                f"{item.get('title') or 'selected title'}: "
                f"{item.get('reason') or 'the governed update did not complete'}"
                for item in failed
            )
            if completed:
                parts.append(
                    f"I could not complete the same action for {failed_names}. {details}."
                )
            else:
                parts.append(
                    f"I could not complete the action for {failed_names}. {details}."
                )
        return " ".join(parts)
