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

from ui.app.catalogue_service import CatalogueService
from ui.app.config import Settings
from ui.app.llm_service import LocalModelService


def bare_catalogue() -> CatalogueService:
    service = object.__new__(CatalogueService)
    service.settings = Settings()
    return service


def test_region_question_and_statement_are_deterministic() -> None:
    assert LocalModelService.is_region_question("what region do i live in?") is True
    assert LocalModelService.extract_region_statement("I live in the US region") == "US"
    assert LocalModelService.extract_region_statement("My region is United Kingdom") == "GB"
    assert LocalModelService.render_region_reply({"regionCode": "US"}).startswith(
        "Your viewer account is currently set to the US region"
    )


def test_watch_history_count_and_contextual_similarity_are_classified() -> None:
    assert LocalModelService.is_watch_history_count_question(
        "how many movies have i watched?"
    ) is True
    assert LocalModelService.is_contextual_similarity_request("Suggest similar movies") is True
    assert LocalModelService.render_watch_history_count([{"id": "movie::1"}], "movie") == (
        "You have 1 watched movie recorded in your viewing history."
    )


def test_top_rated_catalogue_question_is_not_left_to_the_llm() -> None:
    parsed = LocalModelService.extract_top_rated_catalogue_question(
        "Which is the top rated movie in the catalogue?"
    )
    assert parsed == {"contentType": "movie", "kind": "movie"}
    answer = LocalModelService.render_top_rated_title(
        {"title": "Swapped", "releaseYear": 2026, "voteAverage": 8.9, "voteCount": 142},
        "movie",
    )
    assert "Swapped (2026)" in answer
    assert "8.9/10" in answer


def test_contextual_and_named_title_affinity_questions() -> None:
    assert LocalModelService.extract_title_affinity_question("is this something i like?") == {
        "title": "this",
        "contextual": True,
    }
    assert LocalModelService.extract_title_affinity_question("Will i like Swapped?") == {
        "title": "Swapped",
        "contextual": False,
    }


def test_top_rated_title_orders_catalogue_rating_then_votes() -> None:
    service = bare_catalogue()
    captured = {}

    def query(statement, params):
        captured["statement"] = statement
        captured["params"] = params
        return [{"id": "movie::swapped", "title": "Swapped", "voteAverage": 8.9, "voteCount": 142}]

    service._query_titles = query
    title = service.top_rated_title(content_type="movie")
    assert title and title["title"] == "Swapped"
    assert "voteAverage DESC" in captured["statement"]
    assert captured["params"] == {"contentType": "movie"}


def test_title_affinity_uses_verified_profile_evidence() -> None:
    service = bare_catalogue()
    service.get_profile = lambda _login: {
        "likedTitleIds": [],
        "dislikedTitleIds": [],
        "preferredGenres": ["Science Fiction", "Thriller"],
        "dislikedGenres": ["Horror"],
        "preferredThemes": ["Atmospheric"],
        "dislikedThemes": [],
        "preferredPeople": [],
        "dislikedPeople": [],
        "preferredLanguages": ["English"],
        "avoidGraphicViolence": False,
        "avoidAdultContent": False,
    }
    title = {
        "id": "movie::swapped",
        "title": "Swapped",
        "genres": ["Science Fiction", "Thriller"],
        "keywords": ["atmospheric"],
        "originalLanguage": "en",
    }
    result = service.title_affinity("sai", title)
    assert result["verdict"] == "likely"
    assert result["score"] > 0
    assert LocalModelService.render_title_affinity(title, result).startswith(
        "Based on your verified viewer profile"
    )


def test_region_update_writes_authoritative_entitlement_document() -> None:
    service = bare_catalogue()
    stored = {}

    class Collection:
        def upsert(self, key, doc):
            stored["key"] = key
            stored["doc"] = doc

    service.entitlements = Collection()
    service.ensure_entitlement = lambda _login: {
        "viewerId": "sai", "regionCode": "GB", "subscriptionTier": "standard"
    }
    service.invalidate_home_cache = lambda _login: None
    updated = service.update_entitlement_region("sai", "US")
    assert updated["regionCode"] == "US"
    assert stored["key"] == "entitlement::sai"
    assert stored["doc"]["regionCode"] == "US"


def test_main_contains_context_persistence_for_history_and_titles() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "ui/app/main.py").read_text()
    assert 'intent="watch_history_count_query"' in source
    assert '"lastGroundedTitle": str(history_items[0].get("title"))' in source
    assert 'intent="viewer_title_affinity_query"' in source
    assert 'intent="update_viewer_region"' in source
    assert 'intent="top_rated_catalogue_title"' in source
