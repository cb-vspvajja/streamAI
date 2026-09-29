"""Regression checks against the real agentc Span/Content API; no database required.

Run inside the UI image: python -m unittest discover -s tests -p test_capella_native_tracing.py
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from agentc_core.activity.span import Span
from ui.app.agent_catalog_gateway import AgentCatalogGateway
from ui.app.agent_service import GovernedAgentService


class RecordingRootSpan(Span):
    session: str


class NativeTracingTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.fail_kind = None

        def logger(*, content, session_id, span_name, **kwargs):
            # Like the real SDK, require typed Content rather than accepting dicts.
            if content.kind == self.fail_kind:
                raise RuntimeError("diagnostic logging failure")
            self.events.append({"content": content.model_dump(mode="json"),
                                "session": session_id, "span": span_name})

        settings = SimpleNamespace(agent_catalog_enabled=False, agent_catalog_required=False,
                                   native_agent_tracing_enabled=True, app_version="1.0.0",
                                   agent_catalog_prompt_name="test_prompt")
        self.gateway = AgentCatalogGateway(settings, [])
        self.gateway.enabled = True
        self.gateway._catalog = SimpleNamespace(
            Span=lambda name, session: RecordingRootSpan(logger=logger, name=name, session=session))
        self.gateway._tool_items = {"inspect_streamai_schema": SimpleNamespace()}
        self.gateway._catalog_id = "test-catalog"
        self.documents = []
        catalogue = SimpleNamespace(sync_agent_catalog=lambda _: None,
                                    write_agent_trace=self.documents.append)
        self.service = GovernedAgentService(catalogue, agent_catalog_gateway=self.gateway)

    def start(self):
        return self.service.new_trace(viewer_id="test", session_id="test-session", user_message="hello")

    def finish(self, trace):
        return self.service.finish_trace(trace, response_mode="test", assistant_response="done",
                                         grounded_title_ids=[], llm_invoked=False, response_ms=2)

    def contents(self, kind):
        return [e["content"] for e in self.events if e["content"]["kind"] == kind]

    def test_user_assistant_tools_and_metadata_reach_real_span(self):
        trace = self.start()

        async def executor(name, inputs):
            return {"result": "ok", "api_key": "must-not-leak"}

        for _ in range(2):
            asyncio.run(self.gateway.execute_tool(name="inspect_streamai_schema",
                inputs={"token": "must-not-leak"}, root_span=trace._native_span,
                trace_id=trace.trace_id, runtime_executor=executor))
        document = self.finish(trace)
        user = self.contents("user")[0]
        self.assertEqual(user["value"], "hello")
        self.assertEqual(user["user_id"], "test")
        self.assertEqual(user["extra"]["trace_id"], trace.trace_id)
        self.assertEqual(self.contents("assistant")[0]["extra"]["response_mode"], "test")
        calls, results = self.contents("tool-call"), self.contents("tool-result")
        self.assertEqual(len({c["tool_call_id"] for c in calls}), 2)
        for call, result in zip(calls, results):
            self.assertEqual(call["tool_call_id"], result["tool_call_id"])
            self.assertEqual(call["tool_args"]["token"], "[redacted]")
            self.assertEqual(result["tool_result"], {"result": "ok", "api_key": "[redacted]"})
        self.assertEqual(len(self.contents("begin")), 3)
        self.assertEqual(len(self.contents("end")), 3)
        self.assertEqual({e["session"] for e in self.events}, {document["nativeTraceSession"]})
        self.assertEqual(document["nativeTraceSession"], "streamai::test::test-session")
        self.assertTrue(document["policy"]["nativeAgentTracerLogged"])

    def test_failed_start_is_closed_and_not_reported_as_logged(self):
        self.fail_kind = "user"
        trace = self.start()
        document = self.finish(trace)
        self.assertIn("diagnostic logging failure", document["policy"]["nativeTraceStartError"])
        self.assertFalse(document["policy"]["nativeAgentTracerLogged"])
        self.assertEqual(len(self.contents("end")), 1)
        self.assertFalse(self.service.catalog_snapshot()["agentTracer"]["healthy"])

    def test_failed_finish_still_closes_and_saves_operational_trace(self):
        trace = self.start()
        self.fail_kind = "assistant"
        document = self.finish(trace)
        self.assertFalse(document["policy"]["nativeAgentTracerLogged"])
        self.assertIn("diagnostic logging failure", document["policy"]["nativeAgentTracerError"])
        self.assertEqual(len(self.contents("end")), 1)
        self.assertEqual(len(self.documents), 1)

    def test_tool_failure_uses_supported_error_result_and_closes_spans(self):
        trace = self.start()

        async def executor(name, inputs):
            raise ValueError("test tool error")

        with self.assertRaisesRegex(ValueError, "test tool error"):
            asyncio.run(self.gateway.execute_tool(name="inspect_streamai_schema", inputs={},
                root_span=trace._native_span, trace_id=trace.trace_id, runtime_executor=executor))
        result = self.contents("tool-result")[0]
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["tool_call_id"], self.contents("tool-call")[0]["tool_call_id"])
        self.service.fail_trace(trace, ValueError("test tool error"))
        self.assertEqual(self.contents("key-value")[0]["key"], "failure")
        self.assertEqual(len(self.contents("end")), 2)
        self.assertEqual(self.documents[0]["status"], "failed")

    def test_failure_logging_error_does_not_hide_application_failure(self):
        trace = self.start()
        self.fail_kind = "key-value"
        self.service.fail_trace(trace, ValueError("original application failure"))
        document = self.documents[0]
        self.assertIn("original application failure", document["policy"]["failure"])
        self.assertFalse(document["policy"]["nativeAgentTracerLogged"])
        self.assertIn("diagnostic logging failure", document["policy"]["nativeAgentTracerError"])

    def test_disabled_tracing_is_not_reported_as_logged(self):
        self.gateway.settings.native_agent_tracing_enabled = False
        document = self.finish(self.start())
        self.assertFalse(document["policy"]["nativeAgentTracerLogged"])
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
