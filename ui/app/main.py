from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .agent_catalog_gateway import AgentCatalogGateway
from .agent_service import AgentTrace, GovernedAgentService
from .catalogue_service import CatalogueService
from .capella_ai_services import CapellaAIServices
from .config import build_ui_experience_config, settings
from .llm_service import LocalModelService
from .memory_service import AgentMemoryService
from .mcp_gateway import MCPGateway
from .planner_service import AssistantPlannerService
from .showcase_service import ShowcaseService
from .schemas import (
    CatalogueSearchRequest,
    ChatRequest,
    InteractionRequest,
    LoginRequest,
    MemorySearchRequest,
    NewSessionRequest,
    PreferenceUpdateRequest,
    RegisterViewerRequest,
    ShowcasePersonaRequest,
    ShowcaseFaultRequest,
    SearchLabRequest,
    RecommendationEvidenceRequest,
    EntitlementSimulationRequest,
)


memory_service: AgentMemoryService | None = None
model_service: LocalModelService | None = None
catalogue_service: CatalogueService | None = None
agent_service: GovernedAgentService | None = None
mcp_gateway: MCPGateway | None = None
agent_catalog_gateway: AgentCatalogGateway | None = None
capella_ai_services: CapellaAIServices | None = None
showcase_service: ShowcaseService | None = None
planner_service: AssistantPlannerService | None = None
startup_error: str | None = None
startup_started_at: float = time.time()
startup_task: asyncio.Task[Any] | None = None
background_tasks: set[asyncio.Task[Any]] = set()
background_persistence_lock = asyncio.Lock()
VERSION = settings.app_version
memory_trial_task: asyncio.Task | None = None
memory_trial_progress = ""


async def _initialise_services() -> None:
    """Initialise blocking SDK clients after the web server is already serving.

    This makes the login screen appear immediately instead of waiting for
    Couchbase, Agent Memory, Search and configured model readiness checks.
    """
    global memory_service, model_service, catalogue_service, agent_service
    global mcp_gateway, agent_catalog_gateway, capella_ai_services, showcase_service
    global planner_service, startup_error
    try:
        model = LocalModelService(settings)
        memory = await asyncio.to_thread(AgentMemoryService, settings)
        catalogue = await asyncio.to_thread(CatalogueService, settings)
        planner = AssistantPlannerService(settings, catalogue, model)
        mcp = MCPGateway(settings)
        await mcp.start()
        catalog_gateway = await asyncio.to_thread(
            AgentCatalogGateway,
            settings,
            list(GovernedAgentService.CATALOG_MCP_TOOL_NAMES),
        )
        agent = await asyncio.to_thread(
            GovernedAgentService,
            catalogue,
            agent_catalog_gateway=catalog_gateway,
            mcp_gateway=mcp,
        )
        capella_ai = CapellaAIServices(settings, catalogue.cluster)
        showcase = (
            ShowcaseService(settings, catalogue, catalog_gateway, capella_ai)
            if settings.showcase_enabled
            else None
        )
        model_service = model
        memory_service = memory
        catalogue_service = catalogue
        mcp_gateway = mcp
        agent_catalog_gateway = catalog_gateway
        agent_service = agent
        capella_ai_services = capella_ai
        showcase_service = showcase
        planner_service = planner
    except Exception as exc:
        startup_error = f"{type(exc).__name__}: {exc}"


@asynccontextmanager
async def lifespan(_: FastAPI):
    global startup_task
    startup_task = asyncio.create_task(_initialise_services())
    try:
        yield
    finally:
        if startup_task and not startup_task.done():
            startup_task.cancel()
        pending = list(background_tasks)
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=12.0,
                )
            except asyncio.TimeoutError:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        if mcp_gateway is not None:
            await mcp_gateway.close()
        if catalogue_service is not None:
            catalogue_service.close()
        if model_service is not None:
            model_service.close()
        if memory_service is not None:
            memory_service.close()


app = FastAPI(title=settings.app_title, version=VERSION, lifespan=lifespan)


@app.middleware("http")
async def isolate_memory_comparison(request, call_next):
    if (memory_trial_task and not memory_trial_task.done() and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not request.url.path.startswith("/api/metrics/memory-trial")):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={"detail": "Memory comparison is running. Try again when it finishes."})
    return await call_next(request)
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _ready() -> bool:
    return (
        memory_service is not None
        and model_service is not None
        and catalogue_service is not None
        and agent_service is not None
        and planner_service is not None
    )


async def _services_wait() -> tuple[AgentMemoryService, LocalModelService, CatalogueService]:
    deadline = time.monotonic() + settings.service_startup_timeout_seconds
    while not _ready() and startup_error is None and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    if startup_error:
        raise HTTPException(status_code=503, detail=f"StreamAI startup failed: {startup_error}")
    if not _ready():
        raise HTTPException(status_code=503, detail="StreamAI services are still starting.")
    assert memory_service is not None and model_service is not None and catalogue_service is not None
    assert agent_service is not None
    return memory_service, model_service, catalogue_service


async def _call(func, *args, **kwargs):
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


async def _agent_tool_call(
    agent: GovernedAgentService,
    trace: AgentTrace,
    name: str,
    func: Any,
    *args: Any,
    inputs: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    safe_inputs = inputs or {}
    catalog_started = time.perf_counter()
    simulate_mcp_fault = bool(showcase_service is not None and showcase_service.faults().get("mcp_unavailable"))
    try:
        if simulate_mcp_fault:
            raise RuntimeError("Presenter-injected MCP outage; using governed direct-SDK fallback")
        native = await agent.try_catalog_tool(
            trace,
            name=name,
            inputs=safe_inputs,
            fallback_callable=func,
        )
        if native.get("used"):
            agent.record_tool(
                trace,
                name=name,
                started=catalog_started,
                status="completed",
                inputs=safe_inputs,
                output=native.get("result"),
                execution_source="agent_catalog_mcp",
                catalog_id=native.get("catalogId"),
            )
            if not native.get("fallback"):
                return native.get("result")
    except Exception as exc:
        agent.record_tool(
            trace,
            name=name,
            started=catalog_started,
            status="failed",
            inputs=safe_inputs,
            error=f"{type(exc).__name__}: {exc}",
            execution_source="agent_catalog_mcp",
            catalog_id=(
                agent.agent_catalog_gateway.catalog_id
                if agent.agent_catalog_gateway is not None
                else None
            ),
        )
        required = bool(
            agent.agent_catalog_gateway
            and agent.agent_catalog_gateway.required
            and agent.mcp_gateway
            and agent.mcp_gateway.required
        )
        if required and not simulate_mcp_fault:
            raise

    # Direct SDK execution is retained for write actions, Search/Vector paths,
    # or an explicitly configured degraded fallback. Exact title resolution also
    # falls back to FTS only after the MCP SQL++ exact check returned no match.
    started = time.perf_counter()
    try:
        result = await _call(func, *args, **kwargs)
        agent.record_tool(
            trace,
            name=name,
            started=started,
            status="completed",
            inputs=safe_inputs,
            output=result,
            execution_source="application_sdk",
        )
        return result
    except Exception as exc:
        agent.record_tool(
            trace,
            name=name,
            started=started,
            status="failed",
            inputs=safe_inputs,
            error=f"{type(exc).__name__}: {exc}",
            execution_source="application_sdk",
        )
        raise


def _line(event_type: str, **payload: Any) -> bytes:
    return (json.dumps({"type": event_type, **payload}, ensure_ascii=False) + "\n").encode("utf-8")


def _login_id(memory: AgentMemoryService) -> str:
    login_id = memory.snapshot(include_memories=False).get("login_id")
    if not login_id:
        raise HTTPException(status_code=401, detail="Sign in to a viewer profile first.")
    return str(login_id)


async def _record_metric_safe(
    catalogue: CatalogueService, login_id: str, event: dict[str, Any]
) -> dict[str, Any] | None:
    """Persist demo value telemetry without ever failing the user operation."""
    try:
        return await asyncio.to_thread(catalogue.record_ai_metric, login_id, event)
    except Exception:
        return None


def _spawn_background(coro: Any) -> None:
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def _persist_chat_artifacts(
    *,
    memory: AgentMemoryService,
    catalogue: CatalogueService,
    login_id: str,
    target: Any,
    user_message: str,
    assistant_message: str,
    facts: list[str],
    metric_event: dict[str, Any],
    sync_profile: bool = False,
) -> None:
    """Persist Agent Memory and telemetry outside the visible chat path."""
    started = time.perf_counter()
    persistence_error: str | None = None
    # Preserve per-session turn order and avoid concurrent calls into the same
    # Agent Memory session while keeping the work off the response path.
    async with background_persistence_lock:
        if sync_profile:
            try:
                result = await _sync_profile_memory(memory, catalogue, login_id)
                if result.get("status") in {"ready", "pending", "empty"}:
                    facts = []  # The idempotent mirror owns these explicit facts.
                    sync_profile = False
            except Exception:
                pass  # Preserve the conversational fact write on mirror failure.
        try:
            if showcase_service is not None and showcase_service.faults().get("agent_memory_unavailable"):
                raise RuntimeError("Presenter-injected Agent Memory outage")
            await asyncio.to_thread(
                memory.persist_turn,
                target=target,
                user_message=user_message,
                assistant_message=assistant_message,
                facts=facts,
            )
        except Exception as exc:
            persistence_error = f"{type(exc).__name__}: {exc}"
        if sync_profile:
            try:
                await _sync_profile_memory(memory, catalogue, login_id)
            except Exception as exc:
                persistence_error = persistence_error or f"{type(exc).__name__}: {exc}"
    persistence_ms = round((time.perf_counter() - started) * 1000, 1)
    # The completed chat observation is recorded before background persistence.
    # Actual Memory HTTP attempts are measured by the shared usage gateway.
    from . import usage_reporter
    await asyncio.to_thread(usage_reporter.record, login_id, {
        "category": "memory_submission", "label": "conversation_and_facts",
        "success": persistence_error is None, "durationMs": persistence_ms,
        "errorType": persistence_error.split(":", 1)[0] if persistence_error else None,
    })


async def _sync_profile_memory(memory: AgentMemoryService, catalogue: CatalogueService,
                               login_id: str, *, apply: bool = True) -> dict[str, Any]:
    blocks = await _call(catalogue.profile_memory_blocks, login_id)
    return await _call(memory.sync_operational_profile, [b["fact"] for b in blocks],
                       apply=apply, expected_login_id=login_id)


def _merge_memory_blocks(data: dict[str, Any], structured: list[dict[str, Any]]) -> dict[str, Any]:
    long_term = list(data.get("long_term") or [])
    for block in structured:
        if not any(item.get("fact") == block.get("fact") for item in long_term):
            long_term.append(block)
    short_term = list(data.get("short_term") or [])
    other = list(data.get("other") or [])
    all_blocks = [*short_term, *long_term, *other]
    data["long_term"] = long_term
    data["all"] = all_blocks
    data["counts"] = {
        **(data.get("counts") or {}),
        "total": len(all_blocks),
        "short_term": len(short_term),
        "long_term": len(long_term),
    }
    data["view"] = "agent_memory_plus_structured_viewer_profile"
    return data


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "healthy" if _ready() else "starting",
        "version": VERSION,
        "app": settings.app_title,
        "ready": _ready(),
        "startup_error": startup_error,
    }


@app.get("/api/ui-config")
async def ui_config() -> dict[str, Any]:
    """Expose non-sensitive presentation defaults before services are ready."""
    return {
        "ok": True,
        "data": build_ui_experience_config(
            settings.ui_experience_mode,
            switch_enabled=settings.ui_experience_switch_enabled,
            showcase_enabled=settings.showcase_enabled,
        ),
    }


@app.get("/api/readiness")
async def readiness() -> dict[str, Any]:
    return {
        "ready": _ready(),
        "state": "ready" if _ready() else ("failed" if startup_error else "starting"),
        "error": startup_error,
        "elapsedMs": round((time.time() - startup_started_at) * 1000, 1),
    }


