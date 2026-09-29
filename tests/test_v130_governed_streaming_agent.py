from __future__ import annotations

import sys
import time
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

from ui.app.agent_service import GovernedAgentService
from ui.app.catalogue_service import CatalogueService
from ui.app.config import Settings
from ui.app.llm_service import LocalModelService


class _NoopCatalogue:
    def __init__(self) -> None:
        self.synced = []
        self.traces = []

    def sync_agent_catalog(self, documents):
        self.synced = documents

    def write_agent_trace(self, document):
        self.traces.append(document)


def test_agent_catalog_has_read_policy_and_action_tools() -> None:
    catalogue = _NoopCatalogue()
    agent = GovernedAgentService(catalogue)
    snapshot = agent.catalog_snapshot()
    tools = {item["name"]: item for item in snapshot["tools"]}

    assert "find_available_content" in tools
    assert "check_entitlement" in tools
    assert tools["update_watchlist"]["mutating"] is True
    assert tools["record_not_interested"]["mutating"] is True
    assert "Never name" in snapshot["prompt"]["content"]
    assert any(item["type"] == "streamai_agent_prompt" for item in catalogue.synced)


def test_follow_up_ordinal_action_resolves_previous_catalogue_result() -> None:
    request = GovernedAgentService.extract_action_request(
        "Add the second one to My List",
        {
            "resultTitleIds": ["movie::one", "movie::two"],
            "resultTitles": ["One", "Two"],
        },
    )
    assert request == {
        "action": "watchlist_add",
        "reference": "second one",
        "resolvedTitleId": "movie::two",
        "resolvedTitle": "Two",
        "source": "previous_results",
    }


def test_direct_action_requires_a_catalogue_title_reference() -> None:
    request = GovernedAgentService.extract_action_request(
        "Please play Disclosure Day", None
    )
    assert request["action"] == "play"
    assert request["reference"] == "Disclosure Day"
    assert request["resolvedTitleId"] is None
    assert request["source"] == "natural_title_reference"


def _bare_entitlement_service(profile: dict, entitlement: dict) -> CatalogueService:
    service = object.__new__(CatalogueService)
    service.settings = Settings()
    service.ensure_entitlement = lambda _login_id: entitlement
    service.get_profile = lambda _login_id: profile

    class _History:
        def get(self, _key):
            raise RuntimeError("no history")

    service.watch_history = _History()
    return service


def test_entitlement_allows_included_title_for_region_and_tier() -> None:
    service = _bare_entitlement_service(
        {"avoidAdultContent": False},
        {"regionCode": "GB", "subscriptionTier": "standard", "maxParentalRating": "18"},
    )
    decision = service.check_entitlement(
        "sai",
        {
            "id": "m1",
            "title": "Catalogue Film",
            "ageRating": "12",
            "availability": {
                "available": True,
                "regions": ["GB", "IE"],
                "minimumTier": "standard",
                "offerType": "included",
            },
        },
    )
    assert decision["allowed"] is True
    assert decision["includedInPlan"] is True
    assert decision["decision"] == "allow"


def test_entitlement_denies_region_tier_and_parental_conflicts() -> None:
    service = _bare_entitlement_service(
        {"avoidAdultContent": False},
        {"regionCode": "GB", "subscriptionTier": "basic", "maxParentalRating": "12"},
    )
    decision = service.check_entitlement(
        "sai",
        {
            "id": "m2",
            "title": "Restricted Film",
            "ageRating": "18",
            "availability": {
                "available": True,
                "regions": ["US"],
                "minimumTier": "premium",
                "offerType": "included",
            },
        },
    )
    assert decision["allowed"] is False
    assert "not licensed in region GB" in decision["reason"]
    assert "requires the premium subscription tier" in decision["reason"]
    assert "parental policy" in decision["reason"]


def test_separate_purchase_is_catalogue_access_not_included_playback() -> None:
    service = _bare_entitlement_service(
        {"avoidAdultContent": False},
        {"regionCode": "GB", "subscriptionTier": "standard", "maxParentalRating": "18"},
    )
    decision = service.check_entitlement(
        "sai",
        {
            "id": "m3",
            "title": "Rental Film",
            "availability": {
                "available": True,
                "regions": ["GB"],
                "minimumTier": "standard",
                "offerType": "rent",
            },
        },
    )
    assert decision["allowed"] is True
    assert decision["includedInPlan"] is False
    assert decision["reason"] == "Available as a separate purchase"


def test_adult_content_safety_is_part_of_entitlement_decision() -> None:
    service = _bare_entitlement_service(
        {
            "avoidAdultContent": True,
            "dislikedGenres": [],
            "dislikedThemes": [],
            "dislikedPeople": [],
            "avoidGraphicViolence": False,
        },
        {"regionCode": "GB", "subscriptionTier": "standard", "maxParentalRating": "18"},
    )
    decision = service.check_entitlement(
        "sai",
        {
            "id": "m4",
            "title": "Explicit Film",
            "keywords": ["sexual content"],
            "genres": ["Drama"],
            "castNames": [],
            "directorNames": [],
            "availability": {"regions": ["GB"], "offerType": "included"},
        },
    )
    assert decision["allowed"] is False
    assert "adult-content preference" in decision["reason"]


def test_availability_answer_uses_entitlement_not_catalogue_presence_alone() -> None:
    denied = LocalModelService.render_title_information(
        {"type": "availability", "title": "Restricted Film"},
        {
            "title": "Restricted Film",
            "releaseYear": 2026,
            "entitlement": {"allowed": False, "reason": "not licensed in region GB"},
        },
    )
    included = LocalModelService.render_title_information(
        {"type": "availability", "title": "Included Film"},
        {
            "title": "Included Film",
            "releaseYear": 2026,
            "entitlement": {"allowed": True, "includedInPlan": True},
        },
    )
    assert "not available to this viewer" in denied
    assert "not licensed in region GB" in denied
    assert "included in your current plan" in included


def test_agent_trace_redacts_sensitive_inputs_and_persists_grounding() -> None:
    catalogue = _NoopCatalogue()
    agent = GovernedAgentService(catalogue)
    trace = agent.new_trace(viewer_id="sai", session_id="session-1", user_message="Play One")
    agent.set_plan(trace, intent="governed_viewer_action", tools=["check_entitlement"])
    started = time.perf_counter()
    agent.record_tool(
        trace,
        name="check_entitlement",
        started=started,
        status="completed",
        inputs={"viewer_id": "sai", "token": "secret"},
        output={"allowed": True, "titleId": "m1", "title": "One"},
    )
    document = agent.finish_trace(
        trace,
        response_mode="governed_action_grounded",
        assistant_response="Starting One.",
        grounded_title_ids=["m1"],
        llm_invoked=False,
        response_ms=12.3,
    )
    assert document["groundedTitleIds"] == ["m1"]
    assert document["spans"][0]["inputs"]["token"] == "[redacted]"
    assert document["spans"][0]["durationMs"] >= 0
    assert document["status"] == "completed"
    assert catalogue.traces[-1]["traceId"] == document["traceId"]


def test_static_ui_contains_presenter_agent_inspector() -> None:
    html = Path("ui/static/index.html").read_text()
    script = Path("ui/static/app.js").read_text()
    assert 'id="agentInspectorSection"' in html
    assert 'id="assistantInspectorButton"' in html
    assert 'id="detailWatchlist"' in html
    assert 'api("/api/agent/traces?limit=20")' in script
    assert "renderAgentInspector" in script
