from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from ui.app.catalogue_service import CatalogueService


def viewer_context() -> dict[str, Any]:
    return {
        "profile": {
            "dislikedGenres": ["Horror"],
            "dislikedThemes": [],
            "dislikedPeople": [],
        },
        "liked": set(),
        "disliked": set(),
        "watchlist": set(),
        "watched": set(),
        "completed": set(),
        "topPickExclusions": set(),
        "heroExclusions": set(),
    }


def test_generic_profile_plan_forces_fast_fts_retrieval() -> None:
    service = object.__new__(CatalogueService)
    captured: dict[str, Any] = {}

    def recommend(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"results": [], "trace": {}, "profile": {}}

    service.recommend_for_profile = recommend  # type: ignore[method-assign]

    service.query_catalogue_plan(
        login_id="sai",
        search_mode="hybrid",
        plan={
            "intent": "recommendation",
            "contentType": "movie",
            "filters": {},
            "semanticQuery": None,
            "personalise": True,
            "limit": 12,
        },
    )

    assert captured["mode"] == "fts"


def test_personalised_free_form_plan_keeps_hybrid_retrieval() -> None:
    service = object.__new__(CatalogueService)
    captured: dict[str, Any] = {}

    def recommend(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"results": [], "trace": {}, "profile": {}}

    service.recommend_for_profile = recommend  # type: ignore[method-assign]

    service.query_catalogue_plan(
        login_id="sai",
        search_mode="hybrid",
        plan={
            "intent": "recommendation",
            "contentType": "movie",
            "filters": {},
            "semanticQuery": "atmospheric political",
            "personalise": True,
            "limit": 12,
        },
    )

    assert captured["mode"] == "hybrid"


def test_sci_fi_browse_removes_unrequested_horror_cross_genre() -> None:
    service = object.__new__(CatalogueService)
    service._apply_entitlement_policy = (  # type: ignore[method-assign]
        lambda docs, *_args, **_kwargs: docs
    )
    context = viewer_context()
    safe = {
        "id": "safe",
        "genres": ["Science Fiction", "Adventure"],
        "keywords": [],
        "castNames": [],
        "directorNames": [],
    }
    horror = {
        "id": "horror",
        "genres": ["Science Fiction", "Horror"],
        "keywords": [],
        "castNames": [],
        "directorNames": [],
    }

    results = service._exclude_by_purpose(
        [safe, horror],
        "sai",
        "search",
        viewer_signals_context=context,
    )

    assert [item["id"] for item in results] == ["safe"]


def test_explicit_disliked_genre_can_still_be_a_one_turn_browse_override() -> None:
    service = object.__new__(CatalogueService)
    service._apply_entitlement_policy = (  # type: ignore[method-assign]
        lambda docs, *_args, **_kwargs: docs
    )
    horror = {
        "id": "horror",
        "genres": ["Horror"],
        "keywords": [],
        "castNames": [],
        "directorNames": [],
    }

    results = service._exclude_by_purpose(
        [horror],
        "sai",
        "search",
        ignored_disliked_genres=["Horror"],
        viewer_signals_context=viewer_context(),
    )

    assert [item["id"] for item in results] == ["horror"]


def test_recommendations_keep_allowed_purchase_titles_but_not_policy_denials() -> None:
    service = object.__new__(CatalogueService)
    service.settings = SimpleNamespace(
        enable_entitlement_filtering=True,
        default_region_code="GB",
        default_subscription_tier="standard",
        default_parental_rating="18",
    )
    service.entitlements = object()
    service.ensure_entitlement = lambda _login_id: {
        "regionCode": "GB",
        "subscriptionTier": "standard",
        "maxParentalRating": "18",
    }
    service.get_profile = lambda _login_id: {
        "dislikedGenres": [],
        "dislikedThemes": [],
        "dislikedPeople": [],
    }
    purchase = {
        "id": "purchase",
        "title": "Purchase Film",
        "genres": ["Drama"],
        "keywords": [],
        "availability": {
            "available": True,
            "regions": ["GB"],
            "offerType": "purchase",
            "minimumTier": "premium",
        },
    }
    denied = {
        "id": "denied",
        "title": "Region Denied",
        "genres": ["Drama"],
        "keywords": [],
        "availability": {
            "available": True,
            "regions": ["US"],
            "offerType": "included",
            "minimumTier": "standard",
        },
    }

    results = service._apply_entitlement_policy(
        [purchase, denied],
        "sai",
        remove_denied=True,
        require_included=False,
    )

    assert [item["id"] for item in results] == ["purchase"]
    assert results[0]["includedInPlan"] is False


def test_search_hit_hydration_uses_one_multi_get_and_preserves_order() -> None:
    service = object.__new__(CatalogueService)
    calls: list[list[str]] = []

    class Titles:
        def get_multi(self, keys: list[str]) -> Any:
            calls.append(keys)
            return SimpleNamespace(
                results={
                    key: SimpleNamespace(
                        content_as={
                            dict: {
                                "id": key,
                                "title": key.upper(),
                                "contentType": "movie",
                            }
                        }
                    )
                    for key in keys
                }
            )

    service.titles = Titles()
    service.settings = SimpleNamespace(
        tmdb_image_base="https://images/",
        tmdb_backdrop_base="https://backdrops/",
    )

    results = service._fetch_hits(
        [{"id": "b"}, {"id": "a"}, {"id": "b"}],
        content_type="movie",
    )

    assert calls == [["b", "a"]]
    assert [item["id"] for item in results] == ["b", "a"]


def test_search_failure_opens_circuit_and_second_request_falls_back_immediately() -> None:
    service = object.__new__(CatalogueService)
    service.settings = SimpleNamespace(
        search_result_limit=20,
        embedding_model="test",
        content_bucket="streaming",
        catalogue_scope="catalogue",
        catalogue_search_index="titles",
        search_query_timeout_seconds=5.0,
        search_interactive_timeout_seconds=1.5,
        search_circuit_breaker_seconds=20.0,
    )
    service._showcase_faults = {}
    service._search_circuit_open_until = 0.0
    service._search_failure_count = 0
    calls = 0

    class SearchClient:
        def post(self, *_args: Any, **_kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            raise TimeoutError("search unavailable")

    service._search_client = SearchClient()
    service._fallback_query = lambda **_kwargs: [  # type: ignore[method-assign]
        {
            "id": "sf",
            "title": "Science Film",
            "contentType": "movie",
            "genres": ["Science Fiction"],
            "keywords": [],
            "castNames": [],
            "directorNames": [],
            "popularity": 10,
            "voteAverage": 8,
        }
    ]
    service._write_trace = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    first = service.search(
        query="show me sci-fi movies",
        mode="fts",
        login_id=None,
        content_type="movie",
        purpose="search",
    )
    second = service.search(
        query="show me sci-fi movies",
        mode="fts",
        login_id=None,
        content_type="movie",
        purpose="search",
    )

    assert calls == 1
    assert first["trace"]["searchCircuitOpen"] is True
    assert second["trace"]["searchCircuitOpen"] is True
    assert [item["id"] for item in second["results"]] == ["sf"]
