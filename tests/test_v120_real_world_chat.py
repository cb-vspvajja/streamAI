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


def bare_catalogue() -> CatalogueService:
    service = object.__new__(CatalogueService)
    service.settings = Settings()
    return service


def test_generic_good_movies_is_profile_recommendation_not_literal_search() -> None:
    service = bare_catalogue()
    intent = service.analyse_request("suggest some good movies")
    assert intent["personalisedRecommendation"] is True
    assert intent["genericRecommendation"] is True
    assert intent["contentType"] == "movie"
    assert intent["terms"] == []
    assert LocalModelService.is_personalised_recommendation_request("suggest some good movies")


def test_bare_good_movies_is_also_a_broad_recommendation() -> None:
    service = bare_catalogue()
    intent = service.analyse_request("good movies")
    assert intent["personalisedRecommendation"] is True
    assert intent["genericRecommendation"] is True
    assert intent["terms"] == []


def test_next_watch_language_routes_to_profile_recommendation() -> None:
    service = bare_catalogue()
    intent = service.analyse_request("find me some next watch movies")
    assert intent["nextWatch"] is True
    assert intent["personalisedRecommendation"] is True
    assert intent["terms"] == []


def test_sify_is_recovered_as_strict_science_fiction_browse() -> None:
    service = bare_catalogue()
    intent = service.analyse_request("sify movies")
    assert intent["normalisedQuery"] == "sci-fi movies"
    assert intent["genres"] == ["Science Fiction"]
    assert intent["contentType"] == "movie"
    assert intent["personalisedRecommendation"] is False
    assert intent["terms"] == []


def test_explicit_title_questions_are_grounded_routes() -> None:
    assert LocalModelService.extract_catalogue_title_question(
        "Is The Matrix available?"
    ) == {"type": "availability", "title": "The Matrix"}
    assert LocalModelService.extract_catalogue_title_question(
        "Who is in The Matrix?"
    ) == {"type": "cast", "title": "The Matrix"}
    assert LocalModelService.extract_catalogue_title_question(
        "find The Matrix"
    ) == {"type": "details", "title": "The Matrix"}


def test_broad_find_request_is_not_mistaken_for_literal_title() -> None:
    assert LocalModelService.extract_catalogue_title_question(
        "find me some next watch movies"
    ) is None


def test_similar_title_request_extracts_authoritative_seed() -> None:
    assert LocalModelService.extract_similar_title_request(
        "show me movies like The Matrix"
    ) == "The Matrix"


def test_unresolved_title_answer_never_claims_availability() -> None:
    answer = LocalModelService.render_title_information(
        {"type": "availability", "title": "Missing Film"},
        None,
    )
    assert "couldn’t find" in answer
    assert "won’t claim it is available" in answer


def test_cold_start_recommendations_use_catalogue_trending_documents() -> None:
    service = bare_catalogue()
    service.get_profile = lambda _login_id: {
        "preferredGenres": [],
        "preferredThemes": [],
        "preferredPeople": [],
        "preferredLanguages": ["English"],
        "dislikedGenres": [],
        "dislikedThemes": [],
    }
    service._trending_items = lambda _limit: [
        {"id": "m1", "title": "Movie One", "contentType": "movie", "popularity": 99, "voteAverage": 8.2},
        {"id": "tv1", "title": "Series One", "contentType": "tv", "popularity": 98, "voteAverage": 8.1},
        {"id": "m2", "title": "Movie Two", "contentType": "movie", "popularity": 97, "voteAverage": 8.0},
    ]
    service._exclude_by_purpose = lambda docs, _login_id, _purpose: docs
    service._write_trace = lambda *_args, **_kwargs: None

    result = service.recommend_for_profile(
        login_id="sai", mode="fts", limit=2, content_type="movie"
    )

    assert [item["id"] for item in result["results"]] == ["m1", "m2"]
    assert result["trace"]["coldStartFallback"] is True
    assert all(item["contentType"] == "movie" for item in result["results"])


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"hits": [{"id": "movie-1", "score": 10.0}]}


class _FakeSearchClient:
    def post(self, *_args, **_kwargs):
        return _FakeResponse()


def test_title_resolution_respects_content_type_and_confidence_gate() -> None:
    service = bare_catalogue()
    service._search_client = _FakeSearchClient()
    observed: dict[str, str | None] = {}

    def fetch(_hits, content_type=None):
        observed["content_type"] = content_type
        return [{"id": "movie-1", "title": "Example Film", "originalTitle": "Example Film", "contentType": "movie"}]

    service._fetch_hits = fetch
    result = service.resolve_title("Example Film", content_type="movie")
    assert observed["content_type"] == "movie"
    assert result["match"]["id"] == "movie-1"


