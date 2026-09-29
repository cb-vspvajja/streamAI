from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from ui.app.ai_enrichment import current_guide, response_text, source_fingerprint
from ui.app.capella_ai_services import CapellaAIServices
from ui.app.llm_service import LocalModelService


class Collection:
    def __init__(self, docs=None):
        self.docs = deepcopy(docs or {})
        self.cas = 1
        self.fail_write = False

    def get(self, key):
        return SimpleNamespace(content_as={dict: deepcopy(self.docs[key])}, cas=self.cas)

    def upsert(self, key, value):
        if self.fail_write:
            raise RuntimeError("write unavailable")
        self.docs[key] = deepcopy(value)

    def replace(self, key, value, options):
        if options["cas"] != self.cas:
            raise RuntimeError("CAS mismatch: catalogue changed")
        self.docs[key] = deepcopy(value)
        self.cas += 1


@pytest.fixture
def setup(monkeypatch):
    document = {"id": "movie::42", "title": "Example", "overview": "A detective solves a puzzle.",
                "parentalRating": "18", "availability": {"GB": False}}
    titles = Collection({"movie::42": document})
    events = Collection()
    calls = []
    published = []
    class Cluster:
        def query(self, statement, options):
            calls.append((statement, options))
            rows = [{"title": "Example", "classification": [{"response": "cerebral"}],
                     "summary": [{"response": "A detective solves a mystery."}],
                     "sentiment": [{"response": "neutral"}]}]
            return SimpleNamespace(rows=lambda: rows, metadata=lambda: SimpleNamespace(request_id=lambda: "query-123"))
    settings = SimpleNamespace(content_bucket="streaming", catalogue_scope="catalogue", titles_collection="titles",
                               ai_functions_enabled=True, ai_functions_temperature=.1, ai_functions_max_tokens=220,
                               data_processing_mode="python_loader", data_processing_workflow_id="")
    service = CapellaAIServices(settings, Cluster())
    monkeypatch.setattr(service, "_collections", lambda: (titles, events))
    monkeypatch.setattr("ui.app.usage_reporter.record_external", lambda viewer, event: published.append(deepcopy(event)) or True)
    return service, titles, events, calls, published


def test_generation_persists_guide_and_query_provenance_without_changing_policy(setup):
    service, titles, events, calls, _ = setup
    result = service.enrich_title("movie::42", viewer_id="sai")
    assert not result["cacheHit"]
    guide = titles.docs["movie::42"]["aiEnrichment"]
    assert guide["summary"] == "A detective solves a mystery."
    assert guide["queryRequestId"] == "query-123" and guide["tokenUsage"] is None
    assert titles.docs["movie::42"]["parentalRating"] == "18"
    assert titles.docs["movie::42"]["availability"] == {"GB": False}
    assert calls[0][1]["read_only"] is True
    assert list(events.docs.values())[0]["status"] == "succeeded"
    assert list(events.docs.values())[0]["modelRequests"] is None
    assert service.viewing_guide("movie::42")["guide"] == guide


def test_repeat_and_concurrent_requests_reuse_persisted_guide(setup):
    service, titles, events, calls, _ = setup
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: service.enrich_title("movie::42"), range(3)))
    assert len(calls) == 1 and sum(r["cacheHit"] for r in results) == 2
    assert len({r["guide"]["executionId"] for r in results}) == 1
    reused = [e for e in events.docs.values() if e["status"] == "reused"]
    assert len(reused) == 2 and all(e["modelRequests"] == 0 for e in reused)


def test_source_changes_and_explicit_regeneration_require_new_function_execution(setup):
    service, titles, _, calls, _ = setup
    first = service.enrich_title("movie::42")
    titles.docs["movie::42"]["overview"] = "A changed synopsis."
    assert service.viewing_guide("movie::42")["stale"]
    assert current_guide(titles.docs["movie::42"]) is None
    second = service.enrich_title("movie::42")
    third = service.enrich_title("movie::42", refresh=True)
    assert len(calls) == 3
    assert len({r["guide"]["executionId"] for r in (first, second, third)}) == 3


