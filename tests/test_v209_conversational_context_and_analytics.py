from __future__ import annotations

import asyncio
import sys
import threading
import types
from types import SimpleNamespace

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
    exceptions.DocumentExistsException = type(
        "DocumentExistsException", (Exception,), {}
    )
    n1ql.QueryScanConsistency = SimpleNamespace(REQUEST_PLUS="request_plus")
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

exceptions_module = sys.modules["couchbase.exceptions"]
if not hasattr(exceptions_module, "DocumentExistsException"):
    exceptions_module.DocumentExistsException = type(
        "DocumentExistsException", (Exception,), {}
    )

if "agentmemory" not in sys.modules:
    agentmemory = types.ModuleType("agentmemory")
    agentmemory.AgentMemoryClient = object
    agentmemory.ChatMessage = object
    sys.modules["agentmemory"] = agentmemory

from ui.app.agent_service import GovernedAgentService
from ui.app.catalogue_service import CatalogueService
from ui.app.llm_service import LocalModelService
from ui.app.mcp_gateway import MCPCall, MCPGateway
from ui.app.memory_service import AgentMemoryService, DemoState


PREVIOUS = {
    "resultTitleIds": ["movie::one", "movie::two", "movie::three"],
    "resultTitles": ["One", "Two", "Three"],
}


def test_reported_contextual_followups_have_dedicated_routes() -> None:
    assert LocalModelService.extract_contextual_request(
        "what question did i ask you before?"
    ) == {"kind": "previous_question"}
    assert LocalModelService.extract_contextual_request(
        "what movies did you add to my list recently"
    ) == {"kind": "recent_my_list_additions"}
    assert LocalModelService.extract_contextual_request(
        "show me movies similar to ones in my list"
    ) == {"kind": "similar_to_my_list"}
    assert LocalModelService.extract_contextual_request(
        "show me the ones i can watch from this list"
    ) == {"kind": "filter_previous_results", "filter": "playable"}
    assert LocalModelService.extract_contextual_request(
        "what did you just say?"
    ) == {"kind": "previous_answer"}
    assert LocalModelService.extract_contextual_request(
        "repeat the previous recommendations"
    ) == {"kind": "repeat_previous_results"}


def test_broader_previous_result_filter_matrix_is_deterministic() -> None:
    examples = {
        "which of these are already in my list": "in_my_list",
        "which of these are missing from my list": "missing_from_my_list",
        "which of these have i watched": "watched",
        "show me the unseen ones": "unwatched",
        "which of these have i liked": "liked",
        "which one is the highest rated": "top_rated",
        "show only the movies from this list": "movies_only",
        "show only the series from this list": "series_only",
    }
    for message, expected_filter in examples.items():
        assert LocalModelService.extract_contextual_request(message) == {
            "kind": "filter_previous_results",
            "filter": expected_filter,
        }


def test_collective_and_missing_actions_resolve_only_previous_ids() -> None:
    all_request = GovernedAgentService.extract_action_request(
        "add them to my list", PREVIOUS
    )
    missing_request = GovernedAgentService.extract_action_request(
        "add the ones missing in my list", PREVIOUS
    )

    assert all_request["resolvedTitleIds"] == PREVIOUS["resultTitleIds"]
    assert all_request["selectionMode"] == "previous_all"
    assert missing_request["resolvedTitleIds"] == PREVIOUS["resultTitleIds"]
    assert missing_request["selectionMode"] == "previous_missing_from_watchlist"


def test_collective_action_without_previous_results_fails_closed() -> None:
    request = GovernedAgentService.extract_action_request(
        "add these to my list", {}
    )
    assert request["selectionError"] == "no_previous_results"
    assert request["source"] == "previous_results"
    assert not request.get("resolvedTitleIds")