def test_natural_catalogue_browse_phrasing_is_not_literal_search() -> None:
    service = bare_catalogue()

    available = service.analyse_request("what films are available")
    assert available["genericRecommendation"] is True
    assert available["personalisedRecommendation"] is True
    assert available["terms"] == []

    comedy = service.analyse_request("what are some classic comedies")
    assert comedy["requestLanguage"] is True
    assert comedy["genres"] == ["Comedy"]
    assert comedy["terms"] == ["classic"]


def test_real_world_availability_runtime_and_rating_questions() -> None:
    assert LocalModelService.extract_catalogue_title_question(
        "Is The Matrix on here?"
    ) == {"type": "availability", "title": "The Matrix"}
    assert LocalModelService.extract_catalogue_title_question(
        "What is the runtime of The Matrix?"
    ) == {"type": "runtime", "title": "The Matrix"}
    assert LocalModelService.extract_catalogue_title_question(
        "What rating is The Matrix?"
    ) == {"type": "rating", "title": "The Matrix"}


def test_explicit_preference_can_be_saved_alone_or_with_a_request() -> None:
    model = LocalModelService
    assert model.should_capture_preference("I like sci-fi movies") is True
    assert model.is_recommendation_request("I like sci-fi movies") is False

    combined = "I like sci-fi movies. What should I watch?"
    assert model.should_capture_preference(combined) is True
    assert model.is_recommendation_request(combined) is True


def test_compound_preference_and_recommendation_are_separated() -> None:
    model = object.__new__(LocalModelService)
    model.settings = Settings()
    message = "I dislike horror films, suggest a comedy"

    assert LocalModelService.is_recommendation_request(message) is True
    assert LocalModelService.extract_recommendation_clause(message) == "suggest a comedy"
    preferences = model.extract_preferences_fast(message)
    assert [(item["value"], item["sentiment"]) for item in preferences] == [("Horror", "dislike")]

    catalogue = bare_catalogue()
    intent = catalogue.analyse_request(LocalModelService.extract_recommendation_clause(message) or message)
    assert intent["genres"] == ["Comedy"]


def test_temporary_request_does_not_cancel_explicit_durable_preference() -> None:
    model = object.__new__(LocalModelService)
    model.settings = Settings()
    message = "I like sci-fi movies. What should I watch tonight?"
    preferences = model.extract_preferences_fast(message)
    assert [(item["value"], item["sentiment"]) for item in preferences] == [
        ("Science Fiction", "like")
    ]
    assert LocalModelService.extract_recommendation_clause(message) == "What should I watch tonight"


def test_ambiguous_find_or_show_command_yields_title_candidate() -> None:
    assert LocalModelService.extract_direct_title_candidate(
        "find me The Matrix"
    ) == "The Matrix"
    assert LocalModelService.extract_direct_title_candidate(
        "show me The Bad Guys 2"
    ) == "The Bad Guys 2"
    assert LocalModelService.extract_direct_title_candidate(
        "show me movies like The Matrix"
    ) is None
    assert LocalModelService.extract_direct_title_candidate(
        "show me some movies"
    ) is None
    assert LocalModelService.extract_direct_title_candidate(
        "find me Tom Hanks movies"
    ) is None
    assert LocalModelService.extract_direct_title_candidate(
        "find me horror movies"
    ) is None


def test_more_natural_title_availability_and_release_phrasing() -> None:
    assert LocalModelService.extract_catalogue_title_question(
        "Does the catalogue have The Matrix?"
    ) == {"type": "availability", "title": "The Matrix"}
    assert LocalModelService.extract_catalogue_title_question(
        "When did The Matrix come out?"
    ) == {"type": "release", "title": "The Matrix"}


def test_title_references_strip_natural_content_descriptors() -> None:
    assert LocalModelService.extract_catalogue_title_question(
        "Tell me about the movie Dune"
    ) == {"type": "overview", "title": "Dune"}
    assert LocalModelService.extract_catalogue_title_question(
        "Who stars in the show Severance?"
    ) == {"type": "cast", "title": "Severance"}
    assert LocalModelService.extract_direct_title_candidate(
        "show me the movie Dune"
    ) == "Dune"
    assert LocalModelService.extract_watched_title_claim(
        "I watched the film Dune"
    ) == "Dune"
