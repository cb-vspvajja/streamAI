from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Awaitable, Callable


@lru_cache(maxsize=1)
def _trace_content_adapter() -> Any:
    # Span.log in agentc 1.1 expects a Content model, not a dictionary.
    # Keep this import lazy so deployments without native tracing still work.
    from agentc.span import Content
    from pydantic import TypeAdapter

    return TypeAdapter(Content)


@dataclass(frozen=True)
class CatalogToolExecution:
    name: str
    result: Any
    duration_ms: float
    catalog_id: str | None
    transport: str


class AgentCatalogGateway:
    """Native Couchbase Agent Catalog and Agent Tracer integration.

    Agent Catalog is the governed source of prompt/tool definitions. The chosen
    agent runtime still executes tools, as intended by Agent Catalog. StreamAI's
    default runtime executor maps the catalogued tool to a warm Couchbase MCP
    Server session; the catalog function remains available as an explicit
    troubleshooting fallback.
    """

    def __init__(self, settings: Any, required_tool_names: list[str]) -> None:
        self.settings = settings
        self.enabled = bool(settings.agent_catalog_enabled)
        self.required = bool(settings.agent_catalog_required)
        self.required_tool_names = list(required_tool_names)
        self._catalog: Any = None
        self._prompt: Any = None
        self._tool_items: dict[str, Any] = {}
        self._last_error: str | None = None
        self._catalog_id: str | None = None
        self._initialised_at: float | None = None
        self._native_trace_count = 0
        self._native_tool_count = 0
        self._last_trace_error: str | None = None
        if self.enabled:
            self._connect()

    def _connect(self) -> None:
        try:
            import agentc

            catalog = agentc.Catalog()
            prompt = self._single(
                catalog.find("prompt", name=self.settings.agent_catalog_prompt_name)
            )
            if prompt is None:
                raise RuntimeError(
                    f"Prompt {self.settings.agent_catalog_prompt_name!r} was not found in Agent Catalog"
                )
            tools: dict[str, Any] = {}
            for name in self.required_tool_names:
                item = self._single(catalog.find("tool", name=name))
                if item is not None:
                    tools[name] = item
            missing = [name for name in self.required_tool_names if name not in tools]
            if missing:
                raise RuntimeError(
                    "Published Agent Catalog is missing required tools: " + ", ".join(missing)
                )
            self._catalog = catalog
            self._prompt = prompt
            self._tool_items = tools
            self._catalog_id = self._extract_catalog_id(prompt)
            self._initialised_at = time.time()
            self._last_error = None
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            if self.required:
                raise RuntimeError(
                    "Couchbase Agent Catalog is required but could not be loaded. "
                    "Run scripts/04d-publish-agent-catalog.sh before starting the UI. "
                    f"Cause: {self._last_error}"
                ) from exc

    @property
    def healthy(self) -> bool:
        return self.enabled and self._catalog is not None and not self._last_error

    @property
    def prompt_content(self) -> str | None:
        content = getattr(self._prompt, "content", None)
        return str(content).strip() if content is not None else None

    @property
    def catalog_id(self) -> str | None:
        return self._catalog_id

    @property
    def native_trace_error(self) -> str | None:
        return self._last_trace_error

    def tool_available(self, name: str) -> bool:
        return self.healthy and name in self._tool_items

    async def execute_tool(
        self,
        *,
        name: str,
        inputs: dict[str, Any],
        root_span: Any = None,
        trace_id: str | None = None,
        runtime_executor: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
    ) -> CatalogToolExecution:
        if name not in self._tool_items:
            raise KeyError(f"Tool {name!r} is not available in the published Agent Catalog")
        item = self._tool_items[name]
        started = time.perf_counter()
        transport = "catalog_function"

        async def invoke() -> Any:
            nonlocal transport
            if runtime_executor is not None:
                transport = "couchbase_mcp_server"
                return await runtime_executor(name, dict(inputs))
            return await asyncio.to_thread(self._invoke_item, item, inputs)

        child = None
        tool_call_id = f"{trace_id or name}::{uuid.uuid4().hex}"
        try:
            if root_span is not None:
                child = self._new_child_span(
                    root_span,
                    name=f"tool:{name}",
                    component="agent_catalog",
                    catalog_id=str(self._catalog_id or ""),
                    transport=("mcp" if runtime_executor is not None else "catalog_function"),
                )
                self._span_enter(child)
                self._span_log(
                    child,
                    {
                        "kind": "tool-call",
                        "tool_name": name,
                        "tool_args": self._safe_value(inputs),
                        "tool_call_id": tool_call_id,
                    },
                )
            result = await invoke()
            self._native_tool_count += 1
            if child is not None:
                self._span_log(
                    child,
                    {
                        "kind": "tool-result",
                        "tool_result": self._safe_value(result),
                        "tool_call_id": tool_call_id,
                        "extra": {"tool_name": name},
                    },
                )
            return CatalogToolExecution(
                name=name,
                result=result,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                catalog_id=self._catalog_id,
                transport=transport,
            )
        except Exception as exc:
            if child is not None:
                self._span_log(
                    child,
                    {
                        "kind": "tool-result",
                        "tool_call_id": tool_call_id,
                        "tool_result": {"error": f"{type(exc).__name__}: {exc}"},
                        "status": "error",
                        "extra": {"tool_name": name},
                    },
                )
            raise
        finally:
            if child is not None:
                self._span_exit(child)

    def start_trace(
        self, *, trace_id: str, viewer_id: str, session_id: str | None, user_message: str
    ) -> Any:
        if not self.healthy or not self.settings.native_agent_tracing_enabled:
            return None
        session = f"streamai::{viewer_id}::{session_id or trace_id}"
        # The documented Agent Tracer lifecycle is Span(...), enter(), log(), exit().
        try:
            root = self._catalog.Span(name="streamai_chat_turn", session=session)
        except TypeError:
            # Agent Catalog versions that only accept the documented name
            # parameter still receive session/viewer metadata in the first log.
            root = self._catalog.Span(name="streamai_chat_turn")
        try:
            self._span_enter(root)
            self._span_log(
                root,
                {
                    "kind": "user",
                    "value": user_message,
                    "user_id": viewer_id,
                    "extra": {
                        "trace_id": trace_id,
                        "viewer_id": viewer_id,
                        "app_version": self.settings.app_version,
                        "catalog_id": self._catalog_id,
                    },
                },
            )
        except Exception as exc:
            self._last_trace_error = f"{type(exc).__name__}: {exc}"
            try:
                self._span_exit(root)
            except Exception:
                pass
            raise
        self._last_trace_error = None
        self._native_trace_count += 1
        return root

    def finish_trace(
        self,
        root_span: Any,
        *,
        assistant_response: str,
        response_mode: str,
        response_ms: float,
        grounded_title_ids: list[str],
    ) -> bool:
        if root_span is None:
            return False
        try:
            self._span_log(
                root_span,
                {
                    "kind": "assistant",
                    "value": assistant_response[:5000],
                    "extra": {"response_mode": response_mode, "response_ms": response_ms},
                },
            )
            self._span_log(
                root_span,
                {
                    "kind": "key-value",
                    "key": "grounded_title_ids",
                    "value": list(grounded_title_ids),
                },
            )
        except Exception as exc:
            self._last_trace_error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._span_exit(root_span)
        return True

    def fail_trace(self, root_span: Any, exc: Exception) -> bool:
        if root_span is None:
            return False
        try:
            self._span_log(
                root_span,
                {
                    "kind": "key-value",
                    "key": "failure",
                    "value": f"{type(exc).__name__}: {exc}",
                },
            )
        finally:
            self._span_exit(root_span)
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "required": self.required,
            "healthy": self.healthy,
            "package": "agentc",
            "mode": "native_agent_catalog",
            "catalogId": self._catalog_id,
            "promptName": self.settings.agent_catalog_prompt_name,
            "promptLoaded": self._prompt is not None,
            "toolCount": len(self._tool_items),
            "tools": [self._tool_metadata(name, item) for name, item in self._tool_items.items()],
            "nativeTracerEnabled": bool(self.settings.native_agent_tracing_enabled),
            "nativeTraceCount": self._native_trace_count,
            "nativeToolExecutions": self._native_tool_count,
            "nativeTraceError": self._last_trace_error,
            "initialisedAt": self._initialised_at,
            "error": self._last_error,
        }

    @staticmethod
    def _single(value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            return value[0] if value else None
        return value

    @staticmethod
    def _extract_catalog_id(item: Any) -> str | None:
        meta = getattr(item, "meta", None)
        for source in (meta, item):
            if source is None:
                continue
            for name in ("catalog_id", "catalogId", "version", "id"):
                value = getattr(source, name, None)
                if value:
                    return str(value)
            if isinstance(source, dict):
                for name in ("catalog_id", "catalogId", "version", "id"):
                    if source.get(name):
                        return str(source[name])
        return None

    @staticmethod
    def _invoke_item(item: Any, inputs: dict[str, Any]) -> Any:
        func = getattr(item, "func", None)
        if not callable(func):
            raise RuntimeError("Agent Catalog item does not expose an executable function")
        signature = inspect.signature(func)
        parameters = list(signature.parameters.values())
        if len(parameters) == 1 and parameters[0].name not in inputs:
            model = getattr(item, "input", None)
            if isinstance(model, type):
                return func(model(**inputs))
        try:
            return func(**inputs)
        except TypeError as exc:
            model = getattr(item, "input", None)
            if isinstance(model, type):
                return func(model(**inputs))
            raise exc

    @staticmethod
    def _new_child_span(root_span: Any, *, name: str, **tags: Any) -> Any:
        try:
            return root_span.new(name=name, **tags)
        except TypeError:
            return root_span.new(name=name)

    @staticmethod
    def _span_enter(span: Any) -> None:
        enter = getattr(span, "enter", None)
        if callable(enter):
            enter()
            return
        context_enter = getattr(span, "__enter__", None)
        if callable(context_enter):
            context_enter()

    @staticmethod
    def _span_exit(span: Any) -> None:
        exit_method = getattr(span, "exit", None)
        if callable(exit_method):
            exit_method()
            return
        context_exit = getattr(span, "__exit__", None)
        if callable(context_exit):
            context_exit(None, None, None)

    def _span_log(self, span: Any, content: dict[str, Any]) -> None:
        try:
            log = getattr(span, "log", None)
            if not callable(log):
                raise TypeError("Agent Tracer span does not expose log()")
            log(content=_trace_content_adapter().validate_python(content))
        except Exception as exc:
            self._last_trace_error = f"{type(exc).__name__}: {exc}"
            raise

    @staticmethod
    def _safe_value(value: Any) -> Any:
        if value is None or isinstance(value, (int, float, bool)):
            return value
        if isinstance(value, str):
            return value[:2000]
        if isinstance(value, list):
            return [AgentCatalogGateway._safe_value(item) for item in value[:30]]
        if isinstance(value, dict):
            result = {}
            for key, item in list(value.items())[:40]:
                if str(key).lower() in {"password", "token", "api_key", "embedding"}:
                    result[str(key)] = "[redacted]"
                else:
                    result[str(key)] = AgentCatalogGateway._safe_value(item)
            return result
        if hasattr(value, "model_dump"):
            return AgentCatalogGateway._safe_value(value.model_dump())
        return str(value)[:2000]

    @staticmethod
    def _tool_metadata(name: str, item: Any) -> dict[str, Any]:
        meta = getattr(item, "meta", None)
        description = (
            getattr(meta, "description", None)
            or getattr(item, "description", None)
            or ""
        )
        return {
            "name": name,
            "description": str(description),
            "source": "Couchbase Agent Catalog",
            "runtime": "StreamAI agent framework",
            "transport": "Couchbase MCP Server",
        }
