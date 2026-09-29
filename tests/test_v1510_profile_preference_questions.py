from ui.app.llm_service import LocalModelService


def test_do_i_like_horror_is_profile_question_not_catalogue_browse() -> None:
    expected = {
        "subject": "Horror",
        "kind": "genre",
        "polarity": "positive",
        "verb": "like",
    }
    assert LocalModelService.extract_profile_preference_question("Do i like horror?") == expected
    assert LocalModelService.extract_profile_preference_question("Do i like horror films?") == expected
    assert LocalModelService.is_profile_question("Do i like horror films?") is True
    assert LocalModelService.is_recommendation_request("Do i like horror films?") is False


def test_negative_profile_preference_question_is_extracted() -> None:
    assert LocalModelService.extract_profile_preference_question("Do I avoid graphic violence?") == {
        "subject": "Graphic Violence",
        "kind": "theme",
        "polarity": "negative",
        "verb": "avoid",
    }
    assert LocalModelService.extract_profile_preference_question("Am I not a fan of romance films?") == {
        "subject": "Romance",
        "kind": "genre",
        "polarity": "negative",
        "verb": "dislike",
    }


def test_real_horror_browse_request_remains_catalogue_request() -> None:
    assert LocalModelService.extract_profile_preference_question("Show me horror films") is None
    assert LocalModelService.is_profile_question("Show me horror films") is False
    assert LocalModelService.is_recommendation_request("Show me horror films") is True


def test_profile_preference_answer_uses_authoritative_genre_lists() -> None:
    profile = {
        "preferredGenres": ["Science Fiction", "Thriller"],
        "dislikedGenres": ["Horror"],
        "preferredThemes": [],
        "dislikedThemes": [],
    }
    positive_question = LocalModelService.extract_profile_preference_question("Do I like horror?")
    assert positive_question is not None
    assert LocalModelService.render_profile_preference_answer(positive_question, profile) == (
        "No — Horror is recorded in your profile as a genre to avoid."
    )

    negative_question = LocalModelService.extract_profile_preference_question("Do I dislike horror films?")
    assert negative_question is not None
    assert LocalModelService.render_profile_preference_answer(negative_question, profile) == (
        "Yes — Horror is recorded in your profile as a genre to avoid."
    )


def test_profile_preference_answer_is_honest_when_unknown() -> None:
    question = LocalModelService.extract_profile_preference_question("Do I like westerns?")
    assert question is not None
    answer = LocalModelService.render_profile_preference_answer(
        question,
        {"preferredGenres": [], "dislikedGenres": [], "preferredThemes": [], "dislikedThemes": []},
    )
    assert answer == "I don’t have a stored like or dislike for Western in your viewer profile yet."
