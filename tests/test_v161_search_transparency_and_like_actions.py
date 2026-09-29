from __future__ import annotations

import sys
import types
from pathlib import Path

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

from ui.app.agent_service import GovernedAgentService
from ui.app.catalogue_service import CatalogueService
from ui.app.config import Settings


def bare_catalogue() -> CatalogueService:
    service = object.__new__(CatalogueService)
    service.settings = Settings()
    return service


def test_final_display_scores_follow_final_carousel_order() -> None:
    service = bare_catalogue()
    docs = [
        {"id": "a", "searchScore": 2.0},
        {"id": "b", "searchScore": 9.0},
        {"id": "c", "searchScore": 5.0},
    ]
    service._finalise_display_scores(
        docs,
        requested_mode="hybrid",
        effective_mode="fts",
        ranking_strategy="exact_genre_then_popularity_quality",
        mode_reason="structured_filter_only",
    )
    assert [item["matchPct"] for item in docs] == sorted(
        [item["matchPct"] for item in docs], reverse=True
    )
    assert docs[0]["rankPosition"] == 1
    assert docs[0]["requestedSearchMode"] == "hybrid"
    assert docs[0]["effectiveSearchMode"] == "fts"
    assert docs[0]["recommendationSource"] == "fts_structured"
    assert "not a probability" in docs[0]["matchScoreMeaning"]


def test_vector_explanation_does_not_claim_random_fts_fields() -> None:
    service = bare_catalogue()
    intent = service._analyse_query("atmospheric and cerebral")
    hit = {
        "fragments": {
            "castNames": ["Matthew Gademske"],
            "directorNames": ["Marcel Walz"],
            "genres": ["Science Fiction"],
        }
    }
    explanation = service._match_explanation(
        {"title": "Master of the Universe", "genres": ["Science Fiction"]},
        hit,
        intent,
        effective_mode="vector",
        requested_mode="vector",
    )
    assert explanation["summary"].startswith("Semantic similarity")
    assert explanation["matchedFields"] == [{
        "field": "semanticQuery",
        "value": "atmospheric and cerebral",
        "matchType": "vector_similarity",
    }]


def test_hybrid_only_keeps_genuine_highlight_fragments() -> None:
    service = bare_catalogue()
    intent = service._analyse_query("space adventure")
    hit = {
        "fragments": {
            "castNames": ["Matthew Gademske"],
            "overview": ["A <mark>space</mark> voyage"],
            "keywords": ["mockbuster"],
        }
    }
    explanation = service._match_explanation(
        {"title": "Example", "overview": "A space voyage", "keywords": ["mockbuster"]},
        hit,
        intent,
        effective_mode="hybrid",
        requested_mode="hybrid",
    )
    fields = [item["field"] for item in explanation["matchedFields"]]
    assert "overview" in fields
    assert "semanticQuery" in fields
    assert "castNames" not in fields
    assert "keywords" not in fields


def test_exact_genre_explanation_reports_effective_fts() -> None:
    service = bare_catalogue()
    intent = service._analyse_query("Thriller")
    explanation = service._match_explanation(
        {"genres": ["Thriller", "Crime"]},
        None,
        intent,
        effective_mode="fts",
        requested_mode="hybrid",
    )
    assert explanation["summary"] == "Matched genre: Thriller"
    assert explanation["requestedMode"] == "hybrid"
    assert explanation["effectiveMode"] == "fts"
    assert all(item["field"] != "castNames" for item in explanation["matchedFields"])


def test_named_title_can_be_added_to_likes_without_model_routing() -> None:
    action = GovernedAgentService.extract_action_request(
        "Add The Odyssey to my likes", previous={}
    )
    assert action == {
        "action": "like_title",
        "reference": "The Odyssey",
        "resolvedTitleId": None,
        "resolvedTitle": None,
        "source": "natural_title_reference",
    }
    assert GovernedAgentService.action_to_interaction("like_title") == "like"


def test_ordinal_like_action_handles_liked_theames_typo() -> None:
    previous = {
        "resultTitleIds": ["movie::odyssey", "movie::silence"],
        "resultTitles": ["The Odyssey", "The Silence of the Lambs"],
    }
    action = GovernedAgentService.extract_action_request(
        "Add The first one to my liked theames", previous=previous
    )
    assert action["action"] == "like_title"
    assert action["resolvedTitleId"] == "movie::odyssey"
    assert action["resolvedTitle"] == "The Odyssey"
    assert action["source"] == "previous_results"


def test_ui_explains_search_modes_and_relative_score() -> None:
    root = Path(__file__).resolve().parents[1]
    page = (root / "ui/static/index.html").read_text()
    script = (root / "ui/static/app.js").read_text()
    assert "How the search modes work" in page
    assert "Structured shortcut" in page
    assert "requestedSearchMode" in script
    assert "% relative rank" in script
    assert "not a probability" in script

