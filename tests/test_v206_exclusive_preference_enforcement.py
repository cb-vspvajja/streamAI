from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

try:
    import httpx  # noqa: F401
except ModuleNotFoundError:
    httpx = types.ModuleType("httpx")
    httpx.TimeoutException = type("TimeoutException", (Exception,), {})
    httpx.ReadTimeout = type("ReadTimeout", (httpx.TimeoutException,), {})
    sys.modules["httpx"] = httpx

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
    sys.modules.update(
        {
            "couchbase": couchbase,
            "couchbase.auth": auth,
            "couchbase.cluster": cluster,
            "couchbase.exceptions": exceptions,
            "couchbase.n1ql": n1ql,
            "couchbase.options": options,
        }
    )

from ui.app.catalogue_service import CatalogueService
from ui.app.config import Settings
from ui.app.llm_service import LocalModelService
from ui.app.mcp_gateway import MCPCall, MCPGateway
from ui.app.preference_policy import (
    exclusive_content_conflict,
    exclusive_content_policy,
    request_conflicts_with_exclusive_policy,
)


FAMILY_PROFILE = {
    "exclusivePreferenceMode": "content",
    "preferredGenres": ["Family"],
    "preferredThemes": [],
    "dislikedGenres": [],
    "dislikedThemes": [],
    "dislikedPeople": [],
    "likedTitleIds": [],
    "dislikedTitleIds": [],
    "watchlistTitleIds": [],
    "avoidGraphicViolence": False,
    "avoidAdultContent": False,
}
FAMILY_TITLE = {
    "id": "movie::family",
    "title": "Family Film",
    "contentType": "movie",
    "genres": ["Family", "Animation"],
    "keywords": ["friendship"],
    "ageRating": "U",
    "availability": {
        "available": True,
        "regions": ["GB"],
        "minimumTier": "standard",
        "offerType": "included",
    },
}
HORROR_TITLE = {
    "id": "movie::horror",
    "title": "Horror Film",
    "contentType": "movie",
    "genres": ["Horror", "Thriller"],
    "keywords": ["haunted house"],
    "ageRating": "15",
    "availability": {
        "available": True,
        "regions": ["GB"],
        "minimumTier": "standard",
        "offerType": "included",
    },
}


def bare_entitlement_service() -> CatalogueService:
    service = object.__new__(CatalogueService)
    service.settings = Settings()
    service.ensure_entitlement = lambda _login_id: {
        "regionCode": "GB",
        "subscriptionTier": "standard",
        "maxParentalRating": "18",
    }
    service.get_profile = lambda _login_id: dict(FAMILY_PROFILE)

    class History:
        def get(self, _key):
            raise RuntimeError("no progress")

    service.watch_history = History()
    return service


def test_exclusive_family_policy_detects_horror_but_allows_family() -> None:
    policy = exclusive_content_policy(FAMILY_PROFILE)
    assert policy == {
        "active": True,
        "mode": "content",
        "allowedGenres": ["Family"],
        "allowedThemes": [],
        "label": "Family",
    }
    assert exclusive_content_conflict(FAMILY_TITLE, FAMILY_PROFILE) is None
    conflict = exclusive_content_conflict(HORROR_TITLE, FAMILY_PROFILE)
    assert conflict is not None
    assert conflict["code"] == "exclusive_content_preference"
    assert "exclusive Family preference" in conflict["reason"]
    assert request_conflicts_with_exclusive_policy(["Horror"], policy) is True
    assert request_conflicts_with_exclusive_policy(["Family"], policy) is False