@app.get("/api/status")
async def status() -> dict[str, Any]:
    if not _ready():
        return {
            "starting": True,
            "ready": False,
            "error": startup_error,
            "services": {},
            "data_plane": {"active": [], "local_adapters": [], "next": []},
        }
    memory, model, catalogue = await _services_wait()
    from . import usage_reporter
    memory_health, model_health, state_health, catalogue_health, usage_health = await asyncio.gather(
        _call(memory.health),
        _call(model.health),
        _call(memory.state_store_health),
        _call(catalogue.health),
        _call(usage_reporter.health),
    )
    return {
        "starting": False,
        "ready": True,
        "services": {
            "data_kv": {"healthy": bool(catalogue_health.get("healthy")), "detail": "Catalogue, viewer profiles, entitlements, watch history and governed actions"},
            "query_index": {"healthy": bool(catalogue_health.get("healthy")), "detail": "Structured filters, operational rows and trace inspection"},
            "search_fts_vector": {"healthy": bool(catalogue_health.get("search_healthy")), "index": catalogue_health.get("search_index"), "detail": "Field-aware lexical, semantic and hybrid retrieval"},
            "agent_memory": memory_health,
            "agent_catalog": {
                "healthy": bool(agent_catalog_gateway and agent_catalog_gateway.healthy),
                "detail": "Native published prompts and MCP-backed tools",
                "catalogId": agent_catalog_gateway.catalog_id if agent_catalog_gateway else None,
            },
            "mcp_server": (mcp_gateway.snapshot() if mcp_gateway else {"healthy": False}),
            "governed_agent": {"healthy": True, "detail": "Agent Catalog tool resolution, policy checks and action orchestration"},
            "agent_tracer": {
                "healthy": bool(agent_catalog_gateway and agent_catalog_gateway.healthy and settings.native_agent_tracing_enabled and not agent_catalog_gateway.native_trace_error),
                "detail": "Native Agent Catalog spans plus Couchbase operational trace projection",
                "error": agent_catalog_gateway.native_trace_error if agent_catalog_gateway else None,
            },
            "model_runtime": model_health,
            "usage_meter": usage_health,
            "ai_functions": (
                capella_ai_services.snapshot()["aiFunctions"]
                if capella_ai_services is not None
                else {"enabled": False, "healthy": False}
            ),
            "data_processing": (
                capella_ai_services.snapshot()["dataProcessing"]
                if capella_ai_services is not None
                else {"enabled": False, "healthy": False}
            ),
            "viewer_state": state_health,
            "showcase_studio": {"enabled": bool(showcase_service), "healthy": bool(showcase_service), "detail": "Presenter mode, search lab, governance, resilience and evaluation"},
        },
        "catalogue": catalogue_health,
        "viewer": memory.snapshot(include_memories=False),
        "data_plane": {
            "active": ["Governed Assistant Planner", "Semantic Plan Cache", "Agent Memory", "Agent Catalog", "Agent Tracer", "Couchbase MCP Server", "Data Service / KV", "Query Service", "Index Service", "Search Service / FTS", "Search Service / Vector Search", "Governed Actions", "Entitlement Policy", "AI Value Telemetry", "Showcase Studio", "Evaluation Dashboard"],
            "conditional": ["Capella Model Service semantic cache", "AI Functions enrichment", "Capella Data Processing workflow"],
            "local_adapters": ["Validated write actions remain inside the StreamAI business service", "Configured chat and embedding services", "Operational trace projection supplements native Agent Tracer"],
            "next": ["Eventing adaptation", "Analytics closed loop"],
        },
    }


@app.get("/api/agent/catalog")
async def agent_catalog_snapshot() -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    login_id = _login_id(memory)
    assert agent_service is not None
    entitlement = await _call(catalogue.get_entitlement, login_id)
    return {
        "ok": True,
        "data": {
            **agent_service.catalog_snapshot(),
            "viewerEntitlement": entitlement,
            "agentCatalogMode": settings.agent_catalog_mode,
            "mcpMode": settings.mcp_mode,
        },
    }


@app.get("/api/planner/cache")
async def assistant_plan_cache() -> dict[str, Any]:
    _, _, catalogue = await _services_wait()
    return {"ok": True, "data": await _call(catalogue.plan_cache_status)}


@app.post("/api/agent/mcp/schema")
async def agent_mcp_schema_proof() -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    login_id = _login_id(memory)
    assert agent_service is not None
    gateway = agent_service.agent_catalog_gateway
    if gateway is None or not gateway.healthy:
        raise HTTPException(status_code=503, detail="Native Agent Catalog is unavailable")
    trace = agent_service.new_trace(
        viewer_id=login_id,
        session_id=str(memory.snapshot(include_memories=False).get("session_id") or ""),
        user_message="Presenter requested MCP schema proof",
    )
    agent_service.set_plan(
        trace,
        intent="operator_mcp_schema_inspection",
        tools=["inspect_streamai_schema"],
        entities={"transport": "streamable_http"},
    )
    started = time.perf_counter()
    try:
        runtime_executor = (
            agent_service.mcp_gateway.call_business_tool
            if agent_service.mcp_gateway is not None and agent_service.mcp_gateway.healthy
            else None
        )
        execution = await gateway.execute_tool(
            name="inspect_streamai_schema",
            inputs={},
            root_span=getattr(trace, "_native_span", None),
            trace_id=trace.trace_id,
            runtime_executor=runtime_executor,
        )
        agent_service.record_tool(
            trace,
            name="inspect_streamai_schema",
            started=started,
            status="completed",
            inputs={},
            output=execution.result,
            execution_source="agent_catalog_mcp",
            catalog_id=execution.catalog_id,
        )
        response_ms = round((time.perf_counter() - started) * 1000, 1)
        document = agent_service.finish_trace(
            trace,
            response_mode="operator_mcp_schema_proof",
            assistant_response="Agent Catalog invoked the schema inspection tool through Couchbase MCP Server.",
            grounded_title_ids=[],
            llm_invoked=False,
            response_ms=response_ms,
            policy={"readOnlyMcp": True, "operatorProof": True},
        )
        return {"ok": True, "data": {"schema": execution.result, "trace": document}}
    except Exception as exc:
        agent_service.fail_trace(trace, exc)
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@app.get("/api/ai-functions/title/{title_id:path}")
async def ai_functions_title(title_id: str) -> dict[str, Any]:
    await _services_wait()
    if capella_ai_services is None:
        raise HTTPException(status_code=503, detail="Capella AI Services integration is unavailable")
    return {"ok": True, "data": await _call(capella_ai_services.viewing_guide, title_id)}


@app.post("/api/ai-functions/enrich/{title_id:path}")
async def ai_functions_enrich(title_id: str, refresh: bool = False) -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    if capella_ai_services is None:
        raise HTTPException(status_code=503, detail="Capella AI Services integration is unavailable")
    result = await _call(capella_ai_services.enrich_title, title_id, refresh=refresh, viewer_id=_login_id(memory))
    return {"ok": True, "data": result}


@app.get("/api/data-processing/status")
async def data_processing_status() -> dict[str, Any]:
    await _services_wait()
    if capella_ai_services is None:
        raise HTTPException(status_code=503, detail="Data Processing integration is unavailable")
    result = await _call(capella_ai_services.data_processing_status)
    return {"ok": True, "data": result}


@app.get("/api/agent/traces")
async def agent_traces(limit: int = 20) -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    login_id = _login_id(memory)
    traces = await _call(catalogue.list_agent_traces, login_id, limit)
    return {"ok": True, "data": {"traces": traces, "count": len(traces)}}


@app.get("/api/agent/traces/{trace_id:path}")
async def agent_trace_detail(trace_id: str) -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    login_id = _login_id(memory)
    trace = await _call(catalogue.get_agent_trace, login_id, trace_id)
    return {"ok": True, "data": trace}


@app.get("/api/agent/sessions/{session_id:path}")
async def agent_session_thread(session_id: str, limit: int = 100) -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    login_id = _login_id(memory)
    traces = await _call(catalogue.list_agent_session_traces, login_id, session_id, limit)
    if not traces:
        raise HTTPException(status_code=404, detail="No Agent Tracer activity was found for this session.")
    return {"ok": True, "data": GovernedAgentService.build_session_thread(traces)}


@app.get("/api/entitlement")
async def entitlement() -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    login_id = _login_id(memory)
    return {"ok": True, "data": await _call(catalogue.get_entitlement, login_id)}


@app.get("/api/showcase")
async def showcase_snapshot() -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    _login_id(memory)
    if showcase_service is None:
        raise HTTPException(status_code=503, detail="Showcase Studio is unavailable")
    return {"ok": True, "data": showcase_service.snapshot()}


@app.post("/api/showcase/persona")
async def showcase_persona(request: ShowcasePersonaRequest) -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    login_id = _login_id(memory)
    if showcase_service is None:
        raise HTTPException(status_code=503, detail="Showcase Studio is unavailable")
    result = await _call(showcase_service.seed_persona, login_id, request.persona_id)
    result["memorySync"] = await _sync_profile_memory(memory, catalogue, login_id)
    return {"ok": True, "data": result}


@app.post("/api/showcase/reset")
async def showcase_reset(request: ShowcasePersonaRequest) -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    login_id = _login_id(memory)
    if showcase_service is None:
        raise HTTPException(status_code=503, detail="Showcase Studio is unavailable")
    result = await _call(showcase_service.reset_demo, login_id, request.persona_id)
    result["memorySync"] = await _sync_profile_memory(memory, catalogue, login_id)
    return {"ok": True, "data": result}


@app.get("/api/showcase/faults")
async def showcase_faults() -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    _login_id(memory)
    if showcase_service is None:
        raise HTTPException(status_code=503, detail="Showcase Studio is unavailable")
    return {"ok": True, "data": showcase_service.faults()}


@app.put("/api/showcase/faults")
async def showcase_set_faults(request: ShowcaseFaultRequest) -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    login_id = _login_id(memory)
    if showcase_service is None:
        raise HTTPException(status_code=503, detail="Showcase Studio is unavailable")
    return {"ok": True, "data": await _call(showcase_service.set_faults, login_id, request.model_dump())}


@app.post("/api/showcase/search-lab")
async def showcase_search_lab(request: SearchLabRequest) -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    login_id = _login_id(memory)
    if showcase_service is None:
        raise HTTPException(status_code=503, detail="Showcase Studio is unavailable")
    return {"ok": True, "data": await _call(showcase_service.search_lab, login_id, request.query, request.content_type, request.limit)}


@app.post("/api/showcase/evidence")
async def showcase_evidence(request: RecommendationEvidenceRequest) -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    login_id = _login_id(memory)
    if showcase_service is None:
        raise HTTPException(status_code=503, detail="Showcase Studio is unavailable")
    return {"ok": True, "data": await _call(showcase_service.recommendation_evidence, login_id, request.title_id, request.item)}


@app.post("/api/showcase/entitlement-simulate")
async def showcase_entitlement_simulate(request: EntitlementSimulationRequest) -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    login_id = _login_id(memory)
    if showcase_service is None:
        raise HTTPException(status_code=503, detail="Showcase Studio is unavailable")
    scenario = {"region": request.region, "tier": request.tier, "parentalRating": request.parental_rating, "deviceType": request.device_type, "roamingAllowed": request.roaming_allowed}
    return {"ok": True, "data": await _call(showcase_service.simulate_entitlement, login_id, request.title_id, scenario)}


@app.get("/api/showcase/governance")
async def showcase_governance() -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    _login_id(memory)
    if showcase_service is None:
        raise HTTPException(status_code=503, detail="Showcase Studio is unavailable")
    return {"ok": True, "data": await _call(showcase_service.governance)}


@app.post("/api/showcase/evaluate")
async def showcase_evaluate() -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    login_id = _login_id(memory)
    if showcase_service is None:
        raise HTTPException(status_code=503, detail="Showcase Studio is unavailable")
    return {"ok": True, "data": await _call(showcase_service.run_evaluation, login_id)}


@app.get("/api/showcase/profile-timeline")
async def showcase_profile_timeline(limit: int = 30) -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    login_id = _login_id(memory)
    if showcase_service is None:
        raise HTTPException(status_code=503, detail="Showcase Studio is unavailable")
    return {"ok": True, "data": await _call(showcase_service.profile_timeline, login_id, limit)}


@app.get("/api/showcase/documents")
async def showcase_documents(title_id: str | None = None, trace_id: str | None = None) -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    login_id = _login_id(memory)
    if showcase_service is None:
        raise HTTPException(status_code=503, detail="Showcase Studio is unavailable")
    return {"ok": True, "data": await _call(showcase_service.supporting_documents, login_id, title_id, trace_id)}


@app.get("/api/showcase/capella")
async def showcase_capella() -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    _login_id(memory)
    if showcase_service is None:
        raise HTTPException(status_code=503, detail="Showcase Studio is unavailable")
    return {"ok": True, "data": showcase_service.capella_profile()}


@app.post("/api/auth/register")
async def register_viewer(request: RegisterViewerRequest) -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    try:
        data = await _call(memory.register_viewer, name=request.name, login_id=request.login_id, pin=request.pin)
        await _call(catalogue.ensure_profile, request.login_id.lower(), request.name)
        data["profile_memory_sync"] = await _sync_profile_memory(memory, catalogue, _login_id(memory))
        return {"ok": True, "data": data}
    except HTTPException as exc:
        if "ValueError" in str(exc.detail):
            raise HTTPException(status_code=409, detail=str(exc.detail).split(": ", 1)[-1]) from exc
        raise


