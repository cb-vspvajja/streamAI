import sys
import types

# The regression tests exercise pure query/profile helpers without opening a
# Couchbase connection. Lightweight SDK stubs keep the test suite runnable in
# environments where the binary Couchbase package is not installed.
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


def test_animal_theme_is_promoted_to_durable_preference() -> None:
    service = model()
    try:
        preferences = service.extract_preferences_fast("I like animal movies")
        assert preferences == [
            {
                "kind": "theme",
                "value": "Animal",
                "sentiment": "like",
                "fact": "Viewer prefers animal-themed content.",
                "source": "explicit_viewer_statement",
            }
        ]
    finally:
        service.close()


def test_thriller_confirmation_has_correct_grammar() -> None:
    answer = LocalModelService.render_preference_confirmation(
        [{"fact": "Viewer prefers Thriller content."}]
    )
    assert "you prefer Thriller content" in answer
    assert "you prefers" not in answer


def test_profile_question_variants_and_typo_are_detected() -> None:
    assert LocalModelService.is_profile_question("what genere movies do i like")
    assert LocalModelService.is_profile_question("what type of movies do i like")
    assert LocalModelService.is_profile_question("Which directors do I prefer?")


def test_grounded_recommendation_mentions_only_candidates() -> None:
    candidates = [
        {
            "id": "movie::1",
            "title": "Catalogue One",
            "releaseYear": 2024,
            "genres": ["Action"],
            "matchExplanation": {"summary": "Matched keywords"},
        },
        {
            "id": "movie::2",
            "title": "Catalogue Two",
            "releaseYear": 2022,
            "genres": ["Drama"],
            "matchExplanation": {"summary": "Semantic catalogue similarity"},
        },
    ]
    answer = LocalModelService.render_catalogue_recommendations(
        "suggest motorcycle movies", candidates, {"mode": "hybrid"}
    )
    assert "Catalogue One" in answer
    assert "Catalogue Two" in answer
    assert "Mad Max" not in answer
    assert "The Accountant" not in answer


def test_empty_catalogue_result_is_honest() -> None:
    answer = LocalModelService.render_catalogue_recommendations(
        "suggest motorcycle movies",
        [],
        {"expandedTerms": ["motorcycle", "motorbike", "biker"]},
    )
    assert "couldn’t find a matching title" in answer
    assert "motorbike" in answer


def test_indian_is_a_structured_country_language_intent() -> None:
    service = bare_catalogue()
    intent = service._analyse_query("Indian")
    assert intent["country"]["countries"] == ["IN"]
    assert "hi" in intent["country"]["languages"]
    assert intent["cleaned"] == ""


def test_structured_country_genre_search_has_no_fuzzy_title_clauses() -> None:
    service = bare_catalogue()
    lexical = service._lexical_query(service._analyse_query("Indian thriller"))
    fuzzy_fields = {
        clause.get("field")
        for clause in lexical["disjuncts"]
        if clause.get("fuzziness") is not None
    }
    assert fuzzy_fields == set()
    assert not any(clause.get("field") == "searchText" for clause in lexical["disjuncts"])


def test_browser_payload_strips_embedding_and_source_text() -> None:
    service = bare_catalogue()
    doc = {
        "title": "Example",
        "posterPath": "/p.jpg",
        "backdropPath": "/b.jpg",
        "embedding": [0.1, 0.2],
        "embeddingText": "large text",
        "searchText": "large search text",
    }
    service._decorate(doc)
    assert "embedding" not in doc
    assert "embeddingText" not in doc
    assert "searchText" not in doc
    assert doc["posterUrl"].endswith("/p.jpg")


def test_top_picks_exclude_watched_liked_and_disliked_titles() -> None:
    service = bare_catalogue()
    service.viewer_signals = lambda _login_id: {
        "disliked": {"d"},
        "liked": {"l"},
        "watched": {"w"},
        "watchlist": set(),
        "topPickExclusions": {"d", "l", "w"},
    }
    docs = [{"id": "d"}, {"id": "l"}, {"id": "w"}, {"id": "fresh"}]
    assert service._exclude_by_purpose(docs, "sai", "recommendation") == [
        {"id": "fresh"}
    ]