def test_title_detail_followups_resolve_exact_previous_ids() -> None:
    second = GovernedAgentService.resolve_previous_title_reference(
        "the second one", PREVIOUS
    )
    this_title = GovernedAgentService.resolve_previous_title_reference(
        "this title",
        {
            **PREVIOUS,
            "lastGroundedTitleId": "movie::three",
            "lastGroundedTitle": "Three",
        },
    )
    assert second == {"id": "movie::two", "title": "Two"}
    assert this_title == {"id": "movie::three", "title": "Three"}
    assert LocalModelService.extract_catalogue_title_question(
        "who directed the second one?"
    ) == {"type": "director", "title": "the second one"}
    play_it = GovernedAgentService.extract_action_request(
        "play it",
        {
            **PREVIOUS,
            "lastGroundedTitleId": "movie::three",
            "lastGroundedTitle": "Three",
        },
    )
    like_it = GovernedAgentService.extract_action_request(
        "like it",
        {
            **PREVIOUS,
            "lastGroundedTitleId": "movie::three",
            "lastGroundedTitle": "Three",
        },
    )
    assert play_it["resolvedTitleId"] == "movie::three"
    assert like_it["resolvedTitleId"] == "movie::three"


def test_generic_profile_recall_and_dark_humour_are_supported() -> None:
    assert LocalModelService.is_profile_question("what do i like?") is True
    model = object.__new__(LocalModelService)
    preferences = model.extract_preferences_fast("I like dark humour")
    assert preferences == [
        {
            "kind": "theme",
            "value": "Dark Humour",
            "sentiment": "like",
            "fact": "Viewer prefers dark humour-themed content.",
            "source": "explicit_viewer_statement",
        }
    ]


def test_grouped_catalogue_question_builds_an_allowlisted_plan() -> None:
    plan = LocalModelService.extract_catalogue_analytics_request(
        "How many movies are in the catalogue by genre?"
    )
    assert plan == {
        "measure": "count",
        "dimension": "genre",
        "filters": {
            "contentType": "movie",
            "genre": None,
            "releaseYear": None,
        },
        "limit": 30,
        "order": "desc",
    }
    rendered = LocalModelService.render_catalogue_analytics(
        {
            **plan,
            "rows": [
                {"label": "Drama", "value": 24},
                {"label": "Thriller", "value": 17},
            ],
        }
    )
    assert "Catalogue title count by genre" in rendered
    assert "• Drama: 24" in rendered

    each_genre = LocalModelService.extract_catalogue_analytics_request(
        "How many movies are in each genre?"
    )
    assert each_genre == plan
    assert LocalModelService.extract_catalogue_analytics_request(
        "How many films does every genre have?"
    )["dimension"] == "genre"
    assert LocalModelService.extract_catalogue_analytics_request(
        "Show me a genre-wise movie count."
    )["dimension"] == "genre"

    assert LocalModelService.extract_catalogue_analytics_request(
        "What is the average rating of movies by release year?"
    )["dimension"] == "release_year"
    assert LocalModelService.extract_catalogue_analytics_request(
        "Show the title count by original language."
    )["dimension"] == "original_language"
    series_runtime = LocalModelService.extract_catalogue_analytics_request(
        "What is the average runtime of series by genre?"
    )
    assert series_runtime["measure"] == "average_runtime"
    assert series_runtime["filters"]["contentType"] == "tv"


def test_previous_turn_recall_uses_active_transcript_without_model() -> None:
    memory = object.__new__(AgentMemoryService)
    memory._lock = threading.RLock()
    memory.state = DemoState(
        user=object(),
        session=object(),
        session_id="session-1",
        chat_log=[
            {"role": "user", "content": "Suggest some good movies"},
            {"role": "assistant", "content": "Here are four catalogue results."},
        ],
    )
    recall = memory.recall_previous_turn()
    assert recall == {
        "userMessage": "Suggest some good movies",
        "assistantMessage": "Here are four catalogue results.",
        "sessionId": "session-1",
        "source": "active_transcript",
    }