@app.post("/api/auth/login")
async def login_viewer(request: LoginRequest) -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    try:
        data = await _call(memory.login, login_id=request.login_id, pin=request.pin)
        await _call(catalogue.ensure_profile, request.login_id.lower(), data.get("user_name") or request.login_id)
        data["profile_memory_sync"] = await _sync_profile_memory(memory, catalogue, _login_id(memory))
        return {"ok": True, "data": data}
    except HTTPException as exc:
        if "ValueError" in str(exc.detail):
            raise HTTPException(status_code=401, detail="Unknown login ID or PIN.") from exc
        raise


@app.post("/api/auth/logout")
async def logout_viewer() -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    return {"ok": True, "data": await _call(memory.logout)}


@app.get("/api/home/shell")
async def home_shell() -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    return {"ok": True, "data": await _call(catalogue.home_shell, _login_id(memory))}


@app.get("/api/home/row/{row_id}")
async def home_row(row_id: str) -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    return {"ok": True, "data": await _call(catalogue.home_row, _login_id(memory), row_id)}


@app.get("/api/home")
async def home() -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    return {"ok": True, "data": await _call(catalogue.home, _login_id(memory))}


@app.post("/api/session/new")
async def new_session(request: NewSessionRequest) -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    return {"ok": True, "data": await _call(memory.start_new_session, request.label)}


@app.post("/api/session/end")
async def end_session() -> dict[str, Any]:
    memory, _, _ = await _services_wait()
    return {"ok": True, "data": await _call(memory.end_session)}


@app.post("/api/catalogue/search")
async def catalogue_search(request: CatalogueSearchRequest) -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    login_id = _login_id(memory)
    started = time.perf_counter()
    result = await _call(
        catalogue.search,
        query=request.query,
        mode=request.mode,
        login_id=login_id,
        content_type=request.content_type,
        limit=request.limit,
        purpose="search",
    )
    await _record_metric_safe(
        catalogue,
        login_id,
        {
            "category": "catalogue_search",
            "label": f"{request.mode}_search",
            "interactions": 1,
            "catalogueSearches": 1,
            "responseMs": (time.perf_counter() - started) * 1000,
            "details": {
                "query": request.query[:160],
                "mode": request.mode,
                "returnedCount": len(result.get("results") or []),
            },
        },
    )
    return {"ok": True, "data": result}


@app.get("/api/catalogue/title/{title_id:path}")
async def title_detail(title_id: str) -> dict[str, Any]:
    _, _, catalogue = await _services_wait()
    return {"ok": True, "data": await _call(catalogue.title, title_id)}


@app.post("/api/interactions")
async def interaction(request: InteractionRequest) -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    login_id = _login_id(memory)
    result = await _call(
        catalogue.record_interaction,
        login_id=login_id,
        title_id=request.title_id,
        action=request.action,
        progress_pct=request.progress_pct,
    )
    memory_error = None
    facts = list(result.get("memoryFacts") or [])
    if facts:
        try:
            await _call(memory.record_external_facts, facts, f"title_{request.action}_interaction")
        except HTTPException as exc:
            memory_error = str(exc.detail)
    metrics = await _record_metric_safe(
        catalogue,
        login_id,
        {
            "category": "viewer_interaction",
            "label": request.action,
            "interactions": 1,
            "agentMemoryWrites": 1 if facts and memory_error is None else 0,
            "memoryFactsWritten": len(facts) if memory_error is None else 0,
            "details": {
                "titleId": request.title_id,
                "title": (result.get("title") or {}).get("title"),
                "action": request.action,
            },
        },
    )
    return {
        "ok": True,
        "data": result,
        "profileChanged": bool(facts),
        "memoryError": memory_error,
        "metrics": metrics,
    }


@app.get("/api/history")
async def watch_history(content_type: str | None = None, limit: int = 30) -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    if content_type not in {None, "movie", "tv"}:
        raise HTTPException(status_code=400, detail="content_type must be 'movie' or 'tv'.")
    items = await _call(
        catalogue.list_watch_history,
        login_id=_login_id(memory),
        content_type=content_type,
        limit=min(max(limit, 1), 100),
    )
    return {"ok": True, "data": {"items": items, "count": len(items)}}


@app.get("/api/profile")
async def profile() -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    viewer = _login_id(memory)
    data = await _call(catalogue.profile_view, viewer)
    data["memorySync"] = await _sync_profile_memory(memory, catalogue, viewer, apply=False)
    return {"ok": True, "data": data}


@app.post("/api/profile/memory-sync")
async def synchronise_profile_memory() -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    return {"ok": True, "data": await _sync_profile_memory(memory, catalogue, _login_id(memory))}


@app.get("/api/metrics")
async def ai_metrics() -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    return {"ok": True, "data": await _call(catalogue.get_ai_metrics, _login_id(memory))}


@app.put("/api/metrics/comparison")
async def update_comparison_assumptions(values: dict[str, Any]) -> dict[str, Any]:
    from . import usage_reporter
    memory, _, catalogue = await _services_wait()
    viewer = _login_id(memory)
    await _call(usage_reporter.set_comparison_assumptions, values)
    return {"ok": True, "data": await _call(catalogue.get_ai_metrics, viewer)}


@app.post("/api/metrics/reset")
async def reset_ai_metrics() -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    return {"ok": True, "data": await _call(catalogue.reset_ai_metrics, _login_id(memory))}


@app.post("/api/metrics/memory-trial")
async def start_memory_comparison() -> dict[str, Any]:
    from . import memory_savings
    global memory_trial_task, memory_trial_progress
    memory, _, _ = await _services_wait()
    _login_id(memory)
    if memory_trial_task and not memory_trial_task.done():
        raise HTTPException(409, "A memory comparison is already running.")
    # Drain submitted app writes first. No reset or user-data deletion is involved.
    if background_tasks or memory.state.persistence_pending:
        raise HTTPException(409, "Memory work is still finishing. Wait a moment, then retry.")
    snapshot = await _call(memory.snapshot)
    snapshot["facts"] = list(memory.state.long_term_profile)
    try:
        messages, facts = memory_savings.workload(snapshot)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    def progress(value):
        global memory_trial_progress
        memory_trial_progress = value
    async def execute():
        try:
            outcome = await asyncio.to_thread(memory_savings.run, settings, messages, facts,
                                              memory._blocks_from_result, progress)
            progress("Comparison finished" if outcome["success"] else outcome["error"])
        except Exception as exc:
            progress(type(exc).__name__ + ": comparison unavailable. Check the usage gateway and Memory service.")
    memory_trial_progress = "Starting measured comparison"
    memory_trial_task = asyncio.create_task(execute())
    return {"ok": True, "turns": len(messages), "facts": len(facts)}


@app.get("/api/metrics/memory-trial/status")
async def memory_comparison_status() -> dict[str, Any]:
    return {"running": bool(memory_trial_task and not memory_trial_task.done()), "progress": memory_trial_progress}


