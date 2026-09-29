"""Publish observations, never token or cost estimates, to the durable usage ledger."""
from __future__ import annotations
import os
import threading
import uuid
import httpx

_lock = threading.Lock()
_failures = 0
_external_failures = 0


def url():
    return os.getenv("MODEL_USAGE_URL", "").rstrip("/")


def record(viewer_id, event):
    global _failures
    if not url():
        return False
    body = {"id": uuid.uuid4().hex, "viewerId": viewer_id, **event}
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(url()+"/activity", json=body)
            response.raise_for_status()
        return True
    except Exception:
        with _lock:
            _failures += 1
        return False


def snapshot(viewer_id):
    if not url():
        return {"available": False, "error": "The usage gateway is not configured; model totals are unavailable."}
    try:
        with httpx.Client(timeout=20) as client:
            response = client.get(url()+"/metrics", params={"viewer": viewer_id})
            response.raise_for_status()
        result = response.json()
        result.update(available=True, activityReportingFailuresThisProcess=_failures, externalReportingFailuresThisProcess=_external_failures)
        return result
    except Exception as exc:
        return {"available": False, "error": f"Usage gateway unavailable ({type(exc).__name__}); do not interpret missing counters as zero."}


def new_window():
    if not url():
        raise RuntimeError("Usage gateway is not configured")
    with httpx.Client(timeout=10) as client:
        response = client.post(url()+"/window")
        response.raise_for_status()
    return response.json()


def health():
    if not url():
        return {"enabled": False, "healthy": False, "detail": "Usage gateway is not configured; model totals are unavailable."}
    try:
        with httpx.Client(timeout=10) as client:
            response = client.get(url()+"/health")
            response.raise_for_status()
        data = response.json()
        return {"enabled": True, "healthy": bool(data.get("healthy")) and data.get("storage") == "couchbase",
                "storage": data.get("storage"), "keyspace": data.get("keyspace"),
                "detail": "Model usage, reporting settings and Memory API request records are stored directly in Couchbase."}
    except Exception as exc:
        return {"enabled": True, "healthy": False, "error": type(exc).__name__}


def set_comparison_assumptions(values):
    if not url():
        raise RuntimeError("Usage gateway is not configured")
    with httpx.Client(timeout=10) as client:
        response = client.put(url()+"/comparison/assumptions", json=values)
        if response.status_code == 422:
            raise ValueError(response.json().get("detail", "Invalid comparison assumptions"))
        response.raise_for_status()
    return response.json()


def record_external(viewer_id, event):
    """Record external execution metadata separately from metered model requests."""
    global _external_failures
    if not url():
        return False
    allowed = {"id", "category", "action", "status", "titleId", "executionId", "functionsRequested",
               "functionsReturned", "modelRequests", "durationMs", "persisted"}
    body = {"id": uuid.uuid4().hex, "viewerId": viewer_id,
            **{key: value for key, value in event.items() if key in allowed}}
    try:
        with httpx.Client(timeout=10) as client:
            response = client.post(url()+"/activity", json=body)
            response.raise_for_status()
        return True
    except Exception:
        with _lock:
            _external_failures += 1
        return False
