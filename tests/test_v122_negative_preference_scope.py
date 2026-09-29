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


def test_negative_preference_scope_survives_commas() -> None:
    model = LocalModelService(Settings())
    try:
        message = "I do not like sex, romance, adult content movies"
        preferences = model.extract_preferences_fast(message)

        assert model.is_recommendation_request(message) is False
        assert model.is_preference_only_statement(message) is True
        assert ("genre", "Romance", "dislike") in {
            (item["kind"], item["value"], item["sentiment"]) for item in preferences
        }
        assert ("safety", "adult_content", "avoid") in {
            (item["kind"], item["value"], item["sentiment"]) for item in preferences
        }
        assert not any(
            item["value"] == "Romance" and item["sentiment"] == "like"
            for item in preferences
        )
        assert not any(item["kind"] == "theme" and "romance" in str(item["value"]).lower() for item in preferences)
    finally:
        model.close()


def test_preference_only_routing_beats_detected_catalogue_genre() -> None:
    model = LocalModelService(Settings())
    catalogue = object.__new__(CatalogueService)
    catalogue.settings = Settings()
    try:
        message = "I do not like sex, romance, adult content movies"
        intent = catalogue.analyse_request(message)

        # The catalogue analyser may correctly notice the noun Romance, but the
        # chat router must give the explicit negative preference verb priority.
        assert intent["genres"] == ["Romance"]
        assert model.is_preference_only_statement(message) is True
    finally:
        model.close()


def test_compound_negative_preference_and_browse_are_separated() -> None:
    model = LocalModelService(Settings())
    try:
        message = "I do not like sex, romance or adult content; show me comedy movies"
        preferences = model.extract_preferences_fast(message)

        assert model.extract_recommendation_clause(message) == "show me comedy movies"
        assert model.is_recommendation_request(message) is True
        assert model.is_preference_only_statement(message) is False
        assert ("genre", "Romance", "dislike") in {
            (item["kind"], item["value"], item["sentiment"]) for item in preferences
        }
        assert ("safety", "adult_content", "avoid") in {
            (item["kind"], item["value"], item["sentiment"]) for item in preferences
        }
        assert all(item.get("value") != "Comedy" for item in preferences)
    finally:
        model.close()


def test_adult_content_safety_preference_filters_catalogue_keywords() -> None:
    service = object.__new__(CatalogueService)
    profile = {
        "dislikedGenres": [],
        "dislikedThemes": [],
        "dislikedPeople": [],
        "avoidGraphicViolence": False,
        "avoidAdultContent": True,
    }

    violations = service._preference_violations(
        {
            "genres": ["Drama"],
            "keywords": ["sexual content", "relationship"],
            "castNames": [],
            "directorNames": [],
            "adult": False,
        },
        profile,
    )
    assert "safety preference: sexual content" in violations

    adult_flag_violations = service._preference_violations(
        {
            "genres": ["Drama"],
            "keywords": [],
            "castNames": [],
            "directorNames": [],
            "adult": True,
        },
        profile,
    )
    assert "safety preference: adult title" in adult_flag_violations


def test_profile_confirmation_is_clear_and_not_a_search_result() -> None:
    model = LocalModelService(Settings())
    try:
        preferences = model.extract_preferences_fast(
            "I do not like sex, romance, adult content movies"
        )
        reply = model.render_preference_confirmation(preferences)
        assert "remember these preferences" in reply.lower()
        assert "you dislike Romance content" in reply
        assert "avoid sexual and adult content" in reply
        assert "none are available" not in reply
    finally:
        model.close()


def test_adult_safety_signal_does_not_treat_relationship_orientation_as_explicit_content() -> None:
    service = object.__new__(CatalogueService)
    profile = {
        "dislikedGenres": [],
        "dislikedThemes": [],
        "dislikedPeople": [],
        "avoidGraphicViolence": False,
        "avoidAdultContent": True,
    }
    violations = service._preference_violations(
        {
            "genres": ["Drama"],
            "keywords": ["same sex relationship"],
            "castNames": [],
            "directorNames": [],
            "adult": False,
        },
        profile,
    )
    assert violations == []


def test_adult_content_preference_is_persisted_in_structured_profile() -> None:
    service = object.__new__(CatalogueService)
    profile = CatalogueService._profile_defaults("sai")

    class Profiles:
        def __init__(self) -> None:
            self.saved = None

        def upsert(self, _key, value) -> None:
            self.saved = value

    service.profiles = Profiles()
    service.get_profile = lambda _login_id: profile
    service.invalidate_home_cache = lambda _login_id: None

    result = service.apply_preferences(
        "sai",
        [{
            "kind": "safety",
            "value": "adult_content",
            "sentiment": "avoid",
            "fact": "Viewer prefers to avoid sexual and adult content.",
        }],
    )
    assert result["avoidAdultContent"] is True
    assert service.profiles.saved["avoidAdultContent"] is True