@app.put("/api/profile/preferences")
async def update_preferences(request: PreferenceUpdateRequest) -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    login_id = _login_id(memory)
    values = request.model_dump()
    profile_doc = await _call(catalogue.replace_preferences, login_id, values)
    memory_sync = await _sync_profile_memory(memory, catalogue, login_id)
    memory_error = memory_sync.get("message") if memory_sync.get("status") == "error" else None
    metrics = await _record_metric_safe(
        catalogue,
        login_id,
        {
            "category": "profile_update",
            "label": "profile_editor",
            "interactions": 1,
            "details": {"preferenceFacts": memory_sync.get("expectedFacts")},
        },
    )
    return {"ok": True, "data": profile_doc, "memoryError": memory_error, "memorySync": memory_sync, "metrics": metrics}


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    memory, model, catalogue = await _services_wait()

    async def generate():
        started = time.perf_counter()
        agent_trace: AgentTrace | None = None
        observation_recorded = False
        try:
            login_id = _login_id(memory)
            assert agent_service is not None
            agent = agent_service
            context, target = await _call(memory.prepare_turn, request.message)
            agent_trace = agent.new_trace(
                viewer_id=login_id,
                session_id=target.session_id,
                user_message=request.message,
            )
            previous_catalogue_request = await _call(memory.get_last_catalogue_request)
            action_request = agent.extract_action_request(
                request.message, previous_catalogue_request
            )
            contextual_request = model.extract_contextual_request(request.message)
            explanation_question = model.is_recommendation_explanation_question(request.message)
            catalogue_complaint = model.is_catalogue_result_complaint(request.message)
            likes_dislikes_question = model.is_likes_dislikes_question(request.message)
            profile_preference_question = model.extract_profile_preference_question(request.message)
            profile_question = bool(profile_preference_question or model.is_profile_question(request.message))
            recommendation_clause = model.extract_recommendation_clause(request.message)
            catalogue_query = recommendation_clause or request.message
            catalogue_intent = await _call(catalogue.analyse_request, catalogue_query)
            recommendation_request = model.is_recommendation_request(request.message)
            personalised_recommendation = bool(
                model.is_personalised_recommendation_request(request.message)
                or catalogue_intent.get("personalisedRecommendation")
            )
            region_update = model.extract_region_statement(request.message)
            region_question = model.is_region_question(request.message)
            history_count_question = model.is_watch_history_count_question(request.message)
            catalogue_analytics_question = model.extract_catalogue_analytics_request(
                request.message
            )
            catalogue_count_question = (
                None
                if catalogue_analytics_question
                else model.extract_catalogue_count_question(request.message)
            )
            top_rated_question = model.extract_top_rated_catalogue_question(request.message)
            title_affinity_question = model.extract_title_affinity_question(request.message)
            contextual_similarity_request = model.is_contextual_similarity_request(request.message)
            similar_title_query = model.extract_similar_title_request(request.message)
            if not similar_title_query and contextual_similarity_request:
                previous_titles = list(previous_catalogue_request.get("resultTitles") or [])
                previous_ids = list(previous_catalogue_request.get("resultTitleIds") or [])
                similar_title_query = str(
                    previous_catalogue_request.get("lastGroundedTitle")
                    or (previous_titles[0] if previous_titles else "")
                ).strip() or None
            title_question = None if title_affinity_question else model.extract_catalogue_title_question(request.message)
            contextual_title_reference = (
                agent.resolve_previous_title_reference(
                    str(title_question.get("title") or ""),
                    previous_catalogue_request,
                )
                if title_question
                else None
            )
            direct_title_candidate = model.extract_direct_title_candidate(request.message)
            preferences = (
                model.extract_preferences_fast(request.message)
                if action_request is None
                and request.capture_long_term
                and settings.auto_capture_facts
                and model.should_capture_preference(request.message)
                else []
            )
            facts = [str(item.get("fact")) for item in preferences]
            # Preference declarations take precedence over catalogue nouns.
            # A message such as "I do not like romance movies" must update the
            # viewer profile, not browse Romance merely because the analyser
            # detected a known genre. Only a separate explicit request clause
            # turns the same message into a preference-write + recommendation.
            pure_preference_statement = bool(
                preferences and model.is_preference_only_statement(request.message)
            )
            catalogue_browse_request = bool(
                not pure_preference_statement
                and (
                    recommendation_request
                    or catalogue_intent.get("requestLanguage")
                    or catalogue_intent.get("genres")
                    or catalogue_intent.get("country")
                )
            )
            profile_changed = False
            if preferences:
                agent.set_plan(
                    agent_trace,
                    intent="update_viewer_preference",
                    tools=["update_viewer_preference"],
                    entities={"preferenceCount": len(preferences)},
                )
                await _agent_tool_call(
                    agent,
                    agent_trace,
                    "update_viewer_preference",
                    catalogue.apply_preferences,
                    login_id,
                    preferences,
                    inputs={"viewer_id": login_id, "preferences": preferences},
                )
                profile_changed = True

            catalogue_results: list[dict[str, Any]] = []
            retrieval_trace: dict[str, Any] = {"mode": "not_required"}
            grounded_reply: str | None = None
            response_mode = "model_stream"
            history_changed = False
            planner_trace_data: dict[str, Any] = {}
            planner_usage: dict[str, Any] = {}
            planner_llm_invoked = False
            plan_cache_promotion: dict[str, Any] | None = None

            if region_update:
                agent.set_plan(
                    agent_trace,
                    intent="update_viewer_region",
                    tools=["update_viewer_entitlement"],
                    entities={"regionCode": region_update},
                )
                entitlement = await _agent_tool_call(
                    agent,
                    agent_trace,
                    "update_viewer_entitlement",
                    catalogue.update_entitlement_region,
                    login_id,
                    region_update,
                    inputs={"viewer_id": login_id, "region_code": region_update},
                )
                facts.append(f"Viewer lives in region {region_update}.")
                grounded_reply = model.render_region_update(entitlement)
                response_mode = "viewer_region_update_grounded"
                profile_changed = True
                retrieval_trace = {
                    "mode": "viewer_region_update",
                    "chatIntent": "update_viewer_region",
                    "regionCode": region_update,
                    "services": ["Data/KV", "Agent Memory"],
                }

            if grounded_reply is None and region_question:
                agent.set_plan(
                    agent_trace,
                    intent="viewer_region_query",
                    tools=["get_viewer_context"],
                    entities={},
                )
                entitlement = await _agent_tool_call(
                    agent,
                    agent_trace,
                    "get_viewer_context",
                    catalogue.get_entitlement,
                    login_id,
                    inputs={"viewer_id": login_id, "document": "entitlement"},
                )
                grounded_reply = model.render_region_reply(entitlement)
                response_mode = "viewer_region_grounded"
                retrieval_trace = {
                    "mode": "viewer_region_kv",
                    "chatIntent": "viewer_region_query",
                    "regionCode": entitlement.get("regionCode"),
                    "services": ["Data/KV"],
                }

            if (
                grounded_reply is None
                and contextual_request
                and contextual_request.get("kind")
                in {"previous_question", "previous_answer"}
            ):
                recall_kind = str(contextual_request.get("kind"))
                agent.set_plan(
                    agent_trace,
                    intent=f"conversation_{recall_kind}",
                    tools=["recall_previous_turn"],
                    entities={},
                    deterministic=True,
                )
                recall = await _agent_tool_call(
                    agent,
                    agent_trace,
                    "recall_previous_turn",
                    memory.recall_previous_turn,
                    inputs={"viewer_id": login_id},
                )
                grounded_reply = (
                    model.render_previous_answer(recall)
                    if recall_kind == "previous_answer"
                    else model.render_previous_question(recall)
                )
                response_mode = f"conversation_memory_{recall_kind}_grounded"
                retrieval_trace = {
                    "mode": "conversation_memory_recall",
                    "chatIntent": f"conversation_{recall_kind}",
                    "memorySource": (recall or {}).get("source"),
                    "memoryBlockId": (recall or {}).get("blockId"),
                    "services": ["Agent Memory"],
                }

            if (
                grounded_reply is None
                and contextual_request
                and contextual_request.get("kind") == "repeat_previous_results"
            ):
                previous_ids = [
                    str(item)
                    for item in previous_catalogue_request.get("resultTitleIds", [])
                    if item
                ]
                agent.set_plan(
                    agent_trace,
                    intent="repeat_previous_grounded_results",
                    tools=["filter_grounded_results"] if previous_ids else [],
                    entities={
                        "inputTitleIds": previous_ids,
                        "expandSearch": False,
                    },
                    deterministic=True,
                )
                if previous_ids:
                    result = await _agent_tool_call(
                        agent,
                        agent_trace,
                        "filter_grounded_results",
                        catalogue.filter_grounded_results,
                        login_id=login_id,
                        title_ids=previous_ids,
                        filter_kind="available",
                        inputs={
                            "viewer_id": login_id,
                            "title_ids": previous_ids,
                            "filter": "available",
                        },
                    )
                    catalogue_results = list(result.get("results") or [])
                    retrieval_trace = dict(result.get("trace") or {})
                else:
                    catalogue_results = []
                    retrieval_trace = {
                        "mode": "context_required",
                        "hardConstraint": "previous grounded title IDs required",
                        "expandedBeyondPreviousResults": False,
                    }
                grounded_reply = model.render_previous_results(catalogue_results)
                response_mode = "previous_results_repeated_grounded"

            if (
                grounded_reply is None
                and contextual_request
                and contextual_request.get("kind") == "current_my_list"
            ):
                agent.set_plan(
                    agent_trace,
                    intent="current_my_list",
                    tools=["get_my_list"],
                    entities={"limit": 25},
                    deterministic=True,
                )
                result = await _agent_tool_call(
                    agent,
                    agent_trace,
                    "get_my_list",
                    catalogue.current_my_list,
                    login_id,
                    limit=25,
                    inputs={"viewer_id": login_id, "limit": 25},
                )
                catalogue_results = list(result.get("results") or [])
                grounded_reply = model.render_my_list(catalogue_results)
                response_mode = "my_list_grounded"
                retrieval_trace = dict(result.get("trace") or {})
                await _call(
                    memory.remember_catalogue_request,
                    {
                        "originalQuery": request.message,
                        "route": "current_my_list",
                        "resultTitleIds": [
                            str(item.get("id")) for item in catalogue_results if item.get("id")
                        ],
                        "resultTitles": [
                            str(item.get("title")) for item in catalogue_results if item.get("title")
                        ],
                    },
                )

            if (
                grounded_reply is None
                and contextual_request
                and contextual_request.get("kind") == "recent_my_list_additions"
            ):
                agent.set_plan(
                    agent_trace,
                    intent="recent_my_list_additions",
                    tools=["get_recent_viewer_actions"],
                    entities={"action": "watchlist", "limit": 10},
                    deterministic=True,
                )
                result = await _agent_tool_call(
                    agent,
                    agent_trace,
                    "get_recent_viewer_actions",
                    catalogue.recent_my_list_additions,
                    login_id,
                    limit=10,
                    inputs={
                        "viewer_id": login_id,
                        "action": "watchlist",
                        "limit": 10,
                    },
                )
                catalogue_results = list(result.get("results") or [])
                grounded_reply = model.render_recent_my_list_additions(
                    catalogue_results
                )
                response_mode = "recent_my_list_actions_grounded"
                retrieval_trace = dict(result.get("trace") or {})
                await _call(
                    memory.remember_catalogue_request,
                    {
                        "originalQuery": request.message,
                        "route": "recent_my_list_additions",
                        "resultTitleIds": [
                            str(item.get("id")) for item in catalogue_results if item.get("id")
                        ],
                        "resultTitles": [
                            str(item.get("title")) for item in catalogue_results if item.get("title")
                        ],
                    },
                )

            if (
                grounded_reply is None
                and contextual_request
                and contextual_request.get("kind") == "similar_to_my_list"
            ):
                agent.set_plan(
                    agent_trace,
                    intent="similar_to_my_list",
                    tools=["get_my_list", "find_similar_to_grounded_titles", "check_entitlement"],
                    entities={"seedSource": "My List", "maxSeedTitles": 5},
                    deterministic=True,
                )
                result = await _agent_tool_call(
                    agent,
                    agent_trace,
                    "find_similar_to_grounded_titles",
                    catalogue.similar_to_my_list,
                    login_id,
                    limit=12,
                    inputs={"viewer_id": login_id, "source": "my_list", "limit": 12},
                )
                seeds = list(result.get("seeds") or [])
                catalogue_results = list(result.get("results") or [])
                grounded_reply = model.render_multi_seed_similarity(
                    seeds, catalogue_results, source_label="My List"
                )
                response_mode = "my_list_similarity_grounded"
                retrieval_trace = dict(result.get("trace") or {})
                await _call(
                    memory.remember_catalogue_request,
                    {
                        "originalQuery": request.message,
                        "route": "similar_to_my_list",
                        "resultTitleIds": [
                            str(item.get("id")) for item in catalogue_results if item.get("id")
                        ],
                        "resultTitles": [
                            str(item.get("title")) for item in catalogue_results if item.get("title")
                        ],
                    },
                )

            if (
                grounded_reply is None
                and contextual_request
                and contextual_request.get("kind") == "similar_to_previous_results"
            ):
                previous_ids = [
                    str(item)
                    for item in previous_catalogue_request.get("resultTitleIds", [])
                    if item
                ]
                agent.set_plan(
                    agent_trace,
                    intent="similar_to_previous_results",
                    tools=["find_similar_to_grounded_titles", "check_entitlement"]
                    if previous_ids
                    else [],
                    entities={"seedTitleIds": previous_ids[:5]},
                    deterministic=True,
                )
                if previous_ids:
                    result = await _agent_tool_call(
                        agent,
                        agent_trace,
                        "find_similar_to_grounded_titles",
                        catalogue.similar_to_title_ids,
                        login_id=login_id,
                        title_ids=previous_ids,
                        limit=12,
                        source_label="the previous results",
                        inputs={
                            "viewer_id": login_id,
                            "title_ids": previous_ids[:5],
                            "limit": 12,
                        },
                    )
                    seeds = list(result.get("seeds") or [])
                    catalogue_results = list(result.get("results") or [])
                    grounded_reply = model.render_multi_seed_similarity(
                        seeds,
                        catalogue_results,
                        source_label="the previous results",
                    )
                    retrieval_trace = dict(result.get("trace") or {})
                    await _call(
                        memory.remember_catalogue_request,
                        {
                            "originalQuery": request.message,
                            "route": "similar_to_previous_results",
                            "resultTitleIds": [
                                str(item.get("id"))
                                for item in catalogue_results
                                if item.get("id")
                            ],
                            "resultTitles": [
                                str(item.get("title"))
                                for item in catalogue_results
                                if item.get("title")
                            ],
                        },
                    )
                else:
                    grounded_reply = (
                        "There is no previous grounded result list to use as a "
                        "similarity seed, so I did not run a new search."
                    )
                    retrieval_trace = {
                        "mode": "context_required",
                        "hardConstraint": "previous grounded title IDs required",
                        "returnedCount": 0,
                    }
                response_mode = "previous_results_similarity_grounded"

            if (
                grounded_reply is None
                and contextual_request
                and contextual_request.get("kind") == "filter_previous_results"
            ):
                previous_ids = [
                    str(item)
                    for item in previous_catalogue_request.get("resultTitleIds", [])
                    if item
                ]
                filter_kind = str(contextual_request.get("filter") or "")
                agent.set_plan(
                    agent_trace,
                    intent="filter_previous_grounded_results",
                    tools=["filter_grounded_results"] if previous_ids else [],
                    entities={
                        "filter": filter_kind,
                        "inputTitleIds": previous_ids,
                        "expandSearch": False,
                    },
                    deterministic=True,
                )
                if previous_ids:
                    result = await _agent_tool_call(
                        agent,
                        agent_trace,
                        "filter_grounded_results",
                        catalogue.filter_grounded_results,
                        login_id=login_id,
                        title_ids=previous_ids,
                        filter_kind=filter_kind,
                        inputs={
                            "viewer_id": login_id,
                            "title_ids": previous_ids,
                            "filter": filter_kind,
                        },
                    )
                    catalogue_results = list(result.get("results") or [])
                    retrieval_trace = dict(result.get("trace") or {})
                    await _call(
                        memory.remember_catalogue_request,
                        {
                            "originalQuery": request.message,
                            "route": "contextual_result_filter",
                            "resultTitleIds": [
                                str(item.get("id"))
                                for item in catalogue_results
                                if item.get("id")
                            ],
                            "resultTitles": [
                                str(item.get("title"))
                                for item in catalogue_results
                                if item.get("title")
                            ],
                        },
                    )
                else:
                    catalogue_results = []
                    retrieval_trace = {
                        "mode": "context_required",
                        "filter": filter_kind,
                        "hardConstraint": "previous grounded title IDs required",
                        "expandedBeyondPreviousResults": False,
                    }
                grounded_reply = (
                    model.render_contextual_filter(filter_kind, catalogue_results)
                    if previous_ids
                    else (
                        "There is no previous grounded result list to filter, "
                        "so I did not run a new search."
                    )
                )
                response_mode = "contextual_result_filter_grounded"

            if grounded_reply is None and action_request and not settings.enable_governed_actions:
                agent.set_plan(
                    agent_trace,
                    intent="governed_viewer_action_disabled",
                    tools=[],
                    entities={"requestedAction": action_request.get("action")},
                )
                grounded_reply = "Governed viewer actions are disabled in this environment, so I did not change any data."
                response_mode = "governed_action_disabled"
                retrieval_trace = {
                    "mode": "governed_action",
                    "chatIntent": "governed_viewer_action_disabled",
                    "actionPerformed": False,
                }
            if grounded_reply is None and action_request:
                action = str(action_request.get("action") or "")
                tool_name = {
                    "like_title": "record_like",
                    "watchlist_add": "update_watchlist",
                    "watchlist_remove": "update_watchlist",
                    "not_interested": "record_not_interested",
                    "play": "start_or_resume_playback",
                }.get(action, "update_watchlist")
                resolved_title_ids = [
                    str(item)
                    for item in list(action_request.get("resolvedTitleIds") or [])
                    if item
                ]
                if not resolved_title_ids and action_request.get("resolvedTitleId"):
                    resolved_title_ids = [str(action_request["resolvedTitleId"])]
                selection_mode = str(action_request.get("selectionMode") or "")
                if (
                    selection_mode == "previous_missing_from_watchlist"
                    and resolved_title_ids
                ):
                    current_profile = await _agent_tool_call(
                        agent,
                        agent_trace,
                        "get_viewer_context",
                        catalogue.get_profile,
                        login_id,
                        inputs={
                            "viewer_id": login_id,
                            "document": "profile",
                            "purpose": "subtract_current_watchlist",
                        },
                    )
                    current_watchlist = {
                        str(item)
                        for item in current_profile.get("watchlistTitleIds", [])
                        if item
                    }
                    resolved_title_ids = [
                        title_id
                        for title_id in resolved_title_ids
                        if title_id not in current_watchlist
                    ]
                    action_request["resolvedTitleIds"] = resolved_title_ids
                    action_request["requestedCount"] = len(resolved_title_ids)
                    if not resolved_title_ids:
                        action_request["selectionError"] = "nothing_missing"
                    elif len(resolved_title_ids) > agent.MAX_BATCH_ACTION_TITLES:
                        action_request["selectionError"] = "batch_limit_exceeded"
                        action_request["availableCount"] = len(resolved_title_ids)
                        resolved_title_ids = []
                        action_request["resolvedTitleIds"] = []
                requested_value = action_request.get("requestedCount")
                requested_count = int(
                    requested_value
                    if requested_value is not None
                    else (len(resolved_title_ids) or 1)
                )
                batch_requested = requested_count > 1
                agent.set_plan(
                    agent_trace,
                    intent="governed_viewer_action",
                    tools=["resolve_catalogue_title", "check_entitlement", tool_name],
                    entities={
                        "action": action,
                        "reference": action_request.get("reference"),
                        "referenceSource": action_request.get("source"),
                        "requestedTitleCount": requested_count,
                        "resolvedTitleIds": resolved_title_ids,
                    },
                )
                resolved_titles: list[dict[str, Any]] = []
                outcomes: list[dict[str, Any]] = []
                selection_error = action_request.get("selectionError")
                if resolved_title_ids:
                    for title_id in resolved_title_ids:
                        title_started = time.perf_counter()
                        try:
                            resolved_title = await _call(catalogue.title, title_id)
                            if not resolved_title:
                                raise LookupError("catalogue title not found")
                            resolved_titles.append(dict(resolved_title))
                            agent.record_tool(
                                agent_trace,
                                name="resolve_catalogue_title",
                                started=title_started,
                                status="completed",
                                inputs={
                                    "title_id": title_id,
                                    "source": "previous_results",
                                },
                                output={"match": resolved_title},
                            )
                        except Exception as exc:
                            agent.record_tool(
                                agent_trace,
                                name="resolve_catalogue_title",
                                started=title_started,
                                status="failed",
                                inputs={"title_id": title_id},
                                error=f"{type(exc).__name__}: {exc}",
                            )
                            outcomes.append(
                                {
                                    "titleId": title_id,
                                    "status": "unresolved",
                                    "reason": "the catalogue record is no longer available",
                                }
                            )
                elif selection_error:
                    resolved_titles = []
                else:
                    resolution = await _agent_tool_call(
                        agent,
                        agent_trace,
                        "resolve_catalogue_title",
                        catalogue.resolve_title,
                        str(action_request.get("reference") or ""),
                        inputs={"title": action_request.get("reference")},
                    )
                    resolved_title = resolution.get("match")
                    if resolved_title:
                        resolved_titles = [dict(resolved_title)]

                if selection_error:
                    available_count = int(action_request.get("availableCount") or 0)
                    if selection_error == "batch_limit_exceeded":
                        grounded_reply = (
                            "A single batch action is limited to five grounded titles, "
                            "so I did not change any viewer data."
                        )
                    elif selection_error == "invalid_batch_count":
                        grounded_reply = (
                            "The requested title count must be between one and five, "
                            "so I did not change any viewer data."
                        )
                    elif selection_error == "no_previous_results":
                        grounded_reply = (
                            "There is no previous grounded result list to act on, "
                            "so I did not change any viewer data."
                        )
                    elif selection_error == "nothing_missing":
                        grounded_reply = (
                            "All of the previous grounded titles are already in My List, "
                            "so no update was needed."
                        )
                    else:
                        grounded_reply = (
                            f"The previous grounded results contain only {available_count} "
                            f"title{'s' if available_count != 1 else ''}, so I did not "
                            f"perform the requested {requested_count}-title action."
                        )
                    response_mode = "governed_batch_action_unresolved"
                    retrieval_trace = {
                        "mode": "governed_action",
                        "chatIntent": "governed_viewer_action",
                        "action": action,
                        "requestedTitleCount": requested_count,
                        "availableTitleCount": available_count,
                        "actionPerformed": False,
                        "selectionError": selection_error,
                        "agentTraceId": agent_trace.trace_id,
                    }
                elif not resolved_titles:
                    grounded_reply = (
                        f"I couldn’t resolve ‘{action_request.get('reference') or 'that title'}’ "
                        "to an exact title in the catalogue, so I did not perform the action."
                    )
                    response_mode = "governed_action_unresolved"
                    retrieval_trace = {
                        "mode": "governed_action",
                        "chatIntent": "governed_viewer_action",
                        "action": action,
                        "requestedTitleCount": requested_count,
                        "actionPerformed": False,
                        "agentTraceId": agent_trace.trace_id,
                    }
                else:
                    decisions: list[dict[str, Any]] = []
                    for title in resolved_titles:
                        title_id = str(title.get("id") or "")
                        try:
                            decision = await _agent_tool_call(
                                agent,
                                agent_trace,
                                "check_entitlement",
                                catalogue.check_entitlement,
                                login_id,
                                title,
                                inputs={
                                    "viewer_id": login_id,
                                    "title_id": title_id,
                                },
                            )
                        except Exception:
                            outcomes.append(
                                {
                                    "titleId": title_id,
                                    "title": title.get("title"),
                                    "status": "failed",
                                    "reason": "the policy check did not complete",
                                }
                            )
                            continue
                        decisions.append(decision)
                        title["entitlement"] = decision
                        blocked = (
                            action in {"like_title", "watchlist_add"}
                            and not decision.get("allowed")
                        ) or (
                            action == "play" and not decision.get("includedInPlan")
                        )
                        if blocked:
                            outcomes.append(
                                {
                                    "titleId": title_id,
                                    "title": title.get("title"),
                                    "status": "policy_denied",
                                    "reason": decision.get("reason"),
                                    "decision": decision,
                                }
                            )
                            continue
                        try:
                            interaction = await _agent_tool_call(
                                agent,
                                agent_trace,
                                tool_name,
                                catalogue.record_interaction,
                                login_id=login_id,
                                title_id=title_id,
                                action=agent.action_to_interaction(action),
                                progress_pct=5.0 if action == "play" else None,
                                inputs={
                                    "viewer_id": login_id,
                                    "title_id": title_id,
                                    "operation": action,
                                },
                            )
                        except Exception:
                            outcomes.append(
                                {
                                    "titleId": title_id,
                                    "title": title.get("title"),
                                    "status": "failed",
                                    "reason": "the governed update did not complete",
                                    "decision": decision,
                                }
                            )
                            continue
                        facts.extend(str(x) for x in interaction.get("memoryFacts") or [])
                        outcomes.append(
                            {
                                "titleId": title_id,
                                "title": title.get("title"),
                                "status": "completed",
                                "decision": decision,
                            }
                        )

                    catalogue_results = resolved_titles
                    completed_count = sum(
                        1 for item in outcomes if item.get("status") == "completed"
                    )
                    blocked_count = sum(
                        1 for item in outcomes if item.get("status") == "policy_denied"
                    )
                    if batch_requested:
                        grounded_reply = agent.render_batch_action_reply(action, outcomes)
                        if completed_count == requested_count:
                            response_mode = "governed_batch_action_grounded"
                        elif completed_count:
                            response_mode = "governed_batch_action_partial"
                        elif blocked_count:
                            response_mode = "governed_batch_action_policy_denied"
                        else:
                            response_mode = "governed_batch_action_failed"
                    else:
                        outcome = outcomes[0] if outcomes else {}
                        decision = dict(outcome.get("decision") or {})
                        resolved_title = resolved_titles[0]
                        if outcome.get("status") == "completed":
                            grounded_reply = agent.render_action_reply(
                                action, resolved_title, decision
                            )
                            response_mode = "governed_action_grounded"
                        elif outcome.get("status") == "policy_denied":
                            grounded_reply = (
                                f"I found {resolved_title.get('title')} in the catalogue, "
                                f"but I can’t complete that action for this viewer: "
                                f"{outcome.get('reason')}."
                            )
                            response_mode = "governed_action_policy_denied"
                        else:
                            grounded_reply = (
                                f"I found {resolved_title.get('title')} in the catalogue, "
                                "but the governed update did not complete."
                            )
                            response_mode = "governed_action_failed"
                    history_changed = completed_count > 0 and action == "play"
                    profile_changed = completed_count > 0 and action in {
                        "like_title", "watchlist_add", "watchlist_remove", "not_interested"
                    }
                    retrieval_trace = {
                        "mode": "governed_action",
                        "chatIntent": "governed_viewer_action",
                        "action": action,
                        "requestedTitleCount": requested_count,
                        "resolvedTitleCount": len(resolved_titles),
                        "completedTitleCount": completed_count,
                        "blockedTitleCount": blocked_count,
                        "actionPerformed": completed_count > 0,
                        "actionOutcomes": [
                            {
                                "titleId": item.get("titleId"),
                                "title": item.get("title"),
                                "status": item.get("status"),
                                "reason": item.get("reason"),
                            }
                            for item in outcomes
                        ],
                        "entitlement": decisions[0] if len(decisions) == 1 else None,
                        "services": ["Data/KV"],
                        "agentTraceId": agent_trace.trace_id,
                    }


            if grounded_reply is None and catalogue_analytics_question:
                measure = str(catalogue_analytics_question.get("measure") or "count")
                dimension = str(catalogue_analytics_question.get("dimension") or "")
                filters = dict(catalogue_analytics_question.get("filters") or {})
                analytics_limit = int(catalogue_analytics_question.get("limit") or 30)
                analytics_order = str(
                    catalogue_analytics_question.get("order") or "desc"
                )
                agent.set_plan(
                    agent_trace,
                    intent="catalogue_analytics_query",
                    tools=["query_catalogue_analytics"],
                    entities={
                        "measure": measure,
                        "dimension": dimension,
                        "filters": filters,
                        "readOnly": True,
                    },
                    deterministic=True,
                )
                analytics_started = time.perf_counter()
                analytics_result = await _agent_tool_call(
                    agent,
                    agent_trace,
                    "query_catalogue_analytics",
                    catalogue.query_catalogue_analytics,
                    measure=measure,
                    dimension=dimension,
                    filters=filters,
                    limit=analytics_limit,
                    order=analytics_order,
                    inputs={
                        "measure": measure,
                        "dimension": dimension,
                        "filters": filters,
                        "limit": analytics_limit,
                        "order": analytics_order,
                    },
                )
                grounded_reply = model.render_catalogue_analytics(
                    dict(analytics_result or {})
                )
                retrieval_trace = {
                    "mode": "catalogue_analytics_query",
                    "measure": measure,
                    "dimension": dimension,
                    "filters": filters,
                    "groupCount": len((analytics_result or {}).get("rows") or []),
                    "queryMode": (analytics_result or {}).get("queryMode"),
                    "readOnly": True,
                    "elapsedMs": round(
                        (time.perf_counter() - analytics_started) * 1000, 1
                    ),
                    "services": ["Query", "Index", "Data/KV"],
                }
                response_mode = "catalogue_analytics_grounded"

            if grounded_reply is None and catalogue_count_question:
                content_type = catalogue_count_question.get("contentType")
                genre = catalogue_count_question.get("genre")
                agent.set_plan(
                    agent_trace,
                    intent="catalogue_count_query",
                    tools=["get_catalogue_statistics"],
                    entities={"contentType": content_type, "genre": genre, "aggregate": "count"},
                )
                count_started = time.perf_counter()
                count_result = await _agent_tool_call(
                    agent,
                    agent_trace,
                    "get_catalogue_statistics",
                    catalogue.count_titles,
                    content_type=content_type,
                    genre=genre,
                    inputs={"content_type": content_type, "genre": genre},
                )
                grounded_reply = model.render_catalogue_count(dict(count_result or {}))
                retrieval_trace = {
                    "mode": "catalogue_count_query",
                    "contentType": content_type,
                    "genre": genre,
                    "returnedCount": int((count_result or {}).get("count") or 0),
                    "elapsedMs": round((time.perf_counter() - count_started) * 1000, 1),
                    "services": ["Query", "Index", "Data/KV"],
                }
                response_mode = "catalogue_count_grounded"

            if grounded_reply is None and history_count_question:
                content_type = model.history_content_type(request.message)
                agent.set_plan(
                    agent_trace,
                    intent="watch_history_count_query",
                    tools=["get_watch_history"],
                    entities={"contentType": content_type, "aggregate": "count"},
                )
                history_started = time.perf_counter()
                history_items = await _agent_tool_call(
                    agent,
                    agent_trace,
                    "get_watch_history",
                    catalogue.list_watch_history,
                    login_id=login_id,
                    content_type=content_type,
                    limit=100,
                    inputs={"viewer_id": login_id, "content_type": content_type, "aggregate": "count"},
                )
                catalogue_results = history_items
                grounded_reply = model.render_watch_history_count(history_items, content_type)
                retrieval_trace = {
                    "mode": "watch_history_count_query",
                    "contentType": content_type,
                    "returnedCount": len(history_items),
                    "elapsedMs": round((time.perf_counter() - history_started) * 1000, 1),
                    "services": ["Query", "Index", "Data/KV"],
                }
                response_mode = "watch_history_count_grounded"
                await _call(
                    memory.remember_catalogue_request,
                    {
                        "query": request.message,
                        "kind": "watch_history",
                        "contentType": content_type,
                        "resultTitleIds": [str(item.get("id")) for item in history_items if item.get("id")],
                        "resultTitles": [str(item.get("title")) for item in history_items if item.get("title")],
                        "lastGroundedTitleId": str(history_items[0].get("id")) if history_items else None,
                        "lastGroundedTitle": str(history_items[0].get("title")) if history_items else None,
                    },
                )

            if grounded_reply is None and model.is_watch_history_question(request.message):
                content_type = model.history_content_type(request.message)
                agent.set_plan(
                    agent_trace,
                    intent="watch_history_query",
                    tools=["get_watch_history"],
                    entities={"contentType": content_type},
                )
                history_started = time.perf_counter()
                history_items = await _agent_tool_call(
                    agent,
                    agent_trace,
                    "get_watch_history",
                    catalogue.list_watch_history,
                    login_id=login_id,
                    content_type=content_type,
                    limit=30,
                    inputs={"viewer_id": login_id, "content_type": content_type},
                )
                catalogue_results = history_items
                grounded_reply = model.render_watch_history_reply(history_items)
                retrieval_trace = {
                    "mode": "watch_history_query",
                    "contentType": content_type,
                    "returnedCount": len(history_items),
                    "elapsedMs": round((time.perf_counter() - history_started) * 1000, 1),
                    "services": ["Query", "Index", "Data/KV"],
                }
                response_mode = "watch_history_grounded"
                await _call(
                    memory.remember_catalogue_request,
                    {
                        "query": request.message,
                        "kind": "watch_history",
                        "contentType": content_type,
                        "resultTitleIds": [str(item.get("id")) for item in history_items if item.get("id")],
                        "resultTitles": [str(item.get("title")) for item in history_items if item.get("title")],
                        "lastGroundedTitleId": str(history_items[0].get("id")) if history_items else None,
                        "lastGroundedTitle": str(history_items[0].get("title")) if history_items else None,
                    },
                )

            watched_title = model.extract_watched_title_claim(request.message)
            if grounded_reply is None and watched_title:
                agent.set_plan(
                    agent_trace,
                    intent="record_watch_completion",
                    tools=["resolve_catalogue_title", "check_entitlement", "start_or_resume_playback"],
                    entities={"title": watched_title, "completion": 100},
                )
                resolution = await _agent_tool_call(
                    agent, agent_trace, "resolve_catalogue_title",
                    catalogue.resolve_title, watched_title,
                    inputs={"title": watched_title},
                )
                matched = resolution.get("match")
                catalogue_results = ([matched] if matched else []) + list(
                    resolution.get("alternatives") or []
                )
                retrieval_trace = dict(resolution.get("trace") or {})
                if matched:
                    decision = await _agent_tool_call(
                        agent, agent_trace, "check_entitlement",
                        catalogue.check_entitlement, login_id, matched,
                        inputs={"viewer_id": login_id, "title_id": matched.get("id")},
                    )
                    matched = dict(matched)
                    matched["entitlement"] = decision
                    catalogue_results = [matched] + list(resolution.get("alternatives") or [])
                    if not decision.get("includedInPlan"):
                        grounded_reply = (
                            f"I found {matched.get('title')} in the catalogue, but I did not record it "
                            f"as watched for this viewer: {decision.get('reason')}."
                        )
                        retrieval_trace["policyDecision"] = decision
                        response_mode = "watch_event_policy_denied"
                    else:
                        await _agent_tool_call(
                            agent, agent_trace, "start_or_resume_playback",
                            catalogue.record_interaction,
                            login_id=login_id,
                            title_id=str(matched["id"]),
                            action="complete",
                            progress_pct=100.0,
                            inputs={"viewer_id": login_id, "title_id": matched.get("id"), "operation": "complete"},
                        )
                        grounded_reply = model.render_watched_confirmation(matched)
                        retrieval_trace["recordedTitleId"] = matched.get("id")
                        retrieval_trace["recordedTitle"] = matched.get("title")
                        retrieval_trace["writeServices"] = ["Data/KV"]
                        history_changed = True
                        response_mode = "watch_event_grounded"
                else:
                    grounded_reply = (
                        f"I couldn’t match ‘{watched_title}’ to a title in the catalogue. "
                        "Try the exact film or series title."
                    )
                    response_mode = "watch_event_unresolved"

            if grounded_reply is None and catalogue_complaint:
                agent.set_plan(
                    agent_trace,
                    intent="catalogue_result_recovery",
                    tools=["find_available_content"],
                    entities={},
                )
                previous = previous_catalogue_request
                if previous and previous.get("query"):
                    search_result = await _agent_tool_call(
                        agent, agent_trace, "find_available_content",
                        catalogue.search,
                        query=str(previous["query"]),
                        mode=str(previous.get("mode") or settings.chat_recommendation_mode),
                        login_id=login_id,
                        content_type=previous.get("contentType"),
                        limit=12,
                        purpose="recommendation",
                        inputs={"query": previous.get("query"), "mode": previous.get("mode")},
                    )
                    catalogue_results = list(search_result.get("results") or [])
                    retrieval_trace = dict(search_result.get("trace") or {})
                    retrieval_trace["recoveryFromComplaint"] = True
                    retrieval_trace["originalQuery"] = previous.get("query")
                    grounded_reply = model.render_catalogue_retry(
                        str(previous["query"]), catalogue_results, retrieval_trace
                    )
                    response_mode = "catalogue_retry_grounded"
                else:
                    grounded_reply = (
                        "You’re right to question those results, but I don’t have a previous catalogue request "
                        "to rerun in this active session. Please repeat the original search."
                    )
                    response_mode = "catalogue_retry_no_context"
                    retrieval_trace = {"mode": "no_previous_catalogue_request"}

            if grounded_reply is None and explanation_question:
                agent.set_plan(agent_trace, intent="explain_recommendation_policy", tools=["get_viewer_context"], entities={})
                profile_doc = await _agent_tool_call(
                    agent, agent_trace, "get_viewer_context",
                    catalogue.get_profile, login_id, inputs={"viewer_id": login_id}
                )
                grounded_reply = model.render_recommendation_explanation(profile_doc)
                response_mode = "recommendation_explanation_grounded"
                retrieval_trace = {
                    "mode": "recommendation_policy_explanation",
                    "services": ["Data/KV"],
                    "profileChanged": False,
                }

            if grounded_reply is None and likes_dislikes_question:
                agent.set_plan(agent_trace, intent="viewer_likes_dislikes", tools=["get_viewer_context"], entities={})
                summary = await _agent_tool_call(
                    agent, agent_trace, "get_viewer_context",
                    catalogue.likes_dislikes_summary, login_id, inputs={"viewer_id": login_id}
                )
                profile_doc = dict(summary.get("profile") or {})
                liked_titles = list(summary.get("likedTitles") or [])
                disliked_titles = list(summary.get("dislikedTitles") or [])
                catalogue_results = [*liked_titles, *disliked_titles]
                grounded_reply = model.render_likes_dislikes_reply(
                    profile_doc, liked_titles, disliked_titles
                )
                response_mode = "viewer_likes_dislikes_grounded"
                retrieval_trace = {
                    "mode": "viewer_likes_dislikes_kv",
                    "services": ["Data/KV"],
                    "likedTitleCount": len(liked_titles),
                    "dislikedTitleCount": len(disliked_titles),
                }

            if grounded_reply is None and profile_question:
                intent_name = "viewer_preference_query" if profile_preference_question else "viewer_profile_query"
                entities = dict(profile_preference_question or {})
                agent.set_plan(agent_trace, intent=intent_name, tools=["get_viewer_context"], entities=entities)
                profile_doc = await _agent_tool_call(
                    agent, agent_trace, "get_viewer_context",
                    catalogue.get_profile, login_id, inputs={"viewer_id": login_id}
                )
                if profile_preference_question:
                    grounded_reply = model.render_profile_preference_answer(
                        profile_preference_question, profile_doc
                    )
                    response_mode = "viewer_preference_grounded"
                else:
                    grounded_reply = model.render_profile_reply(
                        profile_doc, context["long_term"]
                    )
                    response_mode = "viewer_profile_grounded"
                retrieval_trace = {
                    "mode": "viewer_preference_kv" if profile_preference_question else "viewer_profile_kv",
                    "services": ["Data/KV", "Agent Memory"],
                    "profilePreferenceQuestion": profile_preference_question,
                }

            if (
                grounded_reply is None
                and pure_preference_statement
            ):
                grounded_reply = model.render_preference_confirmation(preferences)
                response_mode = "preference_write_grounded"
                retrieval_trace = {
                    "mode": "preference_write",
                    "services": ["Data/KV", "Agent Memory"],
                }

            if (
                grounded_reply is None
                and settings.assistant_planner_enabled
                and planner_service is not None
                and action_request is None
                and contextual_request is None
                and not title_affinity_question
                and not title_question
                and not similar_title_query
                and not direct_title_candidate
            ):
                planned = await planner_service.plan(
                    request.message,
                    catalogue_intent=catalogue_intent,
                )
                assistant_plan = dict(planned.get("plan") or {})
                planner_trace_data = dict(planned.get("trace") or {})
                plan_cache_promotion = (
                    dict(planned.get("cachePromotion") or {}) or None
                )
                planner_llm_invoked = bool(
                    planner_trace_data.get("plannerLlmInvoked")
                )
                planner_usage = dict(
                    planner_trace_data.get("plannerUsage") or {}
                )
                if assistant_plan.get("intent") in {
                    "catalogue_query",
                    "recommendation",
                    "similarity",
                }:
                    agent.set_plan(
                        agent_trace,
                        intent="governed_catalogue_plan",
                        tools=["query_catalogue_plan"],
                        entities={
                            "assistantPlan": assistant_plan,
                            "planSource": planner_trace_data.get("planSource"),
                            "planCacheHitType": planner_trace_data.get(
                                "planCacheHitType"
                            ),
                        },
                        deterministic=not planner_llm_invoked,
                    )
                    plan_result = await _agent_tool_call(
                        agent,
                        agent_trace,
                        "query_catalogue_plan",
                        catalogue.query_catalogue_plan,
                        login_id=login_id,
                        plan=assistant_plan,
                        search_mode=request.search_mode,
                        inputs={
                            "viewer_id": login_id,
                            "plan": assistant_plan,
                            "search_mode": request.search_mode,
                        },
                    )
                    if plan_cache_promotion:
                        _spawn_background(
                            asyncio.to_thread(
                                catalogue.cache_assistant_plan,
                                query=str(plan_cache_promotion["query"]),
                                plan=dict(plan_cache_promotion["plan"]),
                                guard=dict(plan_cache_promotion["guard"]),
                                source=str(plan_cache_promotion["source"]),
                                semantic_eligible=True,
                            )
                        )
                        planner_trace_data["semanticCachePromotionQueued"] = True
                    catalogue_results = list(plan_result.get("results") or [])
                    retrieval_trace = dict(plan_result.get("trace") or {})
                    execution_embedding = {
                        "embeddingModelCalls": int(
                            retrieval_trace.get("embeddingModelCalls") or 0
                        ),
                        "embeddingCacheHits": int(
                            retrieval_trace.get("embeddingCacheHits") or 0
                        ),
                        "embeddingTimeouts": int(
                            retrieval_trace.get("embeddingTimeouts") or 0
                        ),
                        "embeddingElapsedMs": float(
                            retrieval_trace.get("embeddingElapsedMs") or 0.0
                        ),
                    }
                    retrieval_trace.update(planner_trace_data)
                    for field in (
                        "embeddingModelCalls",
                        "embeddingCacheHits",
                        "embeddingTimeouts",
                    ):
                        retrieval_trace[field] = int(
                            execution_embedding.get(field) or 0
                        ) + int(planner_trace_data.get(field) or 0)
                    retrieval_trace["embeddingElapsedMs"] = round(
                        float(execution_embedding.get("embeddingElapsedMs") or 0.0)
                        + float(planner_trace_data.get("embeddingElapsedMs") or 0.0),
                        1,
                    )
                    retrieval_trace.update(
                        {
                            "chatIntent": assistant_plan.get("intent"),
                            "assistantPlan": assistant_plan,
                            "plannerServices": (
                                ["Model Service", "Data/KV", "Search/Vector"]
                                if planner_llm_invoked
                                else ["Data/KV", "Search/Vector"]
                            ),
                        }
                    )
                    if assistant_plan.get("personalise"):
                        grounded_reply = model.render_profile_recommendations(
                            catalogue_results,
                            dict(plan_result.get("profile") or {}),
                            retrieval_trace,
                        )
                        response_mode = "profile_plan_recommendation_grounded"
                    else:
                        grounded_reply = model.render_catalogue_plan_results(
                            assistant_plan,
                            catalogue_results,
                            retrieval_trace,
                        )
                        response_mode = "catalogue_plan_grounded"
                    await _call(
                        memory.remember_catalogue_request,
                        {
                            "query": request.message,
                            "kind": "governed_catalogue_plan",
                            "planSignature": assistant_plan.get("planSignature"),
                            "planSource": planner_trace_data.get("planSource"),
                            "contentType": assistant_plan.get("contentType"),
                            "resultTitleIds": [
                                str(item.get("id"))
                                for item in catalogue_results
                                if item.get("id")
                            ],
                            "resultTitles": [
                                str(item.get("title"))
                                for item in catalogue_results
                                if item.get("title")
                            ],
                            "lastGroundedTitleId": (
                                str(catalogue_results[0].get("id"))
                                if catalogue_results
                                else None
                            ),
                            "lastGroundedTitle": (
                                str(catalogue_results[0].get("title"))
                                if catalogue_results
                                else None
                            ),
                        },
                    )

            if grounded_reply is None and top_rated_question:
                content_type = top_rated_question.get("contentType")
                agent.set_plan(
                    agent_trace,
                    intent="top_rated_catalogue_title",
                    tools=["query_catalogue_ratings"],
                    entities={"contentType": content_type},
                )
                top_title = await _agent_tool_call(
                    agent,
                    agent_trace,
                    "query_catalogue_ratings",
                    catalogue.top_rated_title,
                    content_type=content_type,
                    inputs={"content_type": content_type, "order_by": "voteAverage DESC"},
                )
                catalogue_results = [top_title] if top_title else []
                grounded_reply = model.render_top_rated_title(top_title, content_type)
                response_mode = "catalogue_top_rated_grounded"
                retrieval_trace = {
                    "mode": "catalogue_rating_query",
                    "chatIntent": "top_rated_catalogue_title",
                    "contentType": content_type,
                    "returnedCount": len(catalogue_results),
                    "services": ["Query", "Index", "Data/KV"],
                }
                if top_title:
                    await _call(
                        memory.remember_catalogue_request,
                        {
                            "query": request.message,
                            "kind": "catalogue_title_information",
                            "resultTitleIds": [str(top_title.get("id"))],
                            "resultTitles": [str(top_title.get("title"))],
                            "lastGroundedTitleId": str(top_title.get("id")),
                            "lastGroundedTitle": str(top_title.get("title")),
                        },
                    )

            if grounded_reply is None and title_affinity_question:
                reference = str(title_affinity_question.get("title") or "").strip()
                if title_affinity_question.get("contextual"):
                    reference = str(
                        previous_catalogue_request.get("lastGroundedTitle")
                        or next(iter(previous_catalogue_request.get("resultTitles") or []), "")
                    ).strip()
                agent.set_plan(
                    agent_trace,
                    intent="viewer_title_affinity_query",
                    tools=["resolve_catalogue_title", "get_viewer_context", "score_title_affinity"],
                    entities={"title": reference, "contextual": bool(title_affinity_question.get("contextual"))},
                )
                resolution = await _agent_tool_call(
                    agent,
                    agent_trace,
                    "resolve_catalogue_title",
                    catalogue.resolve_title,
                    reference,
                    inputs={"title": reference, "source": "previous_grounded_title" if title_affinity_question.get("contextual") else "explicit_title"},
                ) if reference else {"match": None, "alternatives": [], "trace": {"mode": "missing_context"}}
                matched = resolution.get("match")
                alternatives = list(resolution.get("alternatives") or [])
                if matched:
                    affinity = await _agent_tool_call(
                        agent,
                        agent_trace,
                        "score_title_affinity",
                        catalogue.title_affinity,
                        login_id,
                        matched,
                        inputs={"viewer_id": login_id, "title_id": matched.get("id")},
                    )
                    catalogue_results = [matched]
                    grounded_reply = model.render_title_affinity(matched, affinity)
                    response_mode = "viewer_title_affinity_grounded"
                    retrieval_trace = dict(resolution.get("trace") or {})
                    retrieval_trace.update({
                        "mode": "viewer_title_affinity",
                        "chatIntent": "viewer_title_affinity_query",
                        "affinity": affinity,
                        "services": ["Search/FTS", "Data/KV"],
                    })
                    await _call(
                        memory.remember_catalogue_request,
                        {
                            "query": request.message,
                            "kind": "viewer_title_affinity",
                            "resultTitleIds": [str(matched.get("id"))],
                            "resultTitles": [str(matched.get("title"))],
                            "lastGroundedTitleId": str(matched.get("id")),
                            "lastGroundedTitle": str(matched.get("title")),
                        },
                    )
                else:
                    catalogue_results = alternatives
                    grounded_reply = (
                        "I do not have a previously grounded title to evaluate. Ask about a title by name first."
                        if not reference
                        else f"I couldn’t resolve ‘{reference}’ to an exact title in the catalogue."
                    )
                    response_mode = "viewer_title_affinity_unresolved"
                    retrieval_trace = dict(resolution.get("trace") or {})
                    retrieval_trace["mode"] = "viewer_title_affinity_unresolved"

            if grounded_reply is None and title_question:
                agent.set_plan(
                    agent_trace, intent="catalogue_title_information",
                    tools=["resolve_catalogue_title", "check_entitlement"],
                    entities={
                        "title": (
                            contextual_title_reference.get("title")
                            if contextual_title_reference
                            else title_question.get("title")
                        ),
                        "titleId": (
                            contextual_title_reference.get("id")
                            if contextual_title_reference
                            else None
                        ),
                        "questionType": title_question.get("type"),
                        "referenceSource": (
                            "previous_results"
                            if contextual_title_reference
                            else "explicit_title"
                        ),
                    },
                )
                if contextual_title_reference:
                    contextual_match = await _agent_tool_call(
                        agent,
                        agent_trace,
                        "resolve_catalogue_title",
                        catalogue.title,
                        str(contextual_title_reference.get("id")),
                        inputs={
                            "title_id": contextual_title_reference.get("id"),
                            "source": "previous_results",
                        },
                    )
                    resolution = {
                        "match": contextual_match,
                        "alternatives": [],
                        "trace": {
                            "mode": "exact_previous_result_id",
                            "source": "previous_results",
                        },
                    }
                else:
                    resolution = await _agent_tool_call(
                        agent, agent_trace, "resolve_catalogue_title",
                        catalogue.resolve_title, title_question["title"],
                        inputs={"title": title_question.get("title")},
                    )
                matched = resolution.get("match")
                alternatives = list(resolution.get("alternatives") or [])
                if matched:
                    matched = dict(matched)
                    matched["entitlement"] = await _agent_tool_call(
                        agent, agent_trace, "check_entitlement",
                        catalogue.check_entitlement, login_id, matched,
                        inputs={"viewer_id": login_id, "title_id": matched.get("id")},
                    )
                catalogue_results = ([matched] if matched else []) + alternatives
                retrieval_trace = dict(resolution.get("trace") or {})
                retrieval_trace.update(
                    {
                        "chatIntent": "catalogue_title_information",
                        "questionType": title_question.get("type"),
                        "normalisedQuery": catalogue_intent.get("normalisedQuery"),
                    }
                )
                grounded_reply = model.render_title_information(
                    {
                        **title_question,
                        "title": (
                            contextual_title_reference.get("title")
                            if contextual_title_reference
                            else title_question.get("title")
                        ),
                    },
                    matched,
                    alternatives,
                )
                response_mode = "catalogue_title_grounded"
                if matched and title_question.get("type") in {"overview", "details"}:
                    from .ai_enrichment import current_guide
                    guide = current_guide(matched)
                    if guide:
                        from . import usage_reporter
                        response_mode = "catalogue_ai_guide_reused"
                        retrieval_trace["aiViewingGuide"] = {"executionId": guide["executionId"],
                            "generatedAt": guide["generatedAt"], "source": guide["source"], "reused": True}
                        await _call(usage_reporter.record_external, login_id,
                            {"category": "ai_functions", "action": "chat_reuse", "status": "reused",
                             "titleId": matched.get("id"), "executionId": guide["executionId"],
                             "functionsRequested": 0, "functionsReturned": 0, "modelRequests": 0,
                             "tokenUsage": {"inputTokens": 0, "outputTokens": 0}})
                if matched:
                    await _call(
                        memory.remember_catalogue_request,
                        {
                            "query": request.message,
                            "kind": "catalogue_title_information",
                            "resultTitleIds": [str(item.get("id")) for item in catalogue_results if item and item.get("id")],
                            "resultTitles": [str(item.get("title")) for item in catalogue_results if item and item.get("title")],
                            "lastGroundedTitleId": str(matched.get("id")),
                            "lastGroundedTitle": str(matched.get("title")),
                        },
                    )

            if (
                grounded_reply is None
                and direct_title_candidate
                and not similar_title_query
                and not title_question
                and not catalogue_intent.get("genres")
                and not catalogue_intent.get("country")
            ):
                agent.set_plan(
                    agent_trace, intent="direct_catalogue_title",
                    tools=["resolve_catalogue_title", "check_entitlement"],
                    entities={"title": direct_title_candidate},
                )
                direct_resolution = await _agent_tool_call(
                    agent, agent_trace, "resolve_catalogue_title",
                    catalogue.resolve_title, direct_title_candidate,
                    inputs={"title": direct_title_candidate},
                )
                direct_match = direct_resolution.get("match")
                if direct_match:
                    direct_match = dict(direct_match)
                    direct_match["entitlement"] = await _agent_tool_call(
                        agent, agent_trace, "check_entitlement",
                        catalogue.check_entitlement, login_id, direct_match,
                        inputs={"viewer_id": login_id, "title_id": direct_match.get("id")},
                    )
                    direct_alternatives = list(direct_resolution.get("alternatives") or [])
                    catalogue_results = [direct_match, *direct_alternatives]
                    retrieval_trace = dict(direct_resolution.get("trace") or {})
                    retrieval_trace.update(
                        {
                            "chatIntent": "direct_catalogue_title",
                            "normalisedQuery": catalogue_intent.get("normalisedQuery"),
                        }
                    )
                    grounded_reply = model.render_title_information(
                        {"type": "details", "title": direct_title_candidate},
                        direct_match,
                        direct_alternatives,
                    )
                    response_mode = "catalogue_title_grounded"

            if grounded_reply is None and similar_title_query:
                agent.set_plan(
                    agent_trace, intent="similar_to_catalogue_title",
                    tools=["resolve_catalogue_title", "find_available_content", "check_entitlement"],
                    entities={"seedTitle": similar_title_query, "contentType": catalogue_intent.get("contentType")},
                )
                similarity = await _agent_tool_call(
                    agent, agent_trace, "find_available_content",
                    catalogue.similar_to_title,
                    login_id=login_id,
                    title_query=similar_title_query,
                    content_type=catalogue_intent.get("contentType"),
                    limit=12,
                    inputs={"query": similar_title_query, "mode": "vector", "limit": 12},
                )
                seed = similarity.get("seed")
                catalogue_results = list(similarity.get("results") or [])
                retrieval_trace = dict(similarity.get("trace") or {})
                retrieval_trace.update(
                    {
                        "chatIntent": "similar_to_catalogue_title",
                        "normalisedQuery": catalogue_intent.get("normalisedQuery"),
                    }
                )
                grounded_reply = model.render_similar_recommendations(seed, catalogue_results)
                response_mode = "catalogue_similarity_grounded"
                await _call(
                    memory.remember_catalogue_request,
                    {
                        "query": request.message,
                        "mode": retrieval_trace.get("mode"),
                        "contentType": catalogue_intent.get("contentType"),
                        "kind": "similar_title_recommendation",
                        "resultTitleIds": [str(item.get("id")) for item in catalogue_results if item.get("id")],
                        "resultTitles": [str(item.get("title")) for item in catalogue_results if item.get("title")],
                    },
                )

            if grounded_reply is None and personalised_recommendation:
                agent.set_plan(
                    agent_trace, intent="personalised_recommendation",
                    tools=["get_viewer_context", "get_watch_history", "rank_personalised_candidates", "check_entitlement"],
                    entities={"contentType": catalogue_intent.get("contentType"), "nextWatch": bool(catalogue_intent.get("nextWatch"))},
                )
                recommendation = await _agent_tool_call(
                    agent, agent_trace, "rank_personalised_candidates",
                    catalogue.recommend_for_profile,
                    login_id=login_id,
                    mode=settings.chat_recommendation_mode,
                    limit=12,
                    content_type=catalogue_intent.get("contentType"),
                    inputs={"viewer_id": login_id, "limit": 12, "content_type": catalogue_intent.get("contentType")},
                )
                catalogue_results = list(recommendation.get("results") or [])
                retrieval_trace = dict(recommendation.get("trace") or {})
                retrieval_trace.update(
                    {
                        "chatIntent": "personalised_recommendation",
                        "normalisedQuery": catalogue_intent.get("normalisedQuery"),
                        "nextWatch": bool(catalogue_intent.get("nextWatch")),
                    }
                )
                grounded_reply = model.render_profile_recommendations(
                    catalogue_results,
                    dict(recommendation.get("profile") or {}),
                    retrieval_trace,
                )
                response_mode = "profile_recommendation_grounded"
                await _call(
                    memory.remember_catalogue_request,
                    {
                        "query": recommendation.get("trace", {}).get("query") or request.message,
                        "mode": recommendation.get("trace", {}).get("mode") or settings.chat_recommendation_mode,
                        "contentType": recommendation.get("trace", {}).get("structuredContentType"),
                        "kind": "profile_recommendation",
                        "resultTitleIds": [str(item.get("id")) for item in catalogue_results if item.get("id")],
                        "resultTitles": [str(item.get("title")) for item in catalogue_results if item.get("title")],
                    },
                )

            if grounded_reply is None and catalogue_browse_request:
                search_mode = request.search_mode
                if search_mode == "hybrid" and settings.chat_recommendation_mode == "fts":
                    search_mode = "fts"
                agent.set_plan(
                    agent_trace, intent="catalogue_browse",
                    tools=["find_available_content", "check_entitlement"],
                    entities={"query": catalogue_query, "mode": search_mode},
                )
                search_result = await _agent_tool_call(
                    agent, agent_trace, "find_available_content",
                    catalogue.search,
                    query=catalogue_query,
                    mode=search_mode,
                    login_id=login_id,
                    limit=12,
                    purpose="recommendation",
                    inputs={"query": catalogue_query, "mode": search_mode, "limit": 12},
                )
                catalogue_results = list(search_result.get("results") or [])
                retrieval_trace = dict(search_result.get("trace") or {})
                retrieval_trace.update(
                    {
                        "chatIntent": "catalogue_browse",
                        "normalisedQuery": catalogue_intent.get("normalisedQuery"),
                        "originalUserMessage": request.message,
                        "catalogueQuery": catalogue_query,
                    }
                )
                grounded_reply = model.render_catalogue_recommendations(
                    request.message, catalogue_results, retrieval_trace
                )
                response_mode = "catalogue_grounded_deterministic"
                await _call(
                    memory.remember_catalogue_request,
                    {
                        "query": catalogue_query,
                        "originalQuery": request.message,
                        "mode": search_mode,
                        "contentType": retrieval_trace.get("structuredContentType"),
                        "kind": "catalogue_recommendation",
                        "resultTitleIds": [str(item.get("id")) for item in catalogue_results if item.get("id")],
                        "resultTitles": [str(item.get("title")) for item in catalogue_results if item.get("title")],
                    },
                )

            # A viewer-facing response may use only evidence returned by the
            # database-backed routes above. Unmatched requests never fall
            # through to model pretraining. This is a runtime invariant rather
            # than a prompt-only instruction.
            if grounded_reply is None:
                model_fault = bool(
                    showcase_service is not None
                    and showcase_service.faults().get("model_unavailable")
                )
                agent.set_plan(
                    agent_trace,
                    intent="database_boundary",
                    tools=[],
                    entities={
                        "groundedTitleCount": len(catalogue_results),
                        "modelCallBlocked": True,
                    },
                    deterministic=True,
                )
                grounded_reply = model.render_database_boundary_reply(request.message)
                response_mode = (
                    "model_unavailable_safe_fallback"
                    if model_fault
                    else "database_boundary_grounded"
                )
                retrieval_trace = {
                    "mode": "database_boundary",
                    "effectiveMode": "database_boundary",
                    "chatIntent": "unsupported_without_database_evidence",
                    "groundingRequired": True,
                    "modelCallBlocked": True,
                    "returnedCount": 0,
                    "services": ["Data/KV"],
                }

            # Recommendation and browse services apply entitlement policy while
            # filtering their candidates. Record that aggregate policy step as
            # an explicit agent span without repeating Couchbase reads.
            if catalogue_results and not any(
                span.get("name") == "check_entitlement" for span in agent_trace.spans
            ):
                decisions = [
                    dict(item.get("entitlement") or {})
                    for item in catalogue_results
                    if item.get("entitlement")
                ]
                if decisions:
                    policy_started = time.perf_counter()
                    all_allowed = all(bool(item.get("allowed")) for item in decisions)
                    all_included = all(bool(item.get("includedInPlan")) for item in decisions)
                    agent.record_tool(
                        agent_trace,
                        name="check_entitlement",
                        started=policy_started,
                        status="completed",
                        inputs={
                            "viewer_id": login_id,
                            "title_ids": [item.get("id") for item in catalogue_results],
                            "aggregate": True,
                        },
                        output={
                            "allowed": all_allowed,
                            "includedInPlan": all_included,
                            "reason": f"{len(decisions)} returned title(s) passed recommendation entitlement policy",
                            "results": catalogue_results,
                        },
                    )

            yield _line(
                "context",
                memory=context,
                captured_facts=facts,
                captured_preferences=preferences,
                catalogue=catalogue_results,
                retrieval_trace=retrieval_trace,
                response_mode=response_mode,
                history_changed=history_changed,
                profile_changed=profile_changed,
                agent_trace={
                    "traceId": agent_trace.trace_id,
                    "plan": agent_trace.plan,
                    "toolCalls": agent_trace.spans,
                    "policy": agent_trace.policy,
                },
            )

            answer_parts: list[str] = []
            first_token_ms: float | None = None
            llm_invoked = planner_llm_invoked
            model_usage: dict[str, Any] = dict(planner_usage)
            if grounded_reply is None:
                raise RuntimeError("Database-boundary router produced no grounded response")
            grounded_reply = model.sanitise_consumer_language(grounded_reply)
            first_token_ms = round((time.perf_counter() - started) * 1000, 1)
            answer_parts.append(grounded_reply)
            yield _line("token", content=grounded_reply)

            answer = model.sanitise_consumer_language(
                "".join(answer_parts).strip()
                or "I could not generate a response. Try a more specific request."
            )
            await _call(
                memory.record_turn_local,
                target=target,
                user_message=request.message,
                assistant_message=answer,
                facts=facts,
            )
            response_ms = round((time.perf_counter() - started) * 1000, 1)
            if not agent_trace.plan:
                agent.set_plan(
                    agent_trace,
                    intent=str(retrieval_trace.get("chatIntent") or response_mode),
                    tools=[],
                    entities={"retrievalMode": retrieval_trace.get("mode")},
                    deterministic=not llm_invoked,
                )
            try:
                trace_document = await _call(
                    agent.finish_trace,
                    agent_trace,
                    response_mode=response_mode,
                    assistant_response=answer,
                    grounded_title_ids=[str(item.get("id")) for item in catalogue_results if item.get("id")],
                    llm_invoked=llm_invoked,
                    response_ms=response_ms,
                    policy={
                        "groundedTitleCount": len(catalogue_results),
                        "entitlementChecks": sum(1 for span in agent_trace.spans if span.get("name") == "check_entitlement"),
                    },
                )
            except Exception as trace_exc:
                trace_document = agent_trace.as_document()
                trace_document["persistenceError"] = f"{type(trace_exc).__name__}: {trace_exc}"
            retrieval_trace["agentTraceId"] = agent_trace.trace_id
            retrieval_trace["toolCallCount"] = len(agent_trace.spans)
            yield _line(
                "response_complete",
                response_ms=response_ms,
                first_token_ms=first_token_ms,
                assistant=answer,
                catalogue=catalogue_results,
                retrieval_trace=retrieval_trace,
                captured_facts=facts,
                captured_preferences=preferences,
                response_mode=response_mode,
                history_changed=history_changed,
                profile_changed=profile_changed,
                viewer=memory.snapshot(include_memories=False),
                agent_trace=trace_document,
            )

            # Planner response usage is limited to this app turn. Deployment
            # totals come only from actual HTTP attempts in the shared ledger.
            prompt_tokens_used = model_usage.get("promptTokens") if llm_invoked else 0
            completion_tokens_used = model_usage.get("completionTokens") if llm_invoked else 0
            metric_event = {
                "category": "chat_turn", "label": response_mode, "responseMode": response_mode,
                "plannerLlmCalls": 1 if planner_llm_invoked else 0,
                "responseMs": response_ms, "firstChunkMs": first_token_ms,
                "details": {
                    "planSource": planner_trace_data.get("planSource"),
                    "planCacheHitType": planner_trace_data.get("planCacheHitType"),
                    "longTermFactsSupplied": len(context["long_term"]),
                    "agentTraceId": agent_trace.trace_id,
                },
            }
            await _record_metric_safe(catalogue, login_id, metric_event)
            observation_recorded = True
            _spawn_background(
                _persist_chat_artifacts(
                    memory=memory,
                    catalogue=catalogue,
                    login_id=login_id,
                    target=target,
                    user_message=request.message,
                    assistant_message=answer,
                    facts=facts,
                    metric_event=metric_event,
                    sync_profile=profile_changed,
                )
            )
            yield _line(
                "done",
                response_ms=response_ms,
                first_token_ms=first_token_ms,
                persistence_ms=0.0,
                persistence_status="queued",
                persistence_error=None,
                stored={"conversation_block_ids": [], "fact_block_ids": []},
                enrichment="background",
                history_changed=history_changed,
                profile_changed=profile_changed,
                llm_invoked=llm_invoked,
                planner={
                    "source": planner_trace_data.get("planSource"),
                    "cacheHit": bool(planner_trace_data.get("planCacheHit")),
                    "cacheHitType": planner_trace_data.get("planCacheHitType"),
                    "similarity": planner_trace_data.get("similarity"),
                    "llmInvoked": planner_llm_invoked,
                },
                token_usage={
                    "scope": "app_planner_response_only; deployment usage is in /api/metrics",
                    "promptUsed": prompt_tokens_used,
                    "completionUsed": completion_tokens_used,
                    "source": model_usage.get("source")
                    or ("no_app_planner_request" if not llm_invoked else "unavailable"),
                },
                metrics=None,
                agent_trace_id=agent_trace.trace_id,
            )
        except Exception as exc:
            if agent_trace is not None and agent_service is not None:
                await asyncio.to_thread(agent_service.fail_trace, agent_trace, exc)
                if not observation_recorded:
                    await _record_metric_safe(catalogue, agent_trace.viewer_id, {
                        "category": "chat_failed", "label": type(exc).__name__,
                        "responseMs": (time.perf_counter()-started)*1000,
                    })
            yield _line("error", detail=f"{type(exc).__name__}: {exc}")

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.get("/api/memories")
async def memories() -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    login_id = _login_id(memory)
    try:
        data = await _call(memory.list_memories)
    except HTTPException:
        data = memory.cached_memories()
    structured = await _call(catalogue.profile_memory_blocks, login_id)
    return {"ok": True, "data": _merge_memory_blocks(data, structured)}


@app.post("/api/memories/search")
async def search_memories(request: MemorySearchRequest) -> dict[str, Any]:
    memory, _, catalogue = await _services_wait()
    login_id = _login_id(memory)
    started = time.perf_counter()
    data = await _call(memory.manual_search, request.query, request.scope)
    metrics = await _record_metric_safe(
        catalogue,
        login_id,
        {
            "category": "semantic_memory_search",
            "label": request.scope,
            "interactions": 1,
            "agentMemoryReads": 1,
            "semanticMemorySearches": 1,
            "responseMs": (time.perf_counter() - started) * 1000,
            "details": {"query": request.query[:160], "scope": request.scope, "returnedCount": len(data.get("blocks") or [])},
        },
    )
    return {"ok": True, "data": data, "metrics": metrics}
