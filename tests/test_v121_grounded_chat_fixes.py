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


class _Response:
    def __init__(self, hits):
        self._hits = hits

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {"hits": self._hits}


class _SearchClient:
    def __init__(self, hits):
        self.hits = hits
        self.payload = None

    def post(self, _path, *, json, timeout):
        self.payload = json
        return _Response(self.hits)


def viewer_signals(profile):
    return {
        "profile": profile,
        "disliked": set(),
        "liked": set(),
        "watched": set(),
        "watchlist": set(),
        "topPickExclusions": set(),
    }


def test_options_and_choices_are_not_catalogue_search_terms() -> None:
    service = bare_catalogue()
    for prompt, genre in (
        ("show me some horror movie options", "Horror"),
        ("show me comedy movie choices", "Comedy"),
        ("show me a science fiction movie selection", "Science Fiction"),
    ):
        intent = service.analyse_request(prompt)
        assert intent["genres"] == [genre]
        assert intent["contentType"] == "movie"
        assert intent["terms"] == []


def test_structured_genre_search_uses_exact_terms_without_embedding() -> None:
    service = bare_catalogue()
    service._search_client = _SearchClient([{"id": "sf", "score": 8.0}])
    service._fetch_hits = lambda _hits, content_type=None: [
        {"id": "sf", "title": "Disclosure Day", "contentType": "movie", "genres": ["Science Fiction", "Thriller"], "keywords": []}
    ]
    service.viewer_signals = lambda _login_id: viewer_signals({
        "dislikedGenres": [], "dislikedThemes": [], "dislikedPeople": [], "avoidGraphicViolence": False,
    })
    service.get_profile = lambda _login_id: {"dislikedGenres": []}
    service._write_trace = lambda *_args, **_kwargs: None
    service.embed_with_trace = lambda _query: (_ for _ in ()).throw(AssertionError("embedding should be skipped"))

    result = service.search(
        query="Show me some science fiction movie options",
        mode="hybrid",
        login_id="sai",
        purpose="recommendation",
        limit=10,
    )

    assert [item["id"] for item in result["results"]] == ["sf"]
    conjuncts = service._search_client.payload["query"]["conjuncts"]
    assert {"term": "Science Fiction", "field": "genres"} in conjuncts
    assert {"term": "movie", "field": "contentType"} in conjuncts
    assert result["trace"]["embeddingModelCalls"] == 0
    assert result["trace"]["embeddingSkippedReason"] == "structured_filter_only"


def test_empty_search_index_result_recovers_from_exact_sqlpp_query() -> None:
    service = bare_catalogue()
    service._search_client = _SearchClient([])
    service._fetch_hits = lambda _hits, content_type=None: []
    service._fallback_query = lambda **_kwargs: [
        {"id": "comedy", "title": "Moana", "contentType": "movie", "genres": ["Family", "Fantasy", "Comedy"], "keywords": [], "popularity": 90}
    ]
    service.viewer_signals = lambda _login_id: viewer_signals({
        "dislikedGenres": [], "dislikedThemes": [], "dislikedPeople": [], "avoidGraphicViolence": False,
    })
    service.get_profile = lambda _login_id: {"dislikedGenres": []}
    service._write_trace = lambda *_args, **_kwargs: None

    result = service.search(
        query="show me some comedy movie options",
        mode="fts",
        login_id="sai",
        purpose="recommendation",
        limit=10,
    )

    assert [item["id"] for item in result["results"]] == ["comedy"]
    assert result["trace"]["mode"] == "sqlpp_exact_fallback"


