from __future__ import annotations

import sys
import types

if "couchbase" not in sys.modules:
    couchbase = types.ModuleType("couchbase")
    auth = types.ModuleType("couchbase.auth")
    cluster = types.ModuleType("couchbase.cluster")
    exceptions = types.ModuleType("couchbase.exceptions")
    n1ql = types.ModuleType("couchbase.n1ql")
    options = types.ModuleType("couchbase.options")
    auth.PasswordAuthenticator = object
    cluster.Cluster = object
    exceptions.DocumentNotFoundException = type("DocumentNotFoundException", (Exception,), {})
    n1ql.QueryScanConsistency = types.SimpleNamespace(REQUEST_PLUS="request_plus")
    options.ClusterOptions = object
    options.QueryOptions = object
    sys.modules.update({
        "couchbase": couchbase,
        "couchbase.auth": auth,
        "couchbase.cluster": cluster,
        "couchbase.exceptions": exceptions,
        "couchbase.n1ql": n1ql,
        "couchbase.options": options,
    })

from copy import deepcopy
from types import SimpleNamespace

from ui.app.catalogue_service import CatalogueService
from ui.app.llm_service import LocalModelService


class _ContentAs:
    def __init__(self, value):
        self.value = value

    def __getitem__(self, _):
        return deepcopy(self.value)


class _GetResult:
    def __init__(self, value):
        self.content_as = _ContentAs(value)


class _FakeCollection:
    def __init__(self, initial):
        self.value = deepcopy(initial)

    def get(self, _key):
        return _GetResult(self.value)

    def upsert(self, _key, value):
        self.value = deepcopy(value)


def _service() -> CatalogueService:
    service = CatalogueService.__new__(CatalogueService)
    service.settings = SimpleNamespace(chat_provider="capella_model_service", chat_model="mistral",
                                       embedding_provider="capella_model_service", embedding_model="nvidia")
    service.ai_metrics = _FakeCollection({"totals": {"llmCalls": 0, "promptTokensAvoided": 3000},
                                        "pricing": {"actualRuntime": "Local Ollama"}})
    return service


def test_legacy_estimates_are_retained_but_not_presented(monkeypatch):
    from ui.app import usage_reporter
    monkeypatch.setattr(usage_reporter, "snapshot", lambda viewer: {"available": True, "totals": {"chatRequests": 15}})
    service = _service()
    before = deepcopy(service.ai_metrics.value)
    result = service.get_ai_metrics("sai")
    assert result["measured"]["totals"]["chatRequests"] == 15
    assert "derived" not in result and "totals" not in result and "pricing" not in result
    assert "Ollama" not in str(result)
    assert result["runtime"]["providerLabel"] == "Capella Model Service"
    assert service.ai_metrics.value == before


def test_only_observed_activity_is_submitted(monkeypatch):
    from ui.app import usage_reporter
    submitted = []
    monkeypatch.setattr(usage_reporter, "record", lambda viewer, event: submitted.append((viewer, event)) or True)
    monkeypatch.setattr(usage_reporter, "snapshot", lambda viewer: {"available": True})
    service = _service()
    result = service.record_ai_metric("sai", {"category": "chat_turn", "plannerLlmCalls": 0,
        "promptTokensAvoided": 10000, "agentMemoryReads": 1, "responseMs": 123,
        "details": {"planSource": "exact_cache", "planCacheHitType": "exact", "longTermFactsSupplied": 2}})
    event = submitted[0][1]
    assert event["responseMs"] == 123 and event["contextFactCount"] == 2
    assert event["planCacheHit"] and event["plannerDecision"] and not event["plannerRequested"]
    assert "promptTokensAvoided" not in event and "agentMemoryReads" not in event
    assert result["observationRecorded"] is True


def test_missing_meter_has_no_zero_or_legacy_fallback(monkeypatch):
    from ui.app import usage_reporter
    monkeypatch.setattr(usage_reporter, "snapshot", lambda viewer: {"available": False, "error": "not configured"})
    result = _service().get_ai_metrics("sai")
    assert result["telemetryAvailable"] is False
    assert "totals" not in result and "derived" not in result


def test_window_reset_preserves_legacy_records(monkeypatch):
    from ui.app import usage_reporter
    calls = []
    monkeypatch.setattr(usage_reporter, "new_window", lambda: calls.append("window"))
    monkeypatch.setattr(usage_reporter, "snapshot", lambda viewer: {"available": True})
    service = _service(); before = deepcopy(service.ai_metrics.value)
    service.reset_ai_metrics("sai")
    assert calls == ["window"] and service.ai_metrics.value == before


def test_failed_observation_is_reported(monkeypatch):
    from ui.app import usage_reporter
    monkeypatch.setattr(usage_reporter, "record", lambda viewer, event: False)
    monkeypatch.setattr(usage_reporter, "snapshot", lambda viewer: {"available": True, "activityReportingFailuresThisProcess": 1})
    result = _service().record_ai_metric("sai", {"category": "chat_failed"})
    assert result["observationRecorded"] is False
    assert result["measured"]["activityReportingFailuresThisProcess"] == 1


def test_enabled_managed_inference_is_marked_outside_meter(monkeypatch):
    from ui.app import usage_reporter
    monkeypatch.setattr(usage_reporter, "snapshot", lambda viewer: {"available": True})
    service = _service()
    service.settings.ai_functions_enabled = True
    service.settings.data_processing_mode = "capella_workflow"
    assert service.get_ai_metrics("sai")["measured"]["unmeteredServices"] == ["AI Functions", "managed Data Processing"]
