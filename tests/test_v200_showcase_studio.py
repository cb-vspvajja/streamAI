from __future__ import annotations

import sys
import types
from pathlib import Path

if "couchbase" not in sys.modules:
    couchbase = types.ModuleType("couchbase")
    n1ql = types.ModuleType("couchbase.n1ql")
    options = types.ModuleType("couchbase.options")
    n1ql.QueryScanConsistency = types.SimpleNamespace(REQUEST_PLUS="request_plus")
    options.QueryOptions = object
    sys.modules.update({"couchbase": couchbase, "couchbase.n1ql": n1ql, "couchbase.options": options})

from ui.app.config import Settings
from ui.app.showcase_service import ShowcaseService


class FakeCollection:
    def __init__(self) -> None:
        self.docs = {}

    def upsert(self, key, value):
        self.docs[key] = value


class FakeCatalogue:
    def __init__(self) -> None:
        self._showcase_faults = {}
        self.profiles = FakeCollection()
        self.entitlements = FakeCollection()
        self.profile = {
            "viewerId": "sai",
            "preferredGenres": ["Science Fiction"],
            "dislikedGenres": ["Horror"],
            "preferredThemes": ["Atmospheric"],
            "dislikedThemes": [],
            "preferredPeople": [],
            "dislikedPeople": [],
            "preferredLanguages": ["English"],
            "likedTitleIds": [],
            "dislikedTitleIds": [],
            "watchlistTitleIds": [],
            "genreAffinities": {},
            "themeAffinities": {},
            "recommendationVersion": 1,
        }

    def set_showcase_faults(self, faults):
        self._showcase_faults = dict(faults)

    def get_profile(self, _login_id):
        return dict(self.profile)

    def ensure_entitlement(self, _login_id):
        return {"viewerId": "sai", "regionCode": "GB", "subscriptionTier": "standard", "maxParentalRating": "18"}

    def invalidate_home_cache(self, _login_id):
        return None

    def title(self, title_id):
        return {
            "id": title_id,
            "title": "Arrival",
            "genres": ["Science Fiction", "Drama"],
            "keywords": ["Atmospheric"],
            "voteAverage": 8.0,
            "availability": {"regions": ["GB"], "minimumTier": "standard", "offerType": "included", "available": True},
        }

    def _profile_match_evidence(self, _title, _profile):
        return {"score": 5, "summary": "Matched Science Fiction", "matchedFields": [{"field": "genres", "value": "Science Fiction", "matchType": "profile_exact"}]}

    def _preference_violations(self, _title, _profile):
        return []

    def check_entitlement(self, _login_id, title, entitlement=None):
        entitlement = entitlement or self.ensure_entitlement("sai")
        allowed = entitlement.get("regionCode") == "GB" and entitlement.get("subscriptionTier") in {"standard", "premium"}
        return {"allowed": allowed, "includedInPlan": allowed, "reason": "Included in the current plan" if allowed else "not licensed or tier denied", "title": title["title"]}

    def _analyse_query(self, query):
        if query.lower() == "thriller":
            return {"genres": ["Thriller"], "country": None, "semanticTerms": [], "cleaned": "", "expandedTerms": []}
        return {"genres": [], "country": None, "semanticTerms": [query], "cleaned": query, "expandedTerms": []}

    def search(self, *, query, mode, login_id, content_type, limit, purpose):
        return {"results": [{"id": f"{mode}::1", "title": f"{mode.upper()} result", "genres": ["Thriller"], "matchPct": 98}], "trace": {"requestedMode": mode, "effectiveMode": mode, "candidateCount": 1}}

    def health(self):
        return {"catalogue_count": 10, "search_healthy": True}


def bare_service() -> ShowcaseService:
    service = object.__new__(ShowcaseService)
    service.settings = Settings()
    service.catalogue = FakeCatalogue()
    service.agent_catalog_gateway = None
    service.capella_ai_services = None
    service.showcase_state = FakeCollection()
    service.experiments = FakeCollection()
    service.evaluations = FakeCollection()
    service.profile_snapshots = FakeCollection()
    service._faults = {key: False for key in ("mcp_unavailable", "embedding_timeout", "model_unavailable", "search_unavailable", "agent_memory_unavailable")}
    return service


def test_v2_showcase_ui_and_endpoints_are_present() -> None:
    root = Path(__file__).resolve().parents[1]
    page = (root / "ui/static/index.html").read_text()
    script = (root / "ui/static/app.js").read_text()
    main = (root / "ui/app/main.py").read_text()
    assert 'id="showcaseDialog"' in page
    assert "Showcase Studio" in page
    assert "Search Lab" in page
    assert "Governance" in page
    assert "Resilience" in page
    assert "Evaluation" in page
    assert "loadEvidenceScorecard" in script
    assert '@app.post("/api/showcase/search-lab")' in main
    assert '@app.get("/api/showcase/governance")' in main
    assert '@app.post("/api/showcase/evaluate")' in main


def test_showcase_collections_and_indexes_are_provisioned() -> None:
    root = Path(__file__).resolve().parents[1]
    provision = (root / "tools/provision_content_plane.py").read_text()
    shell = (root / "scripts/03-setup-content-plane.sh").read_text()
    for name in ("showcase_state", "experiments", "evaluations", "profile_snapshots"):
        assert name in provision
        assert name in shell
    assert "ix_profile_snapshots_viewer_time" in provision
    assert "ix_experiments_viewer_time" in shell


def test_fault_state_is_persisted_and_applied_to_catalogue() -> None:
    service = bare_service()
    result = service.set_faults("sai", {"embedding_timeout": True, "mcp_unavailable": True})
    assert result["faults"]["embedding_timeout"] is True
    assert service.catalogue._showcase_faults["mcp_unavailable"] is True
    assert service.showcase_state.docs


def test_search_lab_compares_all_three_modes_and_stores_experiment() -> None:
    service = bare_service()
    result = service.search_lab("sai", "atmospheric thriller", None, 6)
    assert set(result["modes"]) == {"fts", "vector", "hybrid"}
    assert result["autoMode"] == "hybrid"
    assert result["experimentId"]
    assert service.experiments.docs


def test_recommendation_evidence_is_deterministic_and_entitlement_aware() -> None:
    service = bare_service()
    result = service.recommendation_evidence("sai", "movie::arrival", {"searchScore": 4.0, "effectiveSearchMode": "hybrid"})
    assert result["eligible"] is True
    assert result["score"] > 0
    meaning = result["scoreMeaning"].lower()
    assert "heuristic" in meaning
    assert "not measured relevance, accuracy or a probability" in meaning
    assert any(component["name"] == "Profile genre affinity" for component in result["components"])


def test_entitlement_simulator_does_not_mutate_authoritative_profile() -> None:
    service = bare_service()
    result = service.simulate_entitlement("sai", "movie::arrival", {"region": "US", "tier": "basic"})
    assert result["mutated"] is False
    assert result["baseline"]["allowed"] is True
    assert result["decision"]["allowed"] is False


def test_presenter_steps_cover_core_ai_data_plane_story() -> None:
    ids = {step["id"] for step in ShowcaseService.DEMO_STEPS}
    assert {"health", "preference", "memory", "search-lab", "entitlement", "action", "trace", "resilience", "value"}.issubset(ids)
    assert len(ShowcaseService.PERSONAS) == 3
