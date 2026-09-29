from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ledger import Ledger, reported_usage
from storage import CouchbaseStore, StorageUnavailable
import memory_trial

ledger = None
SOURCES = {"app", "agent_memory", "ingestion", "setup", "diagnostic"}
UPSTREAMS = {"chat/completions": os.getenv("UPSTREAM_CHAT_URL", "").rstrip("/").removesuffix("/v1"),
             "embeddings": os.getenv("UPSTREAM_EMBEDDING_URL", "").rstrip("/").removesuffix("/v1")}
http_client = None


def create_ledger():
    store = CouchbaseStore.from_env()
    try:
        return Ledger(store)
    except BaseException:
        store.close()
        raise


@asynccontextmanager
async def lifespan(app):
    global http_client, ledger
    ledger = await asyncio.to_thread(create_ledger)
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(360, connect=15))
    try:
        yield
    finally:
        await http_client.aclose()
        await asyncio.to_thread(ledger.close)


app = FastAPI(lifespan=lifespan)


@app.exception_handler(StorageUnavailable)
async def unavailable(request, exc):
    return JSONResponse(status_code=503, content={
        "detail": str(exc), "storage": "couchbase", "healthy": False})


@app.get("/health")
def health():
    return ledger.health()


@app.get("/metrics")
def metrics(viewer: str | None = None):
    return ledger.summary(viewer)


@app.post("/window")
def window():
    return {"windowStartedAt": ledger.new_window(), "historyDeleted": False}


@app.put("/comparison/assumptions")
async def comparison_assumptions(request: Request):
    try:
        return {"assumptions": await asyncio.to_thread(ledger.set_comparison_assumptions, await request.json())}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/activity")
