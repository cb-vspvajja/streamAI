from __future__ import annotations

import sys
import types
from pathlib import Path

if "couchbase" not in sys.modules:
    couchbase = types.ModuleType("couchbase")
    exceptions = types.ModuleType("couchbase.exceptions")
    n1ql = types.ModuleType("couchbase.n1ql")
    options = types.ModuleType("couchbase.options")
    exceptions.DocumentNotFoundException = type("DocumentNotFoundException", (Exception,), {})
    n1ql.QueryScanConsistency = types.SimpleNamespace(REQUEST_PLUS="request_plus")
    options.QueryOptions = object
    sys.modules.update({
        "couchbase": couchbase,
        "couchbase.exceptions": exceptions,
        "couchbase.n1ql": n1ql,
        "couchbase.options": options,
    })

from ui.app.catalogue_service import CatalogueService
from ui.app.config import Settings
from ui.app.llm_service import LocalModelService


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"hits": []}


class _SearchClient:
    def __init__(self) -> None:
        self.payload = None

    def post(self, _path, *, json, timeout):
        self.payload = json
        return _Response()


def model() -> LocalModelService:
    return LocalModelService(Settings())


def bare_catalogue() -> CatalogueService:
    service = object.__new__(CatalogueService)
    service.settings = Settings()
    return service


def test_only_children_movies_is_an_exclusive_family_preference() -> None:
    service = model()
    try:
        message = "I only like children movies"
        assert service.should_capture_preference(message) is True
        assert service.is_preference_only_statement(message) is True
        assert service.is_recommendation_request(message) is False
        assert service.extract_preferences_fast(message) == [
            {
                "kind": "genre",
                "value": "Family",
                "sentiment": "like",
                "exclusive": True,
                "fact": "Viewer exclusively prefers Family content.",
                "source": "explicit_viewer_statement",
            }
        ]
    finally:
        service.close()


def test_exclusive_content_preference_replaces_positive_tastes_only() -> None:
    service = bare_catalogue()
    profile = CatalogueService._profile_defaults("sai")
    profile.update({
        "preferredGenres": ["Science Fiction", "Thriller"],
        "preferredThemes": ["Atmospheric"],
        "dislikedGenres": ["Horror"],
    })

    class Profiles:
        saved = None

        def upsert(self, _key, value) -> None:
            self.saved = value

    profiles = Profiles()
    service.profiles = profiles
    service.get_profile = lambda _login_id: profile
    service._write_profile_snapshot = lambda *_args, **_kwargs: None
    service.invalidate_home_cache = lambda _login_id: None

    result = service.apply_preferences(
        "sai",
        [{
            "kind": "genre",
            "value": "Family",
            "sentiment": "like",
            "exclusive": True,
            "fact": "Viewer exclusively prefers Family content.",
        }],
    )

    assert result["preferredGenres"] == ["Family"]
    assert result["preferredThemes"] == []
    assert result["dislikedGenres"] == ["Horror"]
    assert result["exclusivePreferenceMode"] == "content"
    assert profiles.saved["preferredGenres"] == ["Family"]


def test_children_and_kids_are_normalised_to_family_genre() -> None:
    service = bare_catalogue()
    assert service.analyse_request("show me children movies")["genres"] == ["Family"]
    assert service.analyse_request("find kid-friendly films")["genres"] == ["Family"]


def test_semantic_genre_query_broadens_to_exact_inventory_when_literal_term_is_too_narrow() -> None:
    service = bare_catalogue()
    service._search_client = _SearchClient()
    service._fetch_hits = lambda _hits, content_type=None: []
    calls: list[dict] = []

    def fallback(**kwargs):
        intent = kwargs["intent"]
        calls.append(intent)
        if intent.get("terms"):
            return []
        return [{
            "id": "sf-1",
            "title": "Interstellar",
            "contentType": "movie",
            "genres": ["Science Fiction", "Adventure", "Drama"],
            "keywords": ["space"],
            "castNames": [],
            "directorNames": [],
            "popularity": 90,
            "voteAverage": 8.5,
        }]

    service._fallback_query = fallback
    service.embed_with_trace = lambda _query: ([0.1, 0.2], {
        "embeddingModel": "test",
        "embeddingModelCalls": 1,
        "embeddingCacheHits": 0,
        "embeddingTimeouts": 0,
        "embeddingElapsedMs": 1.0,
        "embeddingError": None,
    })
    service.get_profile = lambda _login_id: {"dislikedGenres": []}
    service._exclude_by_purpose = lambda docs, *_args, **_kwargs: docs
    service._write_trace = lambda *_args, **_kwargs: None

    result = service.search(
        query="Suggest an atmospheric science-fiction film.",
        mode="hybrid",
        login_id="sai",
        purpose="recommendation",
        limit=10,
    )

    assert [item["id"] for item in result["results"]] == ["sf-1"]
    assert len(calls) == 2
    assert calls[0]["terms"] == ["atmospheric"]
    assert calls[1]["terms"] == []
    assert calls[1]["genres"] == ["Science Fiction"]
    assert result["trace"]["mode"] == "sqlpp_structured_broadening"
    assert result["trace"]["structuredBroadeningApplied"] is True


def test_zero_eligible_results_distinguishes_inventory_from_viewer_availability() -> None:
    reply = LocalModelService.render_catalogue_recommendations(
        "Suggest an atmospheric science-fiction film.",
        [],
        {
            "structuredGenres": ["Science Fiction"],
            "structuredContentType": "movie",
            "catalogueInventoryCount": 14,
        },
    )
    assert "There are 14 Science Fiction movies in the catalogue" in reply
    assert "region, subscription and preference filters" in reply
    assert "none are available in the current catalogue" not in reply


def test_consumer_copy_is_scrubbed_in_server_and_live_browser_stream() -> None:
    service = model()
    try:
        text = service.sanitise_consumer_language(
            "Grounded Couchbase catalogue results; recorded in Couchbase. "
            "Couchbase MCP Server used Couchbase Agent Catalog."
        )
        assert "Couchbase" not in text
        assert text == (
            "Verified catalogue results; recorded in your viewing history. "
            "data access service used governed tool catalogue."
        )
    finally:
        service.close()

    app_js = (Path(__file__).resolve().parents[1] / "ui/static/app.js").read_text()
    assert "function consumerSafeText" in app_js
    assert "assistant.textContent = consumerSafeText" in app_js