def test_trace_summary_handles_previous_turn_without_catalogue_results() -> None:
    summary = GovernedAgentService._summarise_output(
        {
            "userMessage": "Give me super hero movies",
            "assistantMessage": "Here are the strongest catalogue matches.",
            "sessionId": "session-1",
            "source": "active_transcript",
        }
    )

    assert summary == {
        "previousUserMessage": "Give me super hero movies",
        "previousAssistantMessage": "Here are the strongest catalogue matches.",
        "memorySource": "active_transcript",
    }
    assert "titles" not in summary


def test_trace_summary_keeps_catalogue_titles_scoped_to_result_outputs() -> None:
    summary = GovernedAgentService._summarise_output(
        {
            "results": [
                {"id": "movie::supergirl", "title": "Supergirl"},
                {"id": "movie::avengers", "title": "The Avengers"},
            ]
        }
    )

    assert summary["titleIds"] == ["movie::supergirl", "movie::avengers"]
    assert summary["titles"] == ["Supergirl", "The Avengers"]


def test_contextual_filter_is_a_strict_subset_of_previous_ids() -> None:
    service = object.__new__(CatalogueService)
    docs = {
        "movie::one": {"id": "movie::one", "title": "One", "contentType": "movie"},
        "movie::two": {"id": "movie::two", "title": "Two", "contentType": "movie"},
    }
    service._fetch_title_ids = lambda ids: [dict(docs[item]) for item in ids if item in docs]
    service.viewer_signals = lambda _login: {
        "profile": {"viewerId": "viewer"},
        "liked": set(),
        "disliked": set(),
        "watchlist": {"movie::one"},
        "watched": set(),
    }
    service.ensure_entitlement = lambda _login: {}
    service.check_entitlement = lambda _login, doc, **_kwargs: {
        "allowed": True,
        "includedInPlan": doc["id"] == "movie::two",
        "decision": "allow",
    }
    service._write_trace = lambda *_args, **_kwargs: None

    result = service.filter_grounded_results(
        login_id="viewer",
        title_ids=["movie::one", "movie::two", "movie::outside"],
        filter_kind="playable",
    )

    assert [item["id"] for item in result["results"]] == ["movie::two"]
    assert set(result["trace"]["selectedTitleIds"]).issubset(
        set(result["trace"]["inputTitleIds"])
    )
    assert result["trace"]["expandedBeyondPreviousResults"] is False


def test_mcp_catalogue_analytics_adapter_uses_validated_grouping() -> None:
    gateway = MCPGateway(
        SimpleNamespace(
            mcp_enabled=True,
            mcp_required=True,
            mcp_url="http://localhost:8000/mcp",
            mcp_timeout_seconds=8,
            mcp_bearer_token="",
            mcp_read_only=True,
            content_bucket="streaming",
            titles_collection="titles",
            catalogue_scope="catalogue",
        )
    )
    captured: dict[str, object] = {}

    async def fake_run_query(query, named_parameters, *, scope_name):
        captured.update(
            query=query,
            named_parameters=named_parameters,
            scope_name=scope_name,
        )
        return MCPCall(
            tool="run_sql_plus_plus_query",
            arguments={"query": query},
            result=[{"label": "Drama", "metricValue": 12}],
            duration_ms=1.0,
        )

    gateway._run_query = fake_run_query  # type: ignore[method-assign]
    result = asyncio.run(
        gateway.call_business_tool(
            "query_catalogue_analytics",
            {
                "measure": "count",
                "dimension": "genre",
                "filters": {"contentType": "movie"},
                "limit": 30,
            },
        )
    )

    assert result["rows"] == [{"label": "Drama", "value": 12}]
    assert "UNNEST t.genres AS genre" in str(captured["query"])
    assert "AS metricValue" in str(captured["query"])
    assert "ORDER BY metricValue DESC" in str(captured["query"])
    assert " AS value" not in str(captured["query"])
    assert captured["scope_name"] == "catalogue"
    assert result["_nativeExecution"]["businessTool"] == "query_catalogue_analytics"