def test_exclusive_family_policy_filters_horror_from_search_and_recommendations() -> None:
    service = object.__new__(CatalogueService)
    signals = {
        "profile": dict(FAMILY_PROFILE),
        "liked": set(),
        "disliked": set(),
        "watchlist": set(),
        "watched": set(),
        "completed": set(),
        "topPickExclusions": set(),
        "heroExclusions": set(),
    }
    service.viewer_signals = lambda _login_id, **_kwargs: signals
    service._apply_entitlement_policy = (
        lambda docs, _login_id, *, remove_denied: docs
    )

    for purpose in ("search", "recommendation"):
        results = service._exclude_by_purpose(
            [dict(HORROR_TITLE), dict(FAMILY_TITLE)],
            "sai",
            purpose,
        )
        assert [item["id"] for item in results] == ["movie::family"]


def test_full_horror_search_returns_policy_conflict_under_family_only_mode() -> None:
    service = object.__new__(CatalogueService)
    service.settings = Settings()

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"hits": [{"id": "movie::horror", "score": 9.0}]}

    class SearchClient:
        def post(self, _path, *, json, timeout):
            return Response()

    service._search_client = SearchClient()
    service._fetch_hits = lambda _hits, content_type=None: [dict(HORROR_TITLE)]
    service.get_profile = lambda _login_id: dict(FAMILY_PROFILE)
    service.count_titles = lambda **_kwargs: {"count": 1}
    service._write_trace = lambda *_args, **_kwargs: None

    result = service.search(
        query="show me horror movies",
        mode="fts",
        login_id="sai",
        purpose="recommendation",
        limit=10,
    )

    assert result["results"] == []
    assert result["trace"]["structuredGenres"] == ["Horror"]
    assert result["trace"]["exclusivePreferenceConflict"] is True
    assert result["trace"]["exclusivePreferencePolicy"]["allowedGenres"] == ["Family"]
    assert result["trace"]["temporaryPreferenceOverrides"] == []


def test_direct_entitlement_denies_horror_under_family_only_policy() -> None:
    service = bare_entitlement_service()
    denied = service.check_entitlement("sai", dict(HORROR_TITLE))
    allowed = service.check_entitlement("sai", dict(FAMILY_TITLE))

    assert denied["allowed"] is False
    assert denied["includedInPlan"] is False
    assert denied["preferenceConflict"] is True
    assert denied["preferenceConflictCode"] == "exclusive_content_preference"
    assert "exclusive Family preference" in denied["reason"]
    assert allowed["allowed"] is True
    assert allowed["includedInPlan"] is True
    assert allowed["preferenceConflict"] is False


def test_like_and_play_are_rejected_before_any_profile_or_history_write() -> None:
    service = bare_entitlement_service()
    service.title = lambda _title_id: dict(HORROR_TITLE)

    class NoWrites:
        def upsert(self, *_args, **_kwargs):
            raise AssertionError("exclusive-policy denial must happen before writes")

    service.interactions = NoWrites()
    service.profiles = NoWrites()
    service.watch_history = NoWrites()

    for action in ("like", "play"):
        try:
            service.record_interaction(
                login_id="sai",
                title_id="movie::horror",
                action=action,
                progress_pct=5.0 if action == "play" else None,
            )
        except ValueError as exc:
            assert "exclusive Family preference" in str(exc)
        else:
            raise AssertionError(f"{action} should have been denied")


def test_upgrade_reconciles_existing_out_of_policy_likes_and_watchlist() -> None:
    service = object.__new__(CatalogueService)
    service.title = lambda title_id: (
        dict(FAMILY_TITLE) if title_id == "movie::family" else dict(HORROR_TITLE)
    )
    profile = {
        **FAMILY_PROFILE,
        "likedTitleIds": ["movie::horror", "movie::family"],
        "watchlistTitleIds": ["movie::horror"],
    }

    changed = service._reconcile_exclusive_profile_state(profile)

    assert changed is True
    assert profile["likedTitleIds"] == ["movie::family"]
    assert profile["watchlistTitleIds"] == []
    assert profile["exclusivePolicyEnforcementVersion"] == 1
    assert profile["exclusivePolicyReconciledAt"] > 0


