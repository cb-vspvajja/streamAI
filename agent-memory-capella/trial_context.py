"""Correlate real memory model requests without storing prompts in the meter."""
from contextvars import ContextVar
from functools import wraps
import inspect
import re

_scope = ContextVar("streamai_memory_trial", default={})

def trial_headers():
    return dict(_scope.get())

def metered_memory(fn):
    signature = inspect.signature(fn)
    def headers(args, kwargs):
        blocks = signature.bind(*args, **kwargs).arguments.get("memory_blocks", [])
        values = {(str((getattr(b, "annotations", None) or {}).get("streamai_trial", "")),
                   str((getattr(b, "annotations", None) or {}).get("streamai_phase", ""))) for b in blocks}
        if len(values) != 1:
            return {}
        run, phase = values.pop()
        if re.fullmatch(r"[0-9a-f]{32}", run) and phase in {"previous", "optimized"}:
            return {"X-StreamAI-Trial": run, "X-StreamAI-Phase": phase, "X-cb-cache": "none"}
        return {}
    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def async_wrapper(*args, **kwargs):
            token = _scope.set(headers(args, kwargs))
            try:
                return await fn(*args, **kwargs)
            finally:
                _scope.reset(token)
        return async_wrapper
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _scope.set(headers(args, kwargs))
        try:
            return fn(*args, **kwargs)
        finally:
            _scope.reset(token)
    return wrapper