async def activity(request: Request):
    body = await request.json()
    # Only bounded measurement fields enter the ledger; no user text or secrets.
    if not isinstance(body, dict):
        raise HTTPException(422, "Measurement must be a JSON object")
    allowed = {"id", "viewerId", "category", "responseMs", "firstChunkMs", "plannerRequested",
               "plannerDecision", "planCacheHit", "contextFactCount", "success", "operation", "durationMs", "errorType", "action", "status", "titleId", "executionId",
               "functionsRequested", "functionsReturned", "modelRequests", "persisted"}
    clean = {k: v for k,v in body.items() if k in allowed and isinstance(v, (str, int, float, bool, type(None)))}
    for key, value in clean.items():
        if isinstance(value, str) and len(value) > 256:
            raise HTTPException(422, "Measurement string exceeds 256 characters")
        if key in {"responseMs", "firstChunkMs", "durationMs", "contextFactCount", "functionsRequested", "functionsReturned", "modelRequests"} and value is not None:
            if not isinstance(value, (float, int)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise HTTPException(422, "Measurement values must be finite and nonnegative")
        if key in {"contextFactCount", "functionsRequested", "functionsReturned", "modelRequests"} and value is not None and not isinstance(value, int):
            raise HTTPException(422, "Context fact count must be an integer")
        if key in {"plannerRequested", "plannerDecision", "planCacheHit", "success", "persisted"} and value is not None and not isinstance(value, bool):
            raise HTTPException(422, "Measurement flags must be Boolean")
    if not clean.get("category"):
        raise HTTPException(422, "Measurement category is required")
    return {"id": await asyncio.to_thread(ledger.activity, clean)}


@app.post("/memory-trial")
async def start_memory_trial(request: Request):
    try:
        return await asyncio.to_thread(memory_trial.start, ledger, await request.json())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/memory-trial/{run_id}")
async def update_memory_trial(run_id: str, request: Request):
    try:
        return await asyncio.to_thread(memory_trial.update, ledger, run_id, await request.json())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.api_route("/memory-api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def memory_api(path: str, request: Request):
    raw = await request.body()
    payload = {}
    with suppress(ValueError):
        payload = json.loads(raw) if raw else {}
    parts = path.strip("/").split("/")
    user = payload.get("user_id") if isinstance(payload, dict) else None
    if "users" in parts:
        pos = parts.index("users") + 1
        if pos < len(parts) and parts[pos] not in {"search", "list"}:
            user = parts[pos]
    resource = "health" if "health" in path else "memory" if "memory" in parts else "session" if "sessions" in parts else "user"
    event = {"id": "memory-api::"+uuid.uuid4().hex, "kind": "activity", "timestamp": time.time(),
             "category": "memory_http", "resource": resource, "operation": request.method,
             "viewerId": str(user).removeprefix("viewer-") if user else None,
             "status": "in_flight", "success": None,
             "writeRequested": resource == "memory" and request.method in {"POST", "PUT", "PATCH", "DELETE"} and not path.endswith("/search")}
    await asyncio.to_thread(ledger.put, event)
    started = time.perf_counter()
    try:
        target = os.getenv("MEMORY_API_UPSTREAM", "http://memory:8080").rstrip("/")+"/"+path
        response = await http_client.request(request.method, target, params=request.query_params, content=raw,
            headers={k:v for k,v in request.headers.items() if k.lower() in {"authorization", "content-type"}})
        event.update(status="succeeded" if response.is_success else "http_error", success=response.is_success, httpStatus=response.status_code)
        return Response(response.content, status_code=response.status_code, media_type=response.headers.get("content-type", "application/json"))
    except asyncio.CancelledError:
        event.update(status="unknown_outcome", interruption="client_disconnect")
        raise
    except Exception as exc:
        event.update(status="transport_error", success=False, errorType=type(exc).__name__)
        raise HTTPException(502, "Agent Memory request failed") from exc
    finally:
        event["durationMs"] = round((time.perf_counter()-started)*1000, 3)
        await asyncio.to_thread(ledger.put, event)


@app.api_route("/{source}/v1/{operation:path}", methods=["GET", "POST"])
async def inference(source: str, operation: str, request: Request):
    if source not in SOURCES or operation not in {*UPSTREAMS, "models"}:
        raise HTTPException(404, "Unsupported model endpoint")
    upstream = UPSTREAMS.get(operation, UPSTREAMS["chat/completions"])
    if not upstream.startswith("https://"):
        raise HTTPException(503, "HTTPS model endpoint is not configured")
    headers = {k:v for k,v in request.headers.items() if k.lower() in {"authorization", "content-type"} or k.lower().startswith("x-cb-")}
    body = await request.json() if request.method == "POST" else None
    if operation == "models":
        response = await http_client.get(upstream+"/v1/models", headers=headers)
        return Response(response.content, status_code=response.status_code, media_type="application/json")
    if request.method != "POST" or not isinstance(body, dict):
        raise HTTPException(405, "POST with a JSON body is required")
    streaming = bool(body.get("stream"))
    if streaming:
        body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
    event = {"id": "model::"+uuid.uuid4().hex, "kind": "model_request", "timestamp": time.time(),
             "source": source, "operation": operation, "model": str(body.get("model") or ""),
             "status": "in_flight", "streaming": streaming, "usage": {},
             "clientRetryNumber": request.headers.get("x-stainless-retry-count")}
    trial_id = request.headers.get("x-streamai-trial")
    phase = request.headers.get("x-streamai-phase")
    if trial_id or phase:
        import re
        if source != "agent_memory" or not re.fullmatch(r"[0-9a-f]{32}", trial_id or "") or phase not in memory_trial.PHASES:
            raise HTTPException(422, "Invalid trial correlation")
        event.update(trialId=trial_id, trialPhase=phase)
        headers["x-cb-cache"] = "none"
    # No inference is forwarded until Couchbase acknowledges the initial record.
    await asyncio.to_thread(ledger.put, event)
    started = time.perf_counter()
    response = None
    try:
        outbound = http_client.build_request("POST", upstream+"/v1/"+operation, headers=headers, json=body)
        response = await http_client.send(outbound, stream=True)
        event["httpStatus"] = response.status_code
        event["providerRequestId"] = response.headers.get("x-request-id")
        event["cacheHeaders"] = {k:v for k,v in response.headers.items() if k.lower().startswith("x-cb-cache")}
        if not streaming or response.status_code >= 400:
            raw = await response.aread()
            parsed = None
            with suppress(ValueError):
                parsed = json.loads(raw)
                event["usage"] = reported_usage(parsed, operation)
            event["status"] = ("provider_error" if isinstance(parsed, dict) and parsed.get("error") else "succeeded") if response.is_success else "http_error"
            event["durationMs"] = round((time.perf_counter()-started)*1000, 3)
            try:
                await asyncio.to_thread(ledger.put, event)
            finally:
                await response.aclose()
            return Response(raw, status_code=response.status_code, media_type=response.headers.get("content-type", "application/json"))
    except StorageUnavailable:
        # The initial record stays in flight/unknown; do not claim a model failure
        # or retry a possibly completed inference because its final write failed.
        if response is not None:
            await response.aclose()
        raise
    except asyncio.CancelledError:
        event.update(status="unknown_outcome", interruption="client_disconnect", durationMs=round((time.perf_counter()-started)*1000, 3))
        try:
            await asyncio.to_thread(ledger.put, event)
        finally:
            if response is not None:
                await response.aclose()
        raise
    except Exception as exc:
        event.update(status="transport_error", errorType=type(exc).__name__, durationMs=round((time.perf_counter()-started)*1000, 3))
        try:
            await asyncio.to_thread(ledger.put, event)
        finally:
            if response is not None:
                await response.aclose()
        raise HTTPException(502, "Model request failed; usage outcome may be unknown") from exc

    async def chunks():
        pending = b""
        done = False
        stream_error = False
        def observe(line):
            nonlocal done, stream_error
            if not line.startswith(b"data:"):
                return
            payload = line[5:].strip()
            if payload == b"[DONE]":
                done = True
            else:
                with suppress(ValueError):
                    item = json.loads(payload)
                    if not isinstance(item, dict):
                        return
                    if item.get("usage"):
                        event["usage"] = reported_usage(item, operation)
                    if item.get("error"):
                        stream_error = True
        try:
            async for chunk in response.aiter_bytes():
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    observe(line)
                if len(pending) > 1048576:
                    raise ValueError("Oversized streaming event")
                yield chunk
            if pending:
                observe(pending)
            event["status"] = "stream_error" if stream_error else "succeeded" if done else "unknown_outcome"
        except asyncio.CancelledError:
            event["status"] = "stream_error" if stream_error else "succeeded" if done else "unknown_outcome"
            event["interruption"] = "client_disconnect"
            raise
        except Exception as exc:
            event.update(status="stream_error", errorType=type(exc).__name__)
            raise
        finally:
            event["durationMs"] = round((time.perf_counter()-started)*1000, 3)
            try:
                await asyncio.to_thread(ledger.put, event)
            finally:
                await response.aclose()
    return StreamingResponse(chunks(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", **{k:v for k,v in response.headers.items() if k.lower().startswith("x-cb-")}})