def test_mcp_entitlement_path_applies_the_same_exclusive_policy() -> None:
    settings = SimpleNamespace(
        mcp_enabled=True,
        mcp_required=True,
        mcp_url="http://localhost:8000/mcp",
        mcp_timeout_seconds=8,
        mcp_bearer_token="",
        mcp_read_only=True,
        catalogue_scope="catalogue",
        titles_collection="titles",
        viewers_scope="viewers",
        profiles_collection="profiles",
        operations_scope="operations",
        entitlements_collection="entitlements",
    )
    gateway = MCPGateway(settings)

    async def fake_get_document(scope, collection, document_id):
        if document_id == "movie::horror":
            result = dict(HORROR_TITLE)
        elif document_id == "profile::sai":
            result = dict(FAMILY_PROFILE)
        else:
            result = {
                "regionCode": "GB",
                "subscriptionTier": "standard",
                "maxParentalRating": "18",
            }
        return MCPCall(
            tool="get_document_by_id",
            arguments={
                "scope_name": scope,
                "collection_name": collection,
                "document_id": document_id,
            },
            result=result,
            duration_ms=1.0,
        )

    gateway._get_document = fake_get_document  # type: ignore[method-assign]
    result = asyncio.run(
        gateway.call_business_tool(
            "check_entitlement",
            {"viewer_id": "sai", "title_id": "movie::horror"},
        )
    )
    assert result["allowed"] is False
    assert result["preferenceConflictCode"] == "exclusive_content_preference"
    assert "exclusive Family preference" in result["reason"]


def test_published_agent_catalog_entitlement_tool_applies_exclusive_policy() -> None:
    if "agentc" not in sys.modules:
        agentc = types.ModuleType("agentc")
        agentc.catalog = types.SimpleNamespace(tool=lambda func: func)
        sys.modules["agentc"] = agentc

    from ui.agent_catalog.tools import streamai_mcp_tools as native_tools

    native_tools.get_viewer_context = lambda _viewer_id: dict(FAMILY_PROFILE)

    def document(_scope, _collection, document_id):
        if document_id == "movie::horror":
            return dict(HORROR_TITLE)
        return {
            "regionCode": "GB",
            "subscriptionTier": "standard",
            "maxParentalRating": "18",
        }

    native_tools._document = document
    result = native_tools.check_entitlement("sai", "movie::horror")

    assert result["allowed"] is False
    assert result["preferenceConflictCode"] == "exclusive_content_preference"
    assert "exclusive Family preference" in result["reason"]


def test_exclusive_policy_conflict_has_a_clear_viewer_reply() -> None:
    reply = LocalModelService.render_catalogue_recommendations(
        "show me horror movies",
        [],
        {
            "structuredGenres": ["Horror"],
            "structuredContentType": "movie",
            "catalogueInventoryCount": 12,
            "exclusivePreferencePolicy": {
                "active": True,
                "mode": "content",
                "allowedGenres": ["Family"],
                "allowedThemes": [],
                "label": "Family",
            },
            "exclusivePreferenceConflict": True,
        },
    )
    assert "set to only Family content" in reply
    assert "did not return Horror movies" in reply
    assert "searching, liking or playing" in reply


def test_governed_action_and_published_tool_paths_contain_the_policy_gate() -> None:
    root = Path(__file__).resolve().parents[1]
    main = (root / "ui/app/main.py").read_text()
    catalogue_service = (root / "ui/app/catalogue_service.py").read_text()
    native_tool = (
        root / "ui/agent_catalog/tools/streamai_mcp_tools.py"
    ).read_text()

    assert 'action in {"like_title", "watchlist_add"}' in main
    assert "return self._apply_entitlement_policy(items, login_id, remove_denied=True)" in catalogue_service
    assert 'profile.pop("exclusivePolicyEnforcementVersion", None)' in catalogue_service
    assert "exclusive_content_preference" in native_tool
    assert "_exclusive_content_conflict(title, profile)" in native_tool
