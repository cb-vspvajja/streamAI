from __future__ import annotations

import sys
import types

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


def model() -> LocalModelService:
    return LocalModelService(Settings())


def bare_catalogue() -> CatalogueService:
    service = object.__new__(CatalogueService)
    service.settings = Settings()
    return service


def test_profile_forecast_and_personalised_recommendation_are_distinct() -> None:
    assert LocalModelService.is_profile_question("what kind of movies will i like")
    assert not LocalModelService.is_personalised_recommendation_request(
        "what kind of movies will i like"
    )
    assert LocalModelService.is_personalised_recommendation_request("what movies will i like")
    assert not LocalModelService.is_profile_question("what movies will i like")


def test_recommendation_complaint_is_not_a_preference_write() -> None:
    service = model()
    try:
        message = "how did you suggest scary movie when i dont like horror movies"
        assert service.is_recommendation_explanation_question(message)
        assert not service.should_capture_preference(message)
        assert service.extract_preferences_fast(message) == []
    finally:
        service.close()


def test_explicit_preference_statements_still_write() -> None:
    service = model()
    try:
        assert service.should_capture_preference("I like thriller movies")
        assert service.should_capture_preference("I don't like horror movies")
        assert service.should_capture_preference("My favourite director is Christopher Nolan")
    finally:
        service.close()


def test_profile_query_uses_authoritative_positive_terms() -> None:
    service = bare_catalogue()
    profile = {
        "preferredGenres": ["Science Fiction", "Thriller"],
        "preferredThemes": ["Motorcycles"],
        "preferredPeople": ["Denis Villeneuve"],
        "preferredLanguages": ["English"],
    }
    service.get_profile = lambda _login_id: profile
    plan = service.recommendation_query_for_profile("sai")
    assert "what movies will i like" not in plan["query"]
    assert "Science Fiction" in plan["query"]
    assert "Motorcycles" in plan["query"]


def test_recommendation_hard_excludes_disliked_genres_and_themes() -> None:
    service = bare_catalogue()
    service.viewer_signals = lambda _login_id: {
        "profile": {
            "dislikedGenres": ["Horror"],
            "dislikedThemes": ["Zombie"],
            "dislikedPeople": [],
            "avoidGraphicViolence": False,
        },
        "disliked": set(),
        "liked": set(),
        "watched": set(),
        "watchlist": set(),
        "topPickExclusions": set(),
    }
    docs = [
        {"id": "scary", "genres": ["Comedy", "Horror"], "keywords": []},
        {"id": "zombie", "genres": ["Comedy"], "keywords": ["Zombie"]},
        {"id": "matrix", "genres": ["Action", "Science Fiction"], "keywords": []},
    ]
    assert service._exclude_by_purpose(docs, "sai", "recommendation") == [docs[2]]


def test_recommendation_explanation_does_not_claim_profile_changed() -> None:
    answer = LocalModelService.render_recommendation_explanation(
        {"dislikedGenres": ["Horror"], "dislikedThemes": []}
    )
    assert "avoid Horror" in answer
    assert "not changed your profile" in answer


def test_profile_lexical_query_uses_individual_terms_without_title_fuzziness() -> None:
    service = bare_catalogue()
    intent = service._analyse_query("Science Fiction Thriller Denis Villeneuve")
    intent["profileQuery"] = True
    intent["terms"] = ["Science Fiction", "Thriller", "Denis Villeneuve"]
    lexical = service._lexical_query(intent)
    assert not any(clause.get("field") == "title" for clause in lexical["disjuncts"])
    assert any(
        clause.get("field") == "genres" and clause.get("term") == "Thriller"
        for clause in lexical["disjuncts"]
    )


def test_likes_dislikes_question_is_deterministic_profile_intent() -> None:
    assert LocalModelService.is_likes_dislikes_question("what is my likes and dislikes")
    assert LocalModelService.is_likes_dislikes_question("what do I like and dislike?")
    assert not LocalModelService.is_recommendation_request("what is my likes and dislikes")


def test_likes_dislikes_reply_never_inverts_preferred_people() -> None:
    reply = LocalModelService.render_likes_dislikes_reply(
        {
            "preferredGenres": ["Thriller"],
            "dislikedGenres": ["Horror"],
            "preferredThemes": ["Motorcycles"],
            "dislikedThemes": [],
            "preferredPeople": ["Christopher Nolan", "Denis Villeneuve"],
            "dislikedPeople": [],
        },
        [{"title": "The Dark Knight"}],
        [],
    )
    assert "The Dark Knight" in reply
    assert "titles involving Christopher Nolan" in reply
    assert "Horror content" in reply
    dislikes = reply.split("Dislikes:", 1)[1]
    assert "Christopher Nolan" not in dislikes
    assert "Denis Villeneuve" not in dislikes
