from ui.app.config import Settings
from ui.app.llm_service import LocalModelService


def service() -> LocalModelService:
    return LocalModelService(Settings())


def test_explicit_genre_preference_is_durable() -> None:
    model = service()
    try:
        assert model.extract_facts_fast("I really like science fiction films") == [
            "Viewer prefers Science Fiction content."
        ]
    finally:
        model.close()


def test_tonight_intent_is_not_promoted() -> None:
    model = service()
    try:
        assert model.extract_facts_fast("Tonight I want a comedy under 90 minutes") == []
    finally:
        model.close()


def test_dislike_is_grounded() -> None:
    model = service()
    try:
        facts = model.extract_facts_fast("I dislike horror and avoid graphic violence")
        assert "Viewer dislikes Horror content." in facts
        assert "Viewer prefers to avoid graphic violence." in facts
    finally:
        model.close()


def test_unknown_profile_is_not_invented() -> None:
    model = service()
    try:
        answer = model.grounded_memory_reply(
            user_message="What do you know about my taste?",
            short_term_context=[],
            long_term_context=[],
            current_turn_facts=[],
        )
        assert "don’t have any durable viewing preferences" in answer
    finally:
        model.close()