def test_recipe_change_does_not_reuse_old_generation(setup):
    service, _, _, calls, _ = setup
    service.enrich_title("movie::42")
    service.settings.ai_functions_max_tokens = 400
    assert not service.enrich_title("movie::42")["cacheHit"]
    assert len(calls) == 2


def test_disabled_generation_keeps_saved_guides_available(setup):
    service, _, _, calls, _ = setup
    service.enrich_title("movie::42")
    service.settings.ai_functions_enabled = False
    assert service.enrich_title("movie::42")["cacheHit"]
    with pytest.raises(RuntimeError, match="disabled"):
        service.enrich_title("movie::42", refresh=True)
    assert len(calls) == 1


@pytest.mark.parametrize("value", [None, [], "", {"error": "rejected"}, [{"response": None}], {"unexpected": "x"}])
def test_invalid_function_responses_are_not_success(value):
    with pytest.raises(ValueError):
        response_text(value)


def test_partial_function_failure_does_not_publish_a_guide(setup):
    service, titles, events, _, _ = setup
    service.cluster.query = lambda *a: SimpleNamespace(rows=lambda: [{"classification": "cerebral", "summary": {"error": "denied"}, "sentiment": "neutral"}])
    with pytest.raises(RuntimeError, match="could not be completed"):
        service.enrich_title("movie::42")
    assert "aiEnrichment" not in titles.docs["movie::42"]
    event = list(events.docs.values())[0]
    assert event["status"] == "failed" and event["tokenUsage"] is None
    assert service.snapshot()["aiFunctions"]["error"]


def test_catalogue_edit_during_generation_is_never_overwritten(setup):
    service, titles, _, _, _ = setup
    original = service.cluster.query
    def changed(*args):
        result = original(*args)
        titles.cas += 1
        titles.docs["movie::42"]["parentalRating"] = "12"
        return result
    service.cluster.query = changed
    with pytest.raises(RuntimeError, match="CAS mismatch"):
        service.enrich_title("movie::42")
    assert titles.docs["movie::42"]["parentalRating"] == "12"
    assert "aiEnrichment" not in titles.docs["movie::42"]


def test_no_model_work_if_initial_audit_cannot_be_saved(setup):
    service, _, events, calls, _ = setup
    events.fail_write = True
    with pytest.raises(RuntimeError):
        service.enrich_title("movie::42")
    assert not calls


def test_assistant_uses_current_guide_but_never_uses_it_for_entitlement(setup):
    service, titles, _, _, _ = setup
    service.enrich_title("movie::42")
    doc = titles.docs["movie::42"]
    text = LocalModelService.render_title_information({"type": "overview"}, doc)
    assert "saved AI viewing guide" in text and "A detective solves a mystery." in text
    doc["entitlement"] = {"allowed": False, "reason": "parental restriction"}
    text = LocalModelService.render_title_information({"type": "availability"}, doc)
    assert "not available" in text and "parental restriction" in text
    doc["overview"] = "Changed operational synopsis."
    text = LocalModelService.render_title_information({"type": "overview"}, doc)
    assert "Changed operational synopsis" in text and "saved AI viewing guide" not in text


def test_catalogue_reingestion_preserves_guide_and_changed_source_is_stale(setup):
    from tools.load_catalogue import CouchbaseLoader
    service, titles, _, _, _ = setup
    service.enrich_title("movie::42")
    guide = deepcopy(titles.docs["movie::42"]["aiEnrichment"])
    loader = CouchbaseLoader.__new__(CouchbaseLoader)
    loader.titles = titles
    fresh = {k: v for k, v in titles.docs["movie::42"].items() if k != "aiEnrichment"}
    loader.upsert_titles([fresh])
    assert current_guide(titles.docs["movie::42"]) == guide
    loader.upsert_titles([{**fresh, "overview": "Changed at TMDB."}])
    assert titles.docs["movie::42"]["aiEnrichment"] == guide
    assert current_guide(titles.docs["movie::42"]) is None
