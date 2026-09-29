from __future__ import annotations

from pathlib import Path

from ui.app.agent_service import GovernedAgentService


PREVIOUS_RESULTS = {
    "resultTitleIds": [
        "movie::dark-knight",
        "movie::inception",
        "movie::interstellar",
        "movie::graphic-desires",
        "movie::arrival",
        "movie::prisoners",
    ],
    "resultTitles": [
        "The Dark Knight",
        "Inception",
        "Interstellar",
        "Graphic Desires",
        "Arrival",
        "Prisoners",
    ],
}


def test_numeric_prefix_resolves_exact_previous_title_ids() -> None:
    request = GovernedAgentService.extract_action_request(
        "Add first 2 to my list", PREVIOUS_RESULTS
    )

    assert request == {
        "action": "watchlist_add",
        "reference": "first 2",
        "resolvedTitleId": None,
        "resolvedTitle": None,
        "resolvedTitleIds": ["movie::dark-knight", "movie::inception"],
        "resolvedTitles": ["The Dark Knight", "Inception"],
        "requestedCount": 2,
        "source": "previous_results",
    }


def test_worded_prefix_resolves_exact_previous_title_ids() -> None:
    request = GovernedAgentService.extract_action_request(
        "add first two to my list", PREVIOUS_RESULTS
    )

    assert request["resolvedTitleIds"] == [
        "movie::dark-knight",
        "movie::inception",
    ]
    assert request["resolvedTitles"] == ["The Dark Knight", "Inception"]
    assert request["requestedCount"] == 2
    assert request["source"] == "previous_results"


def test_both_and_plural_nouns_are_supported_for_governed_writes() -> None:
    both = GovernedAgentService.extract_action_request(
        "save both of them to my favourites", PREVIOUS_RESULTS
    )
    first_three = GovernedAgentService.extract_action_request(
        "add the first three movies to my watchlist", PREVIOUS_RESULTS
    )
    first_two_of_them = GovernedAgentService.extract_action_request(
        "add the first two of them to my list", PREVIOUS_RESULTS
    )

    assert both["action"] == "like_title"
    assert both["resolvedTitleIds"] == [
        "movie::dark-knight",
        "movie::inception",
    ]
    assert first_three["resolvedTitleIds"] == [
        "movie::dark-knight",
        "movie::inception",
        "movie::interstellar",
    ]
    assert first_two_of_them["resolvedTitleIds"] == [
        "movie::dark-knight",
        "movie::inception",
    ]


def test_insufficient_previous_results_fail_closed_without_title_search() -> None:
    request = GovernedAgentService.extract_action_request(
        "add first three to my list",
        {
            "resultTitleIds": ["movie::one", "movie::two"],
            "resultTitles": ["One", "Two"],
        },
    )

    assert request["source"] == "previous_results"
    assert request["selectionError"] == "requested_more_than_available"
    assert request["requestedCount"] == 3
    assert request["availableCount"] == 2
    assert request.get("resolvedTitleIds") in (None, [])


def test_batch_scope_is_bounded_and_playback_remains_single_title() -> None:
    oversized = GovernedAgentService.extract_action_request(
        "add first 6 to my list", PREVIOUS_RESULTS
    )
    playback = GovernedAgentService.extract_action_request(
        "play first two", PREVIOUS_RESULTS
    )

    assert oversized["source"] == "previous_results"
    assert oversized["selectionError"] == "batch_limit_exceeded"
    assert oversized["requestedCount"] == 6
    assert playback["action"] == "play"
    assert playback["resolvedTitleId"] is None
    assert "resolvedTitleIds" not in playback


def test_batch_reply_reports_all_successes_and_partial_policy_denial() -> None:
    success = GovernedAgentService.render_batch_action_reply(
        "watchlist_add",
        [
            {"title": "The Dark Knight", "status": "completed"},
            {"title": "Inception", "status": "completed"},
        ],
    )
    partial = GovernedAgentService.render_batch_action_reply(
        "watchlist_add",
        [
            {"title": "The Dark Knight", "status": "completed"},
            {
                "title": "Inception",
                "status": "policy_denied",
                "reason": "outside the exclusive Family preference",
            },
        ],
    )

    assert success == "Added The Dark Knight and Inception to My List."
    assert partial.startswith("Added The Dark Knight to My List.")
    assert "I could not complete the same action for Inception" in partial
    assert "outside the exclusive Family preference" in partial


def test_runtime_re_reads_and_mutates_each_selected_exact_id() -> None:
    root = Path(__file__).resolve().parents[1]
    main_source = (root / "ui/app/main.py").read_text()

    assert "for title_id in resolved_title_ids:" in main_source
    assert "catalogue.title, title_id" in main_source
    assert "for title in resolved_titles:" in main_source
    assert '"requestedTitleCount": requested_count' in main_source
    assert '"actionOutcomes": [' in main_source
    assert "governed_batch_action_partial" in main_source
