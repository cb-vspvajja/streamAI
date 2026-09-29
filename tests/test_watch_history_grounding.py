from ui.app.llm_service import LocalModelService


def test_watch_history_question_is_detected() -> None:
    assert LocalModelService.is_watch_history_question(
        "What films have I watched already?"
    )
    assert LocalModelService.history_content_type(
        "What films have I watched already?"
    ) == "movie"


def test_watched_title_claim_is_extracted() -> None:
    assert (
        LocalModelService.extract_watched_title_claim(
            "I have watched A quite place"
        )
        == "A quite place"
    )
    assert (
        LocalModelService.extract_watched_title_claim(
            "I've watched A Quiet Place"
        )
        == "A Quiet Place"
    )


def test_history_question_is_not_treated_as_claim() -> None:
    assert (
        LocalModelService.extract_watched_title_claim(
            "What films have I watched already?"
        )
        is None
    )


def test_vague_genre_statement_is_not_a_watch_claim() -> None:
    assert LocalModelService.extract_watched_title_claim(
        "I like horror movies"
    ) is None


def test_grounded_history_reply_lists_authoritative_titles() -> None:
    reply = LocalModelService.render_watch_history_reply(
        [
            {
                "title": "A Quiet Place",
                "releaseYear": 2018,
                "progressPct": 100,
            },
            {
                "title": "Slow Horses",
                "releaseYear": 2022,
                "progressPct": 45,
            },
        ]
    )
    assert "A Quiet Place (2018) — completed" in reply
    assert "Slow Horses (2022) — 45% watched" in reply


def test_empty_history_is_explicit() -> None:
    reply = LocalModelService.render_watch_history_reply([])
    assert "don’t have any watched titles recorded" in reply
