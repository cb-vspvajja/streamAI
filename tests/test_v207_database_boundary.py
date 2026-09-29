from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ui.app.llm_service import LocalModelService


def test_highest_rated_wording_never_falls_through_to_model() -> None:
    cases = {
        "Which is the highest rated movie?": {
            "contentType": "movie",
            "kind": "movie",
        },
        "What is the best-rated film?": {
            "contentType": "movie",
            "kind": "film",
        },
        "Which movie has the highest rating?": {
            "contentType": "movie",
            "kind": "movie",
        },
        "What's the top-rated show?": {
            "contentType": "tv",
            "kind": "show",
        },
        "Which film is best rated?": {
            "contentType": "movie",
            "kind": "film",
        },
        "Show me the top rated series in the catalogue": {
            "contentType": "tv",
            "kind": "series",
        },
    }
    for question, expected in cases.items():
        assert LocalModelService.extract_top_rated_catalogue_question(question) == expected
        assert LocalModelService.is_recommendation_request(question) is False


def test_external_rating_request_gets_database_boundary_reply() -> None:
    reply = LocalModelService.render_database_boundary_reply(
        "What is its Rotten Tomatoes score?"
    )
    assert "do not have a verified external review score" in reply
    assert "Rotten Tomatoes" not in reply


def test_unmatched_request_gets_database_boundary_reply() -> None:
    reply = LocalModelService.render_database_boundary_reply(
        "Tell me everything you know about famous films"
    )
    assert "only answer using verified information" in reply
    assert "current catalogue" in reply
    assert "viewer data" in reply


def test_direct_model_stream_is_blocked_without_current_turn_catalogue_evidence() -> None:
    class Provider:
        async def stream(self, _messages: Any, _usage_out: Any):
            raise AssertionError("The provider must not be called without DB evidence")
            yield "unreachable"

    service = object.__new__(LocalModelService)
    service._chat_provider = Provider()

    async def collect() -> str:
        parts = []
        async for token in service.stream_answer(
            user_message="Which is the highest rated movie?",
            short_term_context=[],
            long_term_context=[],
            current_turn_facts=[],
            catalogue_candidates=[],
            retrieval_trace={},
        ):
            parts.append(token)
        return "".join(parts)

    answer = asyncio.run(collect())
    assert answer == LocalModelService.DATABASE_BOUNDARY_REPLY


def test_catalogue_prompt_is_forwarded_for_any_explicit_internal_model_call() -> None:
    captured: dict[str, Any] = {}

    class Provider:
        async def stream(self, messages: Any, _usage_out: Any):
            captured["messages"] = messages
            yield "Grounded explanation"

    service = object.__new__(LocalModelService)
    service._chat_provider = Provider()

    async def collect() -> str:
        parts = []
        async for token in service.stream_answer(
            user_message="Explain this result",
            short_term_context=[],
            long_term_context=[],
            current_turn_facts=[],
            catalogue_candidates=[
                {
                    "id": "movie::one",
                    "title": "Verified Film",
                    "contentType": "movie",
                    "releaseYear": 2026,
                    "genres": ["Drama"],
                }
            ],
            retrieval_trace={"mode": "fts"},
            system_prompt="STRICT DATABASE-BOUND SYSTEM PROMPT",
        ):
            parts.append(token)
        return "".join(parts)

    assert asyncio.run(collect()) == "Grounded explanation"
    assert captured["messages"][0]["content"].startswith(
        "STRICT DATABASE-BOUND SYSTEM PROMPT"
    )


def test_viewer_runtime_has_no_unconstrained_model_fallback() -> None:
    root = Path(__file__).resolve().parents[1]
    main_source = (root / "ui/app/main.py").read_text()
    assert "model.stream_answer(" not in main_source
    assert 'intent="database_boundary"' in main_source
    assert '"modelCallBlocked": True' in main_source
