from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from typing import Any

if "couchbase" not in sys.modules:
    couchbase = types.ModuleType("couchbase")
    auth = types.ModuleType("couchbase.auth")
    cluster = types.ModuleType("couchbase.cluster")
    exceptions = types.ModuleType("couchbase.exceptions")
    n1ql = types.ModuleType("couchbase.n1ql")
    options = types.ModuleType("couchbase.options")
    auth.PasswordAuthenticator = object
    cluster.Cluster = object
    exceptions.DocumentNotFoundException = type(
        "DocumentNotFoundException", (Exception,), {}
    )
    n1ql.QueryScanConsistency = types.SimpleNamespace(
        REQUEST_PLUS="request_plus"
    )
    options.ClusterOptions = object
    options.QueryOptions = object
    sys.modules.update(
        {
            "couchbase": couchbase,
            "couchbase.auth": auth,
            "couchbase.cluster": cluster,
            "couchbase.exceptions": exceptions,
            "couchbase.n1ql": n1ql,
            "couchbase.options": options,
        }
    )

from ui.app.catalogue_service import CatalogueService
from ui.app.planner_service import AssistantPlannerService


def planner_settings(**overrides: Any) -> SimpleNamespace:
    values = {
        "assistant_planner_version": "3.0",
        "assistant_tool_schema_version": "2.1",
        "assistant_planner_model_enabled": False,
        "chat_provider": "disabled",
        "plan_cache_enabled": True,
        "plan_cache_semantic_enabled": True,
        "plan_cache_semantic_threshold": 0.92,
        "plan_cache_semantic_probe_timeout_seconds": 0.75,
        "plan_cache_candidate_limit": 5,
        "assistant_planner_timeout_seconds": 2.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeModel:
    @staticmethod
    def is_personalised_recommendation_request(message: str) -> bool:
        lower = message.lower()
        return "will i like" in lower or "suggest some good" in lower

    @staticmethod
    def extract_catalogue_analytics_request(_message: str) -> None:
        return None

    @staticmethod
    def extract_catalogue_count_question(_message: str) -> None:
        return None

    @staticmethod
    def estimate_tokens(message: str) -> int:
        return max(len(message) // 4, 1)

    async def plan_catalogue_request(self, *_args: Any, **_kwargs: Any):
        raise AssertionError("The model should not be called in this test")


class FakeCatalogue:
    def __init__(self) -> None:
        self.exact: dict[str, Any] | None = None
        self.semantic: list[dict[str, Any]] = []
        self.stored: list[dict[str, Any]] = []
        self.marked: list[str] = []
        self.semantic_calls = 0

    @staticmethod
    def normalise_natural_query(query: str) -> str:
        return (
            query.lower()
            .replace("sequals", "sequel")
            .replace("sequels", "sequel")
        )

    def get_cached_plan_exact(self, _query: str):
        return self.exact

    def find_cached_plans_semantic(self, _query: str, *, limit: int):
        self.semantic_calls += 1
        assert limit == 5
        return self.semantic

    def cache_assistant_plan(self, **kwargs: Any):
        self.stored.append(kwargs)
        return {"stored": True, "cacheDocumentId": "plan::new"}

    def mark_plan_cache_hit(self, document_id: str) -> None:
        self.marked.append(document_id)


def intent(
    *,
    content_type: str | None = "movie",
    genres: list[str] | None = None,
    cleaned: str = "",
    request_language: bool = True,
    generic: bool = False,
) -> dict[str, Any]:
    return {
        "contentType": content_type,
        "genres": genres or [],
        "country": None,
        "cleaned": cleaned,
        "requestLanguage": request_language,
        "genericRecommendation": generic,
        "personalisedRecommendation": generic,
    }


def build_planner(
    catalogue: FakeCatalogue | None = None, **settings: Any
) -> AssistantPlannerService:
    return AssistantPlannerService(
        planner_settings(**settings),
        catalogue or FakeCatalogue(),  # type: ignore[arg-type]
        FakeModel(),  # type: ignore[arg-type]
    )


def test_top_five_rated_movies_becomes_ranked_plan() -> None:
    result = asyncio.run(
        build_planner().plan(
            "show me 5 top rated movies",
            catalogue_intent=intent(generic=True),
        )
    )
    plan = result["plan"]
    assert plan["intent"] == "catalogue_query"
    assert plan["contentType"] == "movie"
    assert plan["sort"] == "rating_desc"
    assert plan["limit"] == 5
    assert plan["personalise"] is False


def test_relative_release_date_stays_symbolic_in_cached_plan() -> None:
    result = asyncio.run(
        build_planner().plan(
            "show me films released last year",
            catalogue_intent=intent(cleaned="released last year"),
        )
    )
    plan = result["plan"]
    assert plan["filters"]["releasePeriod"] == "LAST_YEAR"
    assert plan["filters"]["releaseYear"] is None
    assert plan["sort"] == "release_year_desc"


def test_new_movies_i_will_like_combines_freshness_and_personalisation() -> None:
    result = asyncio.run(
        build_planner().plan(
            "what new movies will i like",
            catalogue_intent=intent(
                cleaned="will like", generic=False
            ),
        )
    )
    plan = result["plan"]
    assert plan["intent"] == "recommendation"
    assert plan["personalise"] is True
    assert plan["sort"] == "personalised"
    assert plan["filters"]["releasePeriod"] == "RECENT"


def test_specific_sequel_request_is_not_hijacked_by_personalisation() -> None:
    result = asyncio.run(
        build_planner().plan(
            "show me movies that are sequals",
            catalogue_intent=intent(cleaned="sequel"),
        )
    )
    plan = result["plan"]
    assert plan["intent"] == "catalogue_query"
    assert plan["personalise"] is False
    assert plan["semanticQuery"] == "sequel"


def test_generic_recommendation_skips_semantic_probe_and_planner_model() -> None:
    catalogue = FakeCatalogue()
    result = asyncio.run(
        build_planner(catalogue).plan(
            "Suggest some good movies",
            catalogue_intent=intent(
                cleaned="",
                generic=True,
            ),
        )
    )

    assert result["trace"]["planSource"] == "deterministic"
    assert result["trace"]["plannerLlmInvoked"] is False
    assert result["plan"]["personalise"] is True
    assert result["plan"]["semanticQuery"] is None
    assert catalogue.semantic_calls == 0
    assert catalogue.stored[0]["semantic_eligible"] is False
    assert result["cachePromotion"] is None


def test_structured_genre_browse_skips_semantic_probe_and_planner_model() -> None:
    catalogue = FakeCatalogue()
    result = asyncio.run(
        build_planner(catalogue).plan(
            "Show me sci-fi movies",
            catalogue_intent=intent(
                genres=["Science Fiction"],
                cleaned="",
            ),
        )
    )

    assert result["trace"]["planSource"] == "deterministic"
    assert result["trace"]["plannerLlmInvoked"] is False
    assert catalogue.semantic_calls == 0


def test_title_contains_war_does_not_become_war_genre() -> None:
    result = asyncio.run(
        build_planner().plan(
            "Are there any movies that have War in the name",
            catalogue_intent=intent(genres=["War"], cleaned="war"),
        )
    )
    plan = result["plan"]
    assert plan["filters"]["titleContains"] == "war"
    assert plan["filters"]["genres"] == []
    assert plan["semanticQuery"] is None


def test_exact_plan_cache_avoids_planner_model() -> None:
    catalogue = FakeCatalogue()
    catalogue.exact = {
        "normalisedQuery": "show films released last year",
        "cacheDocumentId": "plan::exact",
        "planTemplate": {
            "intent": "catalogue_query",
            "contentType": "movie",
            "filters": {"releasePeriod": "LAST_YEAR"},
            "sort": "release_year_desc",
            "limit": 12,
            "confidence": 1,
        },
    }
    result = asyncio.run(
        build_planner(catalogue).plan(
            "show films released last year",
            catalogue_intent=intent(cleaned="released last year"),
        )
    )
    trace = result["trace"]
    assert trace["planSource"] == "exact_cache"
    assert trace["plannerLlmInvoked"] is False
    assert "plannerPromptTokensAvoided" not in trace


def test_semantic_cache_rebinds_free_form_discovery_plan_and_records_hit() -> None:
    catalogue = FakeCatalogue()
    catalogue.semantic = [
        {
            "normalisedQuery": "films about democratic collapse",
            "cacheDocumentId": "plan::semantic",
            "similarity": 0.96,
            "embeddingTrace": {
                "embeddingModelCalls": 1,
                "embeddingCacheHits": 0,
                "embeddingElapsedMs": 7.5,
            },
            "guard": {
                "intentFamily": "discovery",
                "contentType": "movie",
                "fieldConstraint": None,
                "releasePeriod": None,
                "releaseYear": None,
                "topRated": False,
                "personalise": False,
                "contextual": False,
            },
            "planTemplate": {
                "intent": "catalogue_query",
                "contentType": "movie",
                "filters": {},
                "semanticQuery": "democratic collapse",
                "sort": "relevance",
                "limit": 12,
                "confidence": 1,
            },
        }
    ]
    result = asyncio.run(
        build_planner(catalogue).plan(
            "show me films about a collapsing democracy",
            catalogue_intent=intent(cleaned="collapsing democracy"),
        )
    )
    assert result["trace"]["planSource"] == "semantic_cache"
    assert result["trace"]["similarity"] == 0.96
    assert result["trace"]["embeddingModelCalls"] == 1
    assert result["trace"]["embeddingElapsedMs"] == 7.5
    assert result["plan"]["semanticQuery"] == "collapsing democracy"
    assert catalogue.marked == ["plan::semantic"]


def test_semantic_cache_rejects_incompatible_title_field_plan() -> None:
    catalogue = FakeCatalogue()
    catalogue.semantic = [
        {
            "normalisedQuery": "movies with war in the title",
            "cacheDocumentId": "plan::wrong",
            "similarity": 0.99,
            "guard": {
                "intentFamily": "title_field",
                "contentType": "movie",
                "fieldConstraint": "title_contains",
                "releasePeriod": None,
                "releaseYear": None,
                "topRated": False,
                "personalise": False,
                "contextual": False,
            },
            "planTemplate": {
                "intent": "catalogue_query",
                "contentType": "movie",
                "filters": {"titleContains": "war"},
                "sort": "popularity_desc",
                "limit": 12,
                "confidence": 1,
            },
        }
    ]
    result = asyncio.run(
        build_planner(catalogue).plan(
            "show me war genre movies",
            catalogue_intent=intent(genres=["War"], cleaned=""),
        )
    )
    assert result["trace"]["planSource"] == "deterministic"
    assert catalogue.marked == []


def test_sqlpp_plan_execution_uses_fresh_title_field_and_rating_constraints() -> None:
    service = object.__new__(CatalogueService)
    service.settings = SimpleNamespace(
        content_bucket="streaming",
        catalogue_scope="catalogue",
        titles_collection="titles",
    )
    captured: dict[str, Any] = {}

    def query(statement: str, parameters: dict[str, Any]):
        captured["statement"] = statement
        captured["parameters"] = parameters
        return [
            {
                "id": "movie::war-worlds",
                "title": "War of the Worlds",
                "contentType": "movie",
                "releaseYear": 2025,
                "genres": ["Science Fiction", "Thriller"],
                "voteAverage": 8.1,
            },
            {
                "id": "movie::troy",
                "title": "Troy",
                "contentType": "movie",
                "releaseYear": 2004,
                "genres": ["War"],
                "voteAverage": 7.3,
            },
        ]

    service._query_titles = query  # type: ignore[method-assign]
    service._exclude_by_purpose = lambda docs, *_args, **_kwargs: docs  # type: ignore[method-assign]
    service._add_rank_scores = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    service._write_trace = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    result = service.query_catalogue_plan(
        login_id="viewer",
        plan={
            "intent": "catalogue_query",
            "contentType": "movie",
            "filters": {"titleContains": "war"},
            "semanticQuery": None,
            "personalise": False,
            "sort": "rating_desc",
            "limit": 5,
            "planSignature": "abc",
        },
    )
    assert "CONTAINS(LOWER(t.title)" in captured["statement"]
    assert "t.voteAverage DESC" in captured["statement"]
    assert captured["parameters"]["titleContains"] == "war"
    assert [item["title"] for item in result["results"]] == [
        "War of the Worlds"
    ]


def test_legacy_plan_cache_estimates_are_not_exposed_as_measured_values(monkeypatch) -> None:
    from ui.app import usage_reporter
    monkeypatch.setattr(usage_reporter, "snapshot", lambda viewer: {"available": False})
    service = object.__new__(CatalogueService)
    result = service._finalise_ai_metrics({"totals": {"plannerPromptTokensAvoided": 900, "plannerCompletionTokensAvoided": 300}})
    assert "derived" not in result and "totals" not in result
    assert result["measured"]["available"] is False
