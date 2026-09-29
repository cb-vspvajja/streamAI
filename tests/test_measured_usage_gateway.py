from __future__ import annotations
import asyncio
import json
import os
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"usage-gateway"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from usage_store_fake import FakeStore
from storage import StorageUnavailable
import server
from ledger import Ledger


class Chunks(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data
    async def __aiter__(self):
        for pos in range(0,len(self.data),7):
            yield self.data[pos:pos+7]


class UsageGatewayTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.factory = patch.object(server, "create_ledger", side_effect=lambda: Ledger(self.store))
        self.factory.start()
        server.UPSTREAMS = {"chat/completions":"https://chat.test", "embeddings":"https://embed.test"}
        self.requests = []
        self.handler = lambda request: httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}})
        async def transport(request):
            self.requests.append(request)
            return self.handler(request)
        original = httpx.AsyncClient
        self.mock = patch.object(server.httpx, "AsyncClient", side_effect=lambda **kwargs: original(transport=httpx.MockTransport(transport), **kwargs))
        self.mock.start()
        self.client = TestClient(server.app)
        self.client.__enter__()
    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.mock.stop()
        self.factory.stop()
    def post(self, source="app", **body):
        return self.client.post(f"/{source}/v1/chat/completions", json={"model":"mistral", "messages":[{"role":"user","content":"PRIVATE PROMPT"}], **body}, headers={"Authorization":"Bearer SECRET"})
    def summary(self, viewer=None):
        return self.client.get("/metrics", params={"viewer":viewer} if viewer else {}).json()
    def test_ai_functions_are_audited_separately_and_never_fabricate_tokens(self):
        self.client.post('/activity',json={'id':'generation-1','category':'ai_functions','action':'generate',
            'status':'in_flight','functionsRequested':3,'modelRequests':None})
        self.client.post('/activity',json={'id':'generation-1','category':'ai_functions','action':'generate',
            'status':'succeeded','functionsRequested':3,'functionsReturned':3,'modelRequests':None})
        self.client.post('/activity',json={'category':'ai_functions','action':'chat_reuse','status':'reused',
            'functionsRequested':0,'modelRequests':0})
        self.client.post('/activity',json={'category':'chat_turn','viewerId':'sai'})
        self.post('agent_memory')
        stats=self.summary()
        self.assertEqual(stats['aiFunctions']['generationAttempts'],1)
        self.assertEqual(stats['aiFunctions']['completed'],1)
        self.assertEqual(stats['aiFunctions']['functionResults'],3)
        self.assertEqual(stats['aiFunctions']['guideReuses'],1)
        self.assertEqual(stats['aiFunctions']['chatReuses'],1)
        self.assertIsNone(stats['aiFunctions']['tokens'])
        self.assertIsNone(stats['aiFunctions']['modelRequests'])
        self.assertEqual(stats['totals']['chatRequests'],1)
        self.assertEqual(stats['comparison']['observed']['llmTokens'],15)
        self.assertEqual(stats['comparison']['estimatedDifference']['llmTokens'],1185)
        self.assertFalse(stats['comparison']['overallSavingsMeasured'])

    def test_counts_ui_memory_ingestion_setup_and_preserves_privacy(self):
        for source in ("app","agent_memory","ingestion","setup"):
            self.assertEqual(self.post(source).status_code,200)
        stats=self.summary()
        self.assertEqual(stats["totals"]["chatRequests"],4)
        self.assertEqual(stats["totals"]["reportedInputTokens"],48)
        self.assertEqual(stats["totals"]["reportedOutputTokens"],12)
        self.assertEqual(len(stats["bySource"]),4)
        stored=json.dumps(server.ledger.events())
        self.assertNotIn("PRIVATE PROMPT",stored)
        self.assertNotIn("SECRET",stored)
        self.assertEqual(self.requests[0].headers["authorization"],"Bearer SECRET")
    def test_embedding_tokens_are_included_and_batch_is_one_request(self):
        self.handler=lambda request: httpx.Response(200,json={"data":[],"usage":{"prompt_tokens":23,"total_tokens":23}})
        response=self.client.post("/ingestion/v1/embeddings",json={"model":"nvidia","input":["a","b"],"input_type":"passage","encoding_format":"float"})
        self.assertEqual(response.status_code,200)
        self.assertEqual(str(self.requests[0].url),"https://embed.test/v1/embeddings")
        self.assertEqual(json.loads(self.requests[0].content)["input_type"],"passage")
        total=self.summary()["totals"]
        self.assertEqual(total["embeddingRequests"],1)
        self.assertEqual(total["reportedInputTokens"],23)
        self.assertEqual(total["reportedOutputTokens"],0)
        self.assertTrue(total["usageComplete"])
    def test_missing_usage_is_not_reported_as_complete_zero(self):
        self.handler=lambda request: httpx.Response(200,json={"choices":[]})
        self.post()
        total=self.summary()["totals"]
        self.assertEqual(total["requestsWithMissingUsage"],1)
        self.assertEqual(total["inputUsageReports"],0)
        self.assertFalse(total["usageComplete"])
    def test_retries_are_separate_attempts_including_failures(self):
        self.handler=lambda request: httpx.Response(429,json={"error":"limit"})
        for n in range(2):
            self.assertEqual(self.post("agent_memory").status_code,429)
        total=self.summary()["totals"]
        self.assertEqual(total["requests"],2)
        self.assertEqual(total["failed"],2)
        self.assertEqual(total["requestsWithMissingUsage"],2)
    def test_transport_error_remains_visible_without_fabricated_tokens(self):
        def timeout(request): raise httpx.ReadTimeout("private endpoint")
        self.handler=timeout
        response=self.post()
        self.assertEqual(response.status_code,502)
        self.assertNotIn("private endpoint",response.text)
        self.assertEqual(self.summary()["totals"]["failed"],1)
        self.assertFalse(self.summary()["totals"]["usageComplete"])
    def test_stream_usage_is_counted_once_across_chunk_boundaries(self):
        events=[{"choices":[{"delta":{"content":"ok"}}]}, {"usage":{"prompt_tokens":12,"completion_tokens":2}}, {"usage":{"prompt_tokens":12,"completion_tokens":3,"total_tokens":15}}]
        data=("".join("data: "+json.dumps(e)+"\n\n" for e in events)+"data: [DONE]\n\n").encode()
        self.handler=lambda request: httpx.Response(200,stream=Chunks(data),headers={"content-type":"text/event-stream"})
        response=self.post(stream=True)
        self.assertEqual(response.content,data)
        self.assertTrue(json.loads(self.requests[0].content)["stream_options"]["include_usage"])
        total=self.summary()["totals"]
        self.assertEqual(total["reportedInputTokens"],12)
        self.assertEqual(total["reportedOutputTokens"],3)
        self.assertEqual(total["succeeded"],1)
    def test_truncated_stream_has_unknown_outcome_and_incomplete_usage(self):
        data=b'data: {"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\n'
        self.handler=lambda request: httpx.Response(200,stream=Chunks(data))
        self.post(stream=True)
        total=self.summary()["totals"]
        self.assertEqual(total["unknownOutcome"],1)
        self.assertFalse(total["usageComplete"])
    def test_memory_api_counts_real_acceptance_and_health_calls(self):
        self.handler=lambda request: httpx.Response(202,json={"block_ids":["b1"]})
        self.client.post("/memory-api/users/viewer-sai/sessions/s1/memory",json={"facts":["PRIVATE FACT"],"async_processing":True})
        self.client.get("/memory-api/health")
        self.handler=lambda request: httpx.Response(503,json={"detail":"unavailable"})
        self.client.post("/memory-api/users/viewer-sai/sessions/s1/memory",json={"facts":["PRIVATE FACT"]})
        stats=self.summary("sai")["memoryApi"]
        self.assertEqual(stats["requests"],3)
        self.assertEqual(stats["writeRequestsAccepted"],1)
        self.assertEqual(stats["failed"],1)
        self.assertEqual(stats["healthRequests"],1)
        self.assertNotIn("PRIVATE FACT",json.dumps(server.ledger.events()))
    def test_concurrency_does_not_lose_observations_and_viewers_are_separate(self):
        def write(n): server.ledger.activity({"category":"chat_turn", "viewerId":"sai" if n%2 else "other", "contextFactCount":2, "responseMs":n})
        with ThreadPoolExecutor(max_workers=8) as pool: list(pool.map(write,range(100)))
        stats=self.summary("sai")
        self.assertEqual(stats["activity"]["completedChatTurns"],50)
        self.assertEqual(stats["activity"]["contextFactOccurrencesSupplied"],100)
        self.assertEqual(stats["activity"]["meanServerAnswerMs"],50)
    def test_reporting_window_retains_raw_history(self):
        self.post()
        self.client.post("/window")
        self.assertEqual(self.summary()["totals"]["requests"],0)
        self.assertEqual(len(server.ledger.events()),2)
        self.assertEqual(server.ledger.events()[-1]["category"],"reporting_window_started")
    def test_comparison_assumptions_are_validated_persistent_and_audited(self):
        from comparison import DEFAULTS
        custom={**DEFAULTS,"llmRequestsPerChat":2,"llmInputUsdPerMillion":1.25}
        self.assertEqual(self.client.put("/comparison/assumptions",json=custom).status_code,200)
        self.assertEqual(self.client.put("/comparison/assumptions",json={**custom,"llmRequestsPerChat":-1}).status_code,422)
        self.client.post("/activity",json={"category":"chat_turn","viewerId":"sai"})
        self.client.post("/activity",json={"category":"chat_turn","viewerId":"another"})
        result=self.summary("sai")
        self.assertEqual(result["activity"]["completedChatTurns"],1)
        self.assertEqual(result["comparison"]["completedChats"],2)
        self.assertEqual(result["comparison"]["baseline"]["llmRequests"],4)
        self.client.post("/window")
        self.assertEqual(self.summary()["comparison"]["assumptions"],custom)
        reopened=Ledger(self.store)
        self.assertEqual(reopened.comparison_assumptions(),custom)
        self.assertEqual([e["assumptions"] for e in reopened.events() if e.get("category")=="comparison_assumptions_changed"],[custom])
        reopened.close()
    def test_storage_failure_prevents_unrecorded_inference(self):
        self.store.fail_writes = True
        response = self.post()
        self.assertEqual(response.status_code,503)
        self.assertEqual(self.requests,[])
        self.assertEqual(len(self.store.records),0)
        self.assertIn("Couchbase", response.text)

    def test_final_write_failure_leaves_unknown_usage_and_closes_response(self):
        def finish(request):
            self.store.fail_writes = True
            return httpx.Response(200,json={"usage":{"prompt_tokens":10,"completion_tokens":2}})
        self.handler = finish
        self.assertEqual(self.post().status_code,503)
        self.assertEqual(len(self.requests),1)
        self.store.fail_writes = False
        self.assertEqual(self.summary()["totals"]["inFlight"],1)
        self.assertFalse(self.summary()["totals"]["usageComplete"])
        restarted=Ledger(self.store)
        self.assertEqual(restarted.summary()["totals"]["unknownOutcome"],1)
        self.assertIsNone(restarted.summary()["comparison"]["observed"]["llmTokens"])

    def test_database_read_failure_is_unavailable_not_zero(self):
        self.post()
        self.store.fail_reads = True
        for route in ("/health", "/metrics"):
            result=self.client.get(route)
            self.assertEqual(result.status_code,503)
            self.assertFalse(result.json()["healthy"])
            self.assertNotIn("totals",result.json())

    def test_health_and_summary_name_couchbase_as_primary(self):
        self.assertEqual(self.client.get("/health").json()["storage"],"couchbase")
        self.assertEqual(self.summary()["persistence"]["storage"],"couchbase")
        self.assertNotIn("replication",self.summary())

    def test_restart_preserves_unfinished_attempt_as_unknown(self):
        store=FakeStore()
        ledger=Ledger(store)
        ledger.put({"id":"r1","kind":"model_request","timestamp":time.time(),"status":"in_flight","source":"app","operation":"chat/completions"})
        started=ledger.meta("instrumentationStartedAt")
        window=ledger.new_window()
        ledger.close()
        restarted=Ledger(store)
        self.assertEqual(restarted.events()[0]["status"],"unknown_outcome")
        self.assertEqual(restarted.meta("instrumentationStartedAt"),started)
        self.assertEqual(restarted.meta("windowStartedAt"),window)

    def test_billing_and_savings_are_unavailable_not_invented(self):
        self.post()
        billing=self.summary()["billing"]
        self.assertIsNone(billing["amount"])
        self.assertIsNone(billing["savings"])
        self.assertEqual(billing["status"],"not_measured")

    def test_stream_final_line_without_newline_is_observed(self):
        data=b'data: {"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\ndata: [DONE]'
        self.handler=lambda request: httpx.Response(200,stream=Chunks(data))
        self.post(stream=True)
        self.assertEqual(self.summary()["totals"]["succeeded"],1)
        self.assertTrue(self.summary()["totals"]["usageComplete"])

    def test_json_error_with_http_success_is_not_a_successful_model_call(self):
        self.handler=lambda request: httpx.Response(200,json={"error":{"message":"failed"}})
        self.post()
        self.assertEqual(self.summary()["totals"]["failed"],1)
        self.assertFalse(self.summary()["totals"]["usageComplete"])

    def test_activity_rejects_invalid_values_and_drops_free_text(self):
        for data in ({"contextFactCount":-1}, {"responseMs":"unknown"}, {"planCacheHit":"yes"}):
            response=self.client.post("/activity",json={"category":"chat_turn",**data})
            self.assertEqual(response.status_code,422)
        response=self.client.post("/activity",json={"category":"chat_turn", "contextFactCount":None, "label":"PRIVATE TEXT"})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.summary()["activity"]["contextFactOccurrencesSupplied"],0)
        self.assertNotIn("PRIVATE TEXT",json.dumps(server.ledger.events()))

    def test_cached_tokens_are_metadata_not_subtracted_or_counted_twice(self):
        self.handler=lambda request: httpx.Response(200,json={"usage":{"prompt_tokens":100,"completion_tokens":5,"prompt_tokens_details":{"cached_tokens":75}}})
        self.post()
        stats=self.summary()
        self.assertEqual(stats["totals"]["reportedInputTokens"],100)
        self.assertEqual(stats["recentRequests"][0]["usage"]["cachedInputTokens"],75)

    def test_provider_total_only_is_not_invented_input_and_output(self):
        self.handler=lambda request: httpx.Response(200,json={"usage":{"total_tokens":50}})
        self.post()
        stats=self.summary()["totals"]
        self.assertEqual(stats["inputUsageReports"],0)
        self.assertFalse(stats["usageComplete"])


if __name__ == "__main__": unittest.main()
