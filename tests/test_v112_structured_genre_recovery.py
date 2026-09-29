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

from ui.app.catalogue_service import CatalogueService
from ui.app.config import Settings
from ui.app.llm_service import LocalModelService


def bare_catalogue() -> CatalogueService:
    service = object.__new__(CatalogueService)
    service.settings = Settings()
    return service


def test_horror_movies_becomes_exact_structured_request() -> None:
    service = bare_catalogue()
    intent = service._analyse_query("suggest me some horror movies")
    assert intent["genres"] == ["Horror"]
    assert intent["contentType"] == "movie"
    assert intent["cleaned"] == ""
    lexical = service._lexical_query(intent)
    assert lexical["disjuncts"] == []


def test_structured_validation_rejects_non_horror_and_tv() -> None:
    service = bare_catalogue()
    intent = service._analyse_query("suggest me some horror movies")
    assert service._matches_structured_intent(
        {"contentType": "movie", "genres": ["Comedy", "Horror"]}, intent, "movie"
    )
    assert not service._matches_structured_intent(
        {"contentType": "movie", "genres": ["Family", "Comedy"]}, intent, "movie"
    )
    assert not service._matches_structured_intent(
        {"contentType": "tv", "genres": ["Horror"]}, intent, "movie"
    )


def test_explicit_horror_request_can_override_horror_dislike_only_for_turn() -> None:
    service = bare_catalogue()
    service.viewer_signals = lambda _login_id: {
        "profile": {
            "dislikedGenres": ["Horror"],
            "dislikedThemes": [],
            "dislikedPeople": [],
            "avoidGraphicViolence": False,
        },
        "disliked": set(),
        "liked": set(),
        "watched": set(),
        "watchlist": set(),
        "topPickExclusions": set(),
    }
    horror = {"id": "h1", "genres": ["Horror"], "keywords": []}
    assert service._exclude_by_purpose([horror], "sai", "recommendation") == []
    assert service._exclude_by_purpose(
        [horror], "sai", "recommendation", ignored_disliked_genres=["Horror"]
    ) == [horror]


def test_complaint_is_not_a_new_catalogue_search_or_preference() -> None:
    message = "these do not look like horror movies"
    assert LocalModelService.is_catalogue_result_complaint(message)
    assert not LocalModelService.is_recommendation_request(message)
    service = LocalModelService(Settings())
    try:
        assert not service.should_capture_preference(message)
    finally:
        service.close()


def test_genre_explanation_uses_actual_value() -> None:
    service = bare_catalogue()
    intent = service._analyse_query("suggest me some horror movies")
    explanation = service._match_explanation(
        {"genres": ["Comedy", "Horror"], "contentType": "movie"}, None, intent
    )
    assert explanation["summary"] == "Matched genre: Horror"
    assert explanation["matchedFields"][0] == {
        "field": "genres", "value": "Horror", "matchType": "exact"
    }


def test_rendered_answer_has_no_duplicate_cards_footer() -> None:
    answer = LocalModelService.render_catalogue_recommendations(
        "suggest me some horror movies",
        [{
            "title": "Valid Horror",
            "releaseYear": 2024,
            "genres": ["Horror"],
            "matchExplanation": {"summary": "Matched genre: Horror"},
        }],
        {
            "structuredGenres": ["Horror"],
            "structuredContentType": "movie",
            "temporaryPreferenceOverrides": ["disliked genre: Horror"],
        },
    )
    assert "temporarily overrides" in answer
    assert "Valid Horror" in answer
    assert "cards below" not in answer.lower()


def test_retry_response_reuses_original_query() -> None:
    answer = LocalModelService.render_catalogue_retry(
        "suggest me some horror movies",
        [{
            "title": "Valid Horror",
            "genres": ["Horror"],
            "matchExplanation": {"summary": "Matched genre: Horror"},
        }],
        {"structuredGenres": ["Horror"], "structuredContentType": "movie"},
    )
    assert "reran the original request" in answer
    assert "suggest me some horror movies" in answer

class _FakeResponse:
    def __init__(self, hits):
        self._hits = hits

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"hits": self._hits}


class _FakeSearchClient:
    def __init__(self, hits):
        self.hits = hits
        self.payload = None

    def post(self, _path, *, json, timeout):
        self.payload = json
        return _FakeResponse(self.hits)


def test_live_search_shape_returns_only_exact_horror_movies_and_records_override() -> None:
    service = bare_catalogue()
    service._search_client = _FakeSearchClient([
        {"id": "mario", "score": 8.0},
        {"id": "horror", "score": 7.0},
        {"id": "horror-tv", "score": 6.0},
    ])
    docs = {
        "mario": {"id": "mario", "contentType": "movie", "genres": ["Family", "Comedy"], "keywords": []},
        "horror": {"id": "horror", "contentType": "movie", "genres": ["Horror", "Thriller"], "keywords": [], "popularity": 10},
        "horror-tv": {"id": "horror-tv", "contentType": "tv", "genres": ["Horror"], "keywords": []},
    }
    service._fetch_hits = lambda hits, content_type=None: [dict(docs[hit["id"]]) for hit in hits]
    service.get_profile = lambda _login_id: {"dislikedGenres": ["Horror"]}
    service.viewer_signals = lambda _login_id: {
        "profile": {
            "dislikedGenres": ["Horror"],
            "dislikedThemes": [],
            "dislikedPeople": [],
            "avoidGraphicViolence": False,
        },
        "disliked": set(),
        "liked": set(),
        "watched": set(),
        "watchlist": set(),
        "topPickExclusions": set(),
    }
    service._write_trace = lambda *_args, **_kwargs: None

    result = service.search(
        query="suggest me some horror movies",
        mode="fts",
        login_id="sai",
        purpose="recommendation",
        limit=12,
    )

    assert [item["id"] for item in result["results"]] == ["horror"]
    assert result["trace"]["structuredGenres"] == ["Horror"]
    assert result["trace"]["structuredContentType"] == "movie"
    assert result["trace"]["temporaryPreferenceOverrides"] == ["disliked genre: Horror"]
    conjuncts = service._search_client.payload["query"]["conjuncts"]
    assert {"term": "Horror", "field": "genres"} in conjuncts
    assert {"term": "movie", "field": "contentType"} in conjuncts