def test_profile_terms_are_alternatives_not_all_genres_required() -> None:
    service = bare_catalogue()
    service._search_client = _SearchClient([])
    service._fetch_hits = lambda _hits, content_type=None: []
    service._fallback_query = lambda **_kwargs: []
    service.viewer_signals = lambda _login_id: viewer_signals({
        "dislikedGenres": [], "dislikedThemes": [], "dislikedPeople": [], "avoidGraphicViolence": False,
    })
    service._write_trace = lambda *_args, **_kwargs: None

    service.search(
        query="Science Fiction Thriller Crime Christopher Nolan Denis Villeneuve",
        mode="hybrid",
        login_id="sai",
        content_type="movie",
        purpose="recommendation",
        explicit_terms=["Science Fiction", "Thriller", "Crime", "Christopher Nolan", "Denis Villeneuve"],
    )

    conjuncts = service._search_client.payload["query"]["conjuncts"]
    lexical = conjuncts[0]
    assert lexical["min"] == 1
    assert {"term": "Science Fiction", "field": "genres", "boost": 6.0} in lexical["disjuncts"]
    assert {"term": "Thriller", "field": "genres", "boost": 6.0} in lexical["disjuncts"]
    assert {"term": "Crime", "field": "genres", "boost": 6.0} in lexical["disjuncts"]
    assert {"term": "movie", "field": "contentType"} in conjuncts
    assert not any(item.get("field") == "genres" for item in conjuncts[1:])


def test_profile_recommendations_never_pad_with_unrelated_popularity() -> None:
    service = bare_catalogue()
    profile = {
        "preferredGenres": ["Science Fiction", "Thriller", "Crime"],
        "preferredThemes": [],
        "preferredPeople": ["Christopher Nolan", "Denis Villeneuve"],
        "preferredLanguages": ["English"],
        "dislikedGenres": ["Horror"],
        "dislikedThemes": [],
        "genreAffinities": {},
        "themeAffinities": {},
    }
    service.get_profile = lambda _login_id: profile
    service.search = lambda **_kwargs: {
        "results": [
            {"id": "disclosure", "title": "Disclosure Day", "contentType": "movie", "genres": ["Science Fiction", "Thriller"], "keywords": [], "directorNames": [], "castNames": [], "popularity": 85},
            {"id": "minions", "title": "Minions & Monsters", "contentType": "movie", "genres": ["Adventure", "Animation", "Comedy"], "keywords": [], "directorNames": [], "castNames": [], "popularity": 99},
        ],
        "trace": {"mode": "fts", "services": ["Search/FTS", "Data/KV"]},
    }
    service._trending_items = lambda _limit: [
        {"id": "odyssey", "title": "The Odyssey", "contentType": "movie", "genres": ["Adventure", "Action", "Fantasy"], "keywords": [], "directorNames": ["Christopher Nolan"], "castNames": [], "popularity": 98},
        {"id": "moana", "title": "Moana", "contentType": "movie", "genres": ["Family", "Fantasy", "Comedy"], "keywords": [], "directorNames": [], "castNames": [], "popularity": 97},
        {"id": "minions", "title": "Minions & Monsters", "contentType": "movie", "genres": ["Adventure", "Animation", "Comedy"], "keywords": [], "directorNames": [], "castNames": [], "popularity": 96},
    ]
    service._exclude_by_purpose = lambda docs, _login_id, _purpose: docs
    service._write_trace = lambda *_args, **_kwargs: None

    result = service.recommend_for_profile(login_id="sai", mode="hybrid", limit=4, content_type="movie")

    assert [item["id"] for item in result["results"]] == ["disclosure", "odyssey"]
    assert "Science Fiction" in result["results"][0]["matchExplanation"]["summary"]
    assert "Christopher Nolan" in result["results"][1]["matchExplanation"]["summary"]
    assert result["trace"]["profileMatchLimited"] is True
    assert "moana" not in result["trace"]["selectedTitleIds"]
    assert "minions" not in result["trace"]["selectedTitleIds"]

    answer = LocalModelService.render_profile_recommendations(result["results"], profile, result["trace"])
    assert "did not pad the list" in answer
    assert "Moana" not in answer
    assert "Minions" not in answer


def test_natural_people_search_preserves_keyword_field_case() -> None:
    service = bare_catalogue()
    intent = service.analyse_request("show me Tom Hanks movies")
    lexical = service._lexical_query(intent)
    assert {"term": "Tom Hanks", "field": "castNames", "boost": 3.0} in lexical["disjuncts"]
    assert {"term": "Tom Hanks", "field": "directorNames", "boost": 5.0} in lexical["disjuncts"]
