from __future__ import annotations

import sys
import types
from pathlib import Path

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
from ui.app.showcase_service import ShowcaseService
import ui.app.catalogue_service as catalogue_module


def bare_catalogue() -> CatalogueService:
    service = object.__new__(CatalogueService)
    service.settings = Settings()
    return service


def test_motorcycle_fts_card_names_biker_query_expansion() -> None:
    service = bare_catalogue()
    intent = service._analyse_query("motorcycle")
    explanation = service._match_explanation(
        {
            "title": "Vixen",
            "overview": "She even sleeps with her biker brother.",
            "genres": ["Drama"],
            "keywords": [],
        },
        {"fragments": {"overview": ["She even sleeps with her <mark>biker</mark> brother."]}},
        intent,
        effective_mode="fts",
        requested_mode="fts",
    )
    doc = {"searchScore": 5.0, "matchExplanation": explanation}
    service._finalise_display_scores(
        [doc], requested_mode="fts", effective_mode="fts", ranking_strategy="couchbase_search_score"
    )
    field = explanation["matchedFields"][0]
    assert field["matchedTerm"].lower() == "biker"
    assert field["matchType"] == "expanded_lexical_term"
    assert "biker" in doc["matchExplanation"]["cardEvidence"].lower()
    assert "query expansion" in doc["matchExplanation"]["cardEvidence"].lower()


def test_vector_only_card_is_honest_about_no_direct_motorcycle_term() -> None:
    service = bare_catalogue()
    intent = service._analyse_query("motorcycle")
    explanation = service._match_explanation(
        {
            "title": "Hoppers",
            "overview": "Scientists hop human consciousness into robotic animals.",
            "genres": ["Animation", "Adventure"],
            "keywords": ["animals"],
        },
        None,
        intent,
        effective_mode="vector",
        requested_mode="vector",
    )
    doc = {"searchScore": 0.42, "matchExplanation": explanation}
    service._finalise_display_scores(
        [doc], requested_mode="vector", effective_mode="vector", ranking_strategy="couchbase_search_score"
    )
    assert explanation["semanticOnly"] is True
    assert explanation["directLexicalEvidence"] is False
    assert "no direct query term" in explanation["summary"].lower()
    assert "nearest neighbour only" in doc["matchExplanation"]["cardEvidence"].lower()
    assert doc["matchExplanation"]["rawScoreRelativeToTopPct"] == 100.0


def test_hybrid_vector_only_candidate_is_labelled_as_vector_admission() -> None:
    service = bare_catalogue()
    intent = service._analyse_query("motorcycle")
    explanation = service._match_explanation(
        {"title": "Hoppers", "overview": "Robotic animals", "genres": ["Animation"]},
        None,
        intent,
        effective_mode="hybrid",
        requested_mode="hybrid",
    )
    assert explanation["evidenceType"] == "hybrid_vector_only"
    assert "vector search only" in explanation["summary"].lower()


def test_home_exposes_my_list_and_uses_latest_watch_for_because_row(monkeypatch) -> None:
    service = bare_catalogue()
    service._cache_get = lambda *_args: None
    service._cache_put = lambda *_args: None
    service.get_profile = lambda _login: {"displayName": "Sai"}
    service.viewer_signals = lambda _login: {
        "watchlist": {"movie::odyssey"}, "liked": set(), "disliked": set(),
        "watched": {"movie::seed"}, "completed": set(),
        "topPickExclusions": {"movie::seed", "movie::odyssey"},
        "heroExclusions": {"movie::seed", "movie::odyssey"},
        "profile": {},
    }
    service._fetch_title_ids = lambda ids: [{"id": value, "title": "The Odyssey"} for value in ids]
    service._mark_viewer_state = lambda docs, _signals: [doc.update(viewerState="watchlist") for doc in docs]
    my_list = service.home_row("sai", "my-list")
    assert my_list["items"][0]["viewerState"] == "watchlist"

    class Result:
        def rows(self):
            return iter([{"titleId": "movie::seed", "progressPct": 10, "status": "in_progress"}])

    class Cluster:
        def query(self, *_args, **_kwargs):
            return Result()

    service.cluster = Cluster()
    service._search_client = None
    service.title = lambda _title_id, include_internal=False: {
        "id": "movie::seed", "title": "Arrival", "embedding": None if include_internal else None
    }
    monkeypatch.setattr(catalogue_module, "QueryOptions", lambda **kwargs: kwargs)
    because = service._because_you_watched("sai")
    assert because["title"] == "Because You Started Arrival"


def test_policy_simulator_resolves_a_title_name() -> None:
    service = object.__new__(ShowcaseService)

    class Catalogue:
        def title(self, value):
            raise KeyError(value)

        def resolve_title(self, value):
            return {"match": {"id": "movie::odyssey", "title": "The Odyssey", "availability": {}}}

        def ensure_entitlement(self, _login):
            return {"regionCode": "GB", "subscriptionTier": "standard", "maxParentalRating": "18"}

        def check_entitlement(self, _login, title, entitlement=None):
            return {"allowed": entitlement["regionCode"] == "GB", "reason": title["title"]}

    service.catalogue = Catalogue()
    result = service.simulate_entitlement("sai", "The Odyssey", {"region": "US"})
    assert result["title"]["id"] == "movie::odyssey"
    assert result["requestedTitle"] == "The Odyssey"
    assert result["mutated"] is False


def test_ui_fixes_are_present() -> None:
    root = Path(__file__).resolve().parents[1]
    page = (root / "ui/static/index.html").read_text()
    script = (root / "ui/static/app.js").read_text()
    css = (root / "ui/static/styles.css").read_text()
    assert 'data-row-jump="my-list"' in page
    assert 'id="profileWatchlistTitles"' in page
    assert 'id="policyInlineStatus"' in page
    assert 'id="simulateDecisionButton"' in page
    assert 'step.action === "search_lab"' in script
    assert 'Run comparison' in script
    assert 'cardEvidenceText(item)' in script
    assert 'target.scrollIntoView' in script
    assert 'max-height: 92vh' in css
    assert '.card-evidence' in css
    assert '.policy-error' in css

def test_hybrid_phrase_with_genre_keeps_lexical_and_vector_evidence() -> None:
    service = bare_catalogue()
    intent = service._analyse_query("atmospheric political thriller")
    explanation = service._match_explanation(
        {
            "title": "Example",
            "overview": "An atmospheric political conspiracy.",
            "genres": ["Thriller"],
            "keywords": ["politics"],
        },
        {"fragments": {"overview": ["An <mark>atmospheric</mark> political conspiracy."]}},
        intent,
        effective_mode="hybrid",
        requested_mode="hybrid",
    )
    assert explanation["evidenceType"] == "hybrid"
    assert any(item["field"] == "overview" for item in explanation["matchedFields"])
    assert any(item["field"] == "semanticQuery" for item in explanation["matchedFields"])
    assert explanation["summary"].startswith("Hybrid match")