class _Response:
    def __init__(self, hits):
        self._hits = hits

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"hits": self._hits}


class _SearchClient:
    def __init__(self, hits):
        self.hits = hits
        self.payload = None

    def post(self, _path, *, json, timeout):
        self.payload = json
        return _Response(self.hits)


def test_thriller_requested_hybrid_uses_structured_fts_and_consistent_cards() -> None:
    service = bare_catalogue()
    service._search_client = _SearchClient([
        {"id": "low-pop", "score": 9.0, "fragments": {"genres": ["<mark>Thriller</mark>"]}},
        {"id": "high-pop", "score": 7.0, "fragments": {"genres": ["<mark>Thriller</mark>"]}},
    ])
    docs = {
        "low-pop": {"id": "low-pop", "title": "Low Pop", "genres": ["Thriller"], "keywords": [], "popularity": 20, "voteAverage": 8.0},
        "high-pop": {"id": "high-pop", "title": "High Pop", "genres": ["Thriller"], "keywords": [], "popularity": 90, "voteAverage": 7.0},
    }
    service._fetch_hits = lambda hits, content_type=None: [dict(docs[hit["id"]]) for hit in hits]
    service._write_trace = lambda *_args, **_kwargs: None

    result = service.search(query="Thriller", mode="hybrid", login_id=None, purpose="search", limit=10)

    assert [item["id"] for item in result["results"]] == ["high-pop", "low-pop"]
    assert [item["matchPct"] for item in result["results"]] == sorted(
        [item["matchPct"] for item in result["results"]], reverse=True
    )
    assert result["trace"]["requestedMode"] == "hybrid"
    assert result["trace"]["effectiveMode"] == "fts"
    assert result["trace"]["modeReason"] == "structured_filter_only"
    assert "knn" not in service._search_client.payload
    assert result["results"][0]["matchExplanation"]["summary"] == "Matched genre: Thriller"
    assert result["results"][0]["matchExplanation"]["matchedFields"] == [
        {"field": "genres", "value": "Thriller", "matchType": "exact"}
    ]


def test_like_action_has_governed_tool_spec_and_reply() -> None:
    spec = next(item for item in GovernedAgentService.TOOL_SPECS if item.name == "record_like")
    assert spec.mutating is True
    assert spec.components == ("Data/KV", "Agent Memory")
    reply = GovernedAgentService.render_action_reply(
        "like_title", {"title": "The Odyssey"}, {"includedInPlan": True}
    )
    assert "Added The Odyssey to your liked titles" in reply



def test_vector_mode_uses_knn_prefilter_not_hybrid_query_for_content_type() -> None:
    service = bare_catalogue()
    service._search_client = _SearchClient([])
    service.embed_with_trace = lambda _query: ([0.1, 0.2], {
        "embeddingModel": "test",
        "embeddingModelCalls": 1,
        "embeddingCacheHits": 0,
        "embeddingTimeouts": 0,
        "embeddingElapsedMs": 1.0,
        "embeddingError": None,
    })
    service._fetch_hits = lambda _hits, content_type=None: []
    service._write_trace = lambda *_args, **_kwargs: None

    service.search(
        query="atmospheric and cerebral",
        mode="vector",
        login_id=None,
        content_type="movie",
        purpose="search",
        limit=10,
    )

    payload = service._search_client.payload
    assert "query" not in payload
    assert payload["knn"][0]["filter"] == {"term": "movie", "field": "contentType"}


def test_hybrid_mode_applies_structured_filters_to_both_lexical_and_vector_routes() -> None:
    service = bare_catalogue()
    service._search_client = _SearchClient([])
    service.embed_with_trace = lambda _query: ([0.1, 0.2], {
        "embeddingModel": "test",
        "embeddingModelCalls": 1,
        "embeddingCacheHits": 0,
        "embeddingTimeouts": 0,
        "embeddingElapsedMs": 1.0,
        "embeddingError": None,
    })
    service._fetch_hits = lambda _hits, content_type=None: []
    service._fallback_query = lambda **_kwargs: []
    service._write_trace = lambda *_args, **_kwargs: None

    service.search(
        query="atmospheric thriller movie",
        mode="hybrid",
        login_id=None,
        content_type="movie",
        purpose="search",
        limit=10,
    )

    payload = service._search_client.payload
    query_filters = payload["query"]["conjuncts"]
    assert {"term": "Thriller", "field": "genres"} in query_filters
    assert {"term": "movie", "field": "contentType"} in query_filters
    knn_filters = payload["knn"][0]["filter"]["conjuncts"]
    assert {"term": "Thriller", "field": "genres"} in knn_filters
    assert {"term": "movie", "field": "contentType"} in knn_filters
