from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
import uuid
from datetime import timedelta
from difflib import SequenceMatcher
from typing import Any, Iterable

import httpx
from . import usage_reporter
from couchbase.exceptions import DocumentNotFoundException
from couchbase.n1ql import QueryScanConsistency
from couchbase.options import QueryOptions

from .config import Settings
from .connection import create_cluster
from .preference_policy import (
    exclusive_content_conflict,
    exclusive_content_policy,
    request_conflicts_with_exclusive_policy,
)
from .providers import EmbeddingProvider
from .search_adapter import SdkSearchClient


class CatalogueService:
    """Operational catalogue, viewer profile, history and Search adapter.

    The service treats operational documents as the source of truth and adds governed
    entitlement checks, versioned agent tools and persistent agent traces. The
    LLM is never asked to decide what a viewer watched, liked, disliked, which
    titles exist, or whether a title is available to the subscriber.
    """

    COUNTRY_INTENTS: dict[str, dict[str, Any]] = {
        "indian": {
            "countries": ["IN"],
            "languages": ["hi", "ta", "te", "ml", "kn", "bn", "mr", "pa", "gu"],
            "label": "Indian-origin or Indian-language content",
        },
        "british": {"countries": ["GB"], "languages": ["en"], "label": "British content"},
        "korean": {"countries": ["KR"], "languages": ["ko"], "label": "Korean content"},
        "japanese": {"countries": ["JP"], "languages": ["ja"], "label": "Japanese content"},
        "french": {"countries": ["FR"], "languages": ["fr"], "label": "French content"},
        "spanish": {"countries": ["ES"], "languages": ["es"], "label": "Spanish content"},
        "italian": {"countries": ["IT"], "languages": ["it"], "label": "Italian content"},
        "german": {"countries": ["DE"], "languages": ["de"], "label": "German content"},
    }

    QUERY_EXPANSIONS: dict[str, list[str]] = {
        "motorcycle": ["motorcycle", "motorbike", "biker", "motocross", "motorsport", "road racing"],
        "motorbike": ["motorbike", "motorcycle", "biker", "motocross", "motorsport"],
        "animal": ["animal", "animals", "wildlife", "pets", "dog", "horse", "nature"],
        "space": ["space", "astronaut", "spaceship", "interstellar", "outer space"],
        "sequel": ["sequel", "follow-up", "part 2", "chapter 2", "franchise"],
    }

    # Fast, deterministic recovery for common spoken/typed catalogue phrases.
    # These replacements are deliberately narrow: they correct established
    # content vocabulary without guessing arbitrary title names.
    QUERY_NORMALISATIONS: tuple[tuple[str, str], ...] = (
        (r"\bsify\b", "sci-fi"),
        (r"\bsci[ -]?fi\b", "sci-fi"),
        (r"\bscience[ -]?fiction\b", "science fiction"),
        (r"\bhoror\b", "horror"),
        (r"\btriller\b", "thriller"),
        (r"\brom[ -]?com\b", "romance comedy"),
        (r"\bcomedies\b", "comedy"),
        (r"\bdocumentaries\b", "documentary"),
        (r"\bfantasies\b", "fantasy"),
        (r"\bmysteries\b", "mystery"),
        (r"\b(?:kid|child)[ -]?friendly\b", "family"),
        (r"\bchildren(?:'s)?\b", "family"),
        (r"\bkids?\b", "family"),
        (r"\bfamilies\b", "family"),
        (r"\bhistories\b", "history"),
        (r"\b(?:sequals?|sequels?)\b", "sequel"),
    )

    KNOWN_GENRES = {
        "action": "Action",
        "adventure": "Adventure",
        "animation": "Animation",
        "comedy": "Comedy",
        "crime": "Crime",
        "documentary": "Documentary",
        "drama": "Drama",
        "family": "Family",
        "fantasy": "Fantasy",
        "history": "History",
        "horror": "Horror",
        "music": "Music",
        "mystery": "Mystery",
        "romance": "Romance",
        "science fiction": "Science Fiction",
        "sci-fi": "Science Fiction",
        "thriller": "Thriller",
        "war": "War",
        "western": "Western",
    }

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cluster = create_cluster(settings)
        bucket = self.cluster.bucket(settings.content_bucket)
        catalogue_scope = bucket.scope(settings.catalogue_scope)
        self.titles = catalogue_scope.collection(settings.titles_collection)
        viewer_scope = bucket.scope(settings.viewers_scope)
        self.profiles = viewer_scope.collection(settings.profiles_collection)
        self.watch_history = viewer_scope.collection(settings.watch_history_collection)
        self.interactions = viewer_scope.collection(settings.interactions_collection)
        rec_scope = bucket.scope(settings.recommendations_scope)
        self.generated = rec_scope.collection(settings.generated_collection)
        self.traces = rec_scope.collection(settings.traces_collection)
        self.plan_cache = rec_scope.collection(settings.plan_cache_collection)
        operations_scope = bucket.scope(settings.operations_scope)
        self.entitlements = operations_scope.collection(settings.entitlements_collection)
        self.agent_catalog = operations_scope.collection(settings.agent_catalog_collection)
        self.action_receipts = operations_scope.collection(settings.action_receipts_collection)
        telemetry_scope = bucket.scope(settings.telemetry_scope)
        self.ai_metrics = telemetry_scope.collection(settings.ai_metrics_collection)
        self.showcase_state = telemetry_scope.collection(settings.showcase_state_collection)
        self.experiments = telemetry_scope.collection(settings.experiments_collection)
        self.evaluations = telemetry_scope.collection(settings.evaluations_collection)
        self.profile_snapshots = telemetry_scope.collection(settings.profile_snapshots_collection)
        self._showcase_faults: dict[str, bool] = {}
        self._search_circuit_open_until = 0.0
        self._search_failure_count = 0
        use_sdk_search = settings.search_transport == "sdk" or (settings.search_transport == "auto" and settings.deployment_target == "capella")
        self._search_client = SdkSearchClient(catalogue_scope, settings.catalogue_search_index) if use_sdk_search else httpx.Client(
            base_url=settings.search_url, auth=(settings.cb_username, settings.cb_password), timeout=httpx.Timeout(settings.search_query_timeout_seconds)
        )
        self._plan_search_client = (
            SdkSearchClient(rec_scope, settings.plan_cache_search_index)
            if use_sdk_search
            else self._search_client
        )
        self._embedding_provider = EmbeddingProvider(settings)

    def set_showcase_faults(self, faults: dict[str, Any]) -> None:
        self._showcase_faults = {str(key): bool(value) for key, value in dict(faults or {}).items()}

    def showcase_faults(self) -> dict[str, bool]:
        return dict(self._showcase_faults)

    def _write_profile_snapshot(self, login_id: str, profile: dict[str, Any], cause: str) -> None:
        if not hasattr(self, "profile_snapshots"):
            return
        now = time.time()
        try:
            self.profile_snapshots.upsert(
                f"profile-snapshot::{login_id}::{int(now * 1000)}::{uuid.uuid4().hex[:6]}",
                {
                    "type": "streamai_profile_snapshot",
                    "viewerId": login_id,
                    "cause": cause,
                    "timestamp": now,
                    "recommendationVersion": profile.get("recommendationVersion"),
                    "profile": dict(profile),
                },
            )
        except Exception:
            pass

    def close(self) -> None:
        if self._plan_search_client is not self._search_client:
            self._plan_search_client.close()
        self._search_client.close()
        self._embedding_provider.close()
        close = getattr(self.cluster, "close", None)
        if callable(close):
            close()

    @property
    def search_index_path(self) -> str:
        bucket = urllib.parse.quote(self.settings.content_bucket, safe="")
        scope = urllib.parse.quote(self.settings.catalogue_scope, safe="")
        index = urllib.parse.quote(self.settings.catalogue_search_index, safe="")
        return f"/api/bucket/{bucket}/scope/{scope}/index/{index}"

    @property
    def plan_cache_index_path(self) -> str:
        bucket = urllib.parse.quote(self.settings.content_bucket, safe="")
        scope = urllib.parse.quote(self.settings.recommendations_scope, safe="")
        index = urllib.parse.quote(self.settings.plan_cache_search_index, safe="")
        return f"/api/bucket/{bucket}/scope/{scope}/index/{index}"

    def _keyspace(self, scope: str, collection: str) -> str:
        return f"`{self.settings.content_bucket}`.`{scope}`.`{collection}`"

    @property
    def titles_keyspace(self) -> str:
        return self._keyspace(self.settings.catalogue_scope, self.settings.titles_collection)

    @property
    def history_keyspace(self) -> str:
        return self._keyspace(self.settings.viewers_scope, self.settings.watch_history_collection)

    @property
    def interactions_keyspace(self) -> str:
        return self._keyspace(self.settings.viewers_scope, self.settings.interactions_collection)

    @property
    def traces_keyspace(self) -> str:
        return self._keyspace(self.settings.recommendations_scope, self.settings.traces_collection)

    @property
    def entitlements_keyspace(self) -> str:
        return self._keyspace(self.settings.operations_scope, self.settings.entitlements_collection)

    @property
    def agent_catalog_keyspace(self) -> str:
        return self._keyspace(self.settings.operations_scope, self.settings.agent_catalog_collection)

    @property
    def action_receipts_keyspace(self) -> str:
        return self._keyspace(
            self.settings.operations_scope, self.settings.action_receipts_collection
        )

    def health(self) -> dict[str, Any]:
        catalogue_count = 0
        search_healthy = False
        search_error = None
        try:
            result = self.cluster.query(
                f"SELECT RAW COUNT(1) FROM {self.titles_keyspace}",
                QueryOptions(readonly=True),
            )
            catalogue_count = int(next(iter(result.rows()), 0))
        except Exception as exc:
            return {"healthy": False, "catalogue_count": 0, "error": f"{type(exc).__name__}: {exc}"}
        try:
            response = self._search_client.get(self.search_index_path, timeout=4.0)
            search_healthy = response.status_code == 200
            if not search_healthy:
                search_error = f"HTTP {response.status_code}"
        except Exception as exc:
            search_error = f"{type(exc).__name__}: {exc}"
        return {
            "healthy": catalogue_count > 0,
            "catalogue_count": catalogue_count,
            "search_index": self.settings.catalogue_search_index,
            "search_healthy": search_healthy,
            "search_error": search_error,
            "bucket": self.settings.content_bucket,
        }

    @staticmethod
    def _profile_defaults(login_id: str, name: str | None = None) -> dict[str, Any]:
        now = time.time()
        return {
            "type": "viewer_profile",
            "viewerId": login_id,
            "displayName": name or login_id,
            "preferredGenres": [],
            "dislikedGenres": [],
            "preferredThemes": [],
            "dislikedThemes": [],
            "preferredPeople": [],
            "dislikedPeople": [],
            "preferredLanguages": ["English"],
            "preferredContentTypes": [],
            "likedTitleIds": [],
            "dislikedTitleIds": [],
            "watchlistTitleIds": [],
            "genreAffinities": {},
            "themeAffinities": {},
            "maxRuntimeMinutes": None,
            "avoidGraphicViolence": False,
            "avoidAdultContent": False,
            "recommendationVersion": 1,
            "createdAt": now,
            "updatedAt": now,
        }


    @staticmethod
    def _rating_value(value: Any) -> int:
        text = str(value or "").upper().strip()
        mapping = {
            "U": 0,
            "G": 0,
            "TV-Y": 0,
            "PG": 8,
            "TV-PG": 8,
            "12": 12,
            "12A": 12,
            "TV-12": 12,
            "PG-13": 13,
            "15": 15,
            "TV-14": 14,
            "16": 16,
            "18": 18,
            "R": 18,
            "NC-17": 18,
            "TV-MA": 18,
        }
        if text in mapping:
            return mapping[text]
        match = re.search(r"\d+", text)
        return int(match.group(0)) if match else 0

    def ensure_entitlement(self, login_id: str) -> dict[str, Any]:
        key = f"entitlement::{login_id}"
        try:
            document = self.entitlements.get(key).content_as[dict]
        except DocumentNotFoundException:
            now = time.time()
            document = {
                "type": "streamai_viewer_entitlement",
                "viewerId": login_id,
                "householdId": f"household::{login_id}",
                "regionCode": self.settings.default_region_code,
                "subscriptionTier": self.settings.default_subscription_tier,
                "maxParentalRating": self.settings.default_parental_rating,
                "deviceType": "web-demo",
                "roamingAllowed": True,
                "createdAt": now,
                "updatedAt": now,
            }
            self.entitlements.insert(key, document)
        return document

    def get_entitlement(self, login_id: str) -> dict[str, Any]:
        return self.ensure_entitlement(login_id)


    def update_entitlement_region(self, login_id: str, region_code: str) -> dict[str, Any]:
        code = str(region_code or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", code):
            raise ValueError("region_code must be a two-letter region code")
        key = f"entitlement::{login_id}"
        entitlement = dict(self.ensure_entitlement(login_id))
        entitlement["regionCode"] = code
        entitlement["updatedAt"] = time.time()
        self.entitlements.upsert(key, entitlement)
        self.invalidate_home_cache(login_id)
        return entitlement

    @staticmethod
    def _tier_rank(value: Any) -> int:
        return {"free": 0, "basic": 1, "standard": 2, "premium": 3}.get(
            str(value or "standard").lower(), 2
        )

    def check_entitlement(
        self,
        login_id: str,
        title_or_id: dict[str, Any] | str,
        *,
        entitlement: dict[str, Any] | None = None,
        profile: dict[str, Any] | None = None,
        include_progress: bool = True,
    ) -> dict[str, Any]:
        title = title_or_id if isinstance(title_or_id, dict) else self.title(str(title_or_id))
        # Batch recommendation paths pass a pre-fetched viewer context so one rail
        # does not repeat the same KV reads for every candidate document.
        entitlement = entitlement or self.ensure_entitlement(login_id)
        profile = profile or self.get_profile(login_id)
        availability = dict(title.get("availability") or {})
        regions = [str(value).upper() for value in availability.get("regions", ["GB", "IE"])]
        required_tier = str(availability.get("minimumTier") or "standard").lower()
        offer_type = str(availability.get("offerType") or "included").lower()
        available = bool(availability.get("available", True))
        region = str(entitlement.get("regionCode") or self.settings.default_region_code).upper()
        viewer_tier = str(
            entitlement.get("subscriptionTier") or self.settings.default_subscription_tier
        ).lower()
        max_rating = self._rating_value(entitlement.get("maxParentalRating"))
        title_rating = self._rating_value(title.get("ageRating") or title.get("certification"))

        reasons: list[str] = []
        if not available:
            reasons.append("title is not currently available")
        if regions and region not in regions:
            reasons.append(f"not licensed in region {region}")
        if offer_type == "included" and self._tier_rank(viewer_tier) < self._tier_rank(required_tier):
            reasons.append(f"requires the {required_tier} subscription tier")
        if title_rating > max_rating:
            reasons.append(
                f"parental policy allows rating {entitlement.get('maxParentalRating')} but the title is rated {title.get('ageRating') or title.get('certification')}"
            )
        if profile.get("avoidAdultContent"):
            adult_violations = [
                item for item in self._preference_violations(title, profile)
                if item.startswith("safety preference:")
            ]
            if adult_violations:
                reasons.append("blocked by the viewer's adult-content preference")
        exclusive_conflict = exclusive_content_conflict(title, profile)
        if exclusive_conflict is not None:
            reasons.append(str(exclusive_conflict["reason"]))

        allowed = not reasons
        included = allowed and offer_type == "included"
        decision = {
            "allowed": allowed,
            "decision": "allow" if allowed else "deny",
            "reason": "Included in the current plan" if included else (
                "Available as a separate purchase" if allowed else "; ".join(reasons)
            ),
            "includedInPlan": included,
            "offerType": offer_type,
            "region": region,
            "subscriptionTier": viewer_tier,
            "requiredTier": required_tier,
            "allowedRegions": regions,
            "parentalRating": entitlement.get("maxParentalRating"),
            "titleRating": title.get("ageRating") or title.get("certification"),
            "titleId": title.get("id"),
            "title": title.get("title"),
            "preferencePolicy": (
                exclusive_conflict["policy"]
                if exclusive_conflict is not None
                else exclusive_content_policy(profile)
            ),
            "preferenceConflict": exclusive_conflict is not None,
            "preferenceConflictCode": (
                exclusive_conflict["code"] if exclusive_conflict is not None else None
            ),
        }
        if include_progress:
            try:
                history = self.watch_history.get(
                    f"watch::{login_id}::{title.get('id')}"
                ).content_as[dict]
                if 0 < float(history.get("progressPct") or 0) < 100:
                    decision["resumeProgressPct"] = float(history.get("progressPct") or 0)
            except Exception:
                pass
        return decision

    def _apply_entitlement_policy(
        self,
        docs: list[dict[str, Any]],
        login_id: str | None,
        *,
        remove_denied: bool,
        require_included: bool | None = None,
        entitlement: dict[str, Any] | None = None,
        profile: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if (
            not login_id
            or not self.settings.enable_entitlement_filtering
            or not hasattr(self, "entitlements")
        ):
            return docs
        # Fetch invariant viewer context once per candidate batch. This keeps the
        # entitlement layer inexpensive even when Search returns a wider pool.
        entitlement = entitlement or self.ensure_entitlement(login_id)
        profile = profile or self.get_profile(login_id)
        if require_included is None:
            require_included = remove_denied
        result: list[dict[str, Any]] = []
        for doc in docs:
            decision = self.check_entitlement(
                login_id,
                doc,
                entitlement=entitlement,
                profile=profile,
                include_progress=False,
            )
            doc["entitlement"] = decision
            doc["includedInPlan"] = bool(decision.get("includedInPlan"))
            doc["availabilityStatus"] = decision.get("decision")
            if remove_denied and (
                not decision.get("allowed")
                or (require_included and not decision.get("includedInPlan"))
            ):
                continue
            result.append(doc)
        return result

    def sync_agent_catalog(self, documents: list[dict[str, Any]]) -> None:
        for document in documents:
            name = str(document.get("name") or uuid.uuid4().hex)
            kind = "prompt" if document.get("type") == "streamai_agent_prompt" else "tool"
            self.agent_catalog.upsert(f"agent-{kind}::{name}", document)

    def write_agent_trace(self, document: dict[str, Any]) -> str:
        trace_id = str(document.get("traceId") or f"agent-trace::{uuid.uuid4().hex}")
        self.traces.upsert(trace_id, document)
        return trace_id

    def list_agent_traces(self, login_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        size = max(1, min(int(limit or self.settings.agent_trace_limit), 100))
        rows = self.cluster.query(
            f"SELECT t.* FROM {self.traces_keyspace} AS t "
            "WHERE t.viewerId=$viewerId AND t.type='streamai_agent_trace' "
            "ORDER BY t.timestamp DESC LIMIT $limit",
            QueryOptions(
                named_parameters={"viewerId": login_id, "limit": size},
                readonly=True,
                scan_consistency=QueryScanConsistency.REQUEST_PLUS,
            ),
        ).rows()
        return list(rows)

    def get_agent_trace(self, login_id: str, trace_id: str) -> dict[str, Any]:
        document = self.traces.get(trace_id).content_as[dict]
        if str(document.get("viewerId")) != login_id:
            raise ValueError("Trace does not belong to the signed-in viewer.")
        return document


    def list_agent_session_traces(
        self, login_id: str, session_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        size = max(1, min(int(limit or 100), 250))
        rows = self.cluster.query(
            f"SELECT t.* FROM {self.traces_keyspace} AS t "
            "WHERE t.viewerId=$viewerId AND t.sessionId=$sessionId "
            "AND t.type='streamai_agent_trace' "
            "ORDER BY t.startedAt ASC LIMIT $limit",
            QueryOptions(
                named_parameters={
                    "viewerId": login_id,
                    "sessionId": session_id,
                    "limit": size,
                },
                readonly=True,
                scan_consistency=QueryScanConsistency.REQUEST_PLUS,
            ),
        ).rows()
        return list(rows)

    def ensure_profile(self, login_id: str, name: str) -> dict[str, Any]:
        key = f"profile::{login_id}"
        try:
            profile = self.profiles.get(key).content_as[dict]
        except DocumentNotFoundException:
            profile = self._profile_defaults(login_id, name)
            self.profiles.insert(key, profile)
            try:
                self.ensure_entitlement(login_id)
            except Exception:
                pass
            return profile
        migrated = self._migrate_profile(profile, login_id, name)
        try:
            self.ensure_entitlement(login_id)
        except Exception:
            pass
        return migrated

    def get_profile(self, login_id: str) -> dict[str, Any]:
        key = f"profile::{login_id}"
        try:
            profile = self.profiles.get(key).content_as[dict]
        except DocumentNotFoundException:
            profile = self._profile_defaults(login_id)
            self.profiles.insert(key, profile)
            try:
                self.ensure_entitlement(login_id)
            except Exception:
                pass
            return profile
        migrated = self._migrate_profile(profile, login_id, None)
        try:
            self.ensure_entitlement(login_id)
        except Exception:
            pass
        return migrated

    def _migrate_profile(self, profile: dict[str, Any], login_id: str, name: str | None) -> dict[str, Any]:
        defaults = self._profile_defaults(login_id, name)
        changed = False
        for key, value in defaults.items():
            if key not in profile:
                profile[key] = value
                changed = True
        if name and profile.get("displayName") != name:
            profile["displayName"] = name
            changed = True
        if self._reconcile_exclusive_profile_state(profile):
            changed = True
        if changed:
            profile["updatedAt"] = time.time()
            self.profiles.upsert(f"profile::{login_id}", profile)
        return profile

    def _reconcile_exclusive_profile_state(self, profile: dict[str, Any]) -> bool:
        """Remove legacy likes/My List entries that violate an exclusive allow-list."""
        if (
            exclusive_content_policy(profile) is None
            or int(profile.get("exclusivePolicyEnforcementVersion") or 0) >= 1
        ):
            return False

        changed = False
        unresolved = False
        for field in ("likedTitleIds", "watchlistTitleIds"):
            current = self._dedupe(profile.get(field, []))
            kept: list[str] = []
            for title_id in current:
                try:
                    title = self.title(title_id)
                except Exception:
                    kept.append(title_id)
                    unresolved = True
                    continue
                if exclusive_content_conflict(title, profile) is None:
                    kept.append(title_id)
            if kept != current:
                profile[field] = kept
                changed = True

        # Do not mark the migration complete if a transient lookup failure left an
        # entry unverified; the next profile read will safely try again.
        if not unresolved:
            profile["exclusivePolicyEnforcementVersion"] = 1
            profile["exclusivePolicyReconciledAt"] = time.time()
            changed = True
        return changed

    @staticmethod
    def _dedupe(values: Iterable[Any]) -> list[str]:
        result: list[str] = []
        for value in values:
            text = str(value).strip()
            if text and text not in result:
                result.append(text)
        return result

    def viewer_signals(self, login_id: str) -> dict[str, Any]:
        profile = self.get_profile(login_id)
        liked = set(str(x) for x in profile.get("likedTitleIds", []))
        disliked = set(str(x) for x in profile.get("dislikedTitleIds", []))
        watchlist = set(str(x) for x in profile.get("watchlistTitleIds", []))
        watched: set[str] = set()
        completed: set[str] = set()
        try:
            # One bounded Query request hydrates legacy interaction signals and
            # watch history together. Profile arrays remain the canonical fast
            # path for current likes/dislikes/My List state.
            rows = self.cluster.query(
                f"SELECT 'interaction' AS signalType, i.titleId, i.action, "
                f"i.timestamp, NULL AS progressPct FROM {self.interactions_keyspace} AS i "
                "WHERE i.viewerId=$viewerId "
                "AND i.action IN ['like','dislike','watchlist','remove_watchlist'] "
                "UNION ALL "
                f"SELECT 'history' AS signalType, h.titleId, NULL AS action, "
                f"h.lastWatchedAt AS timestamp, h.progressPct FROM {self.history_keyspace} AS h "
                "WHERE h.viewerId=$viewerId AND h.progressPct > 0",
                QueryOptions(
                    named_parameters={"viewerId": login_id},
                    readonly=True,
                    scan_consistency=QueryScanConsistency.REQUEST_PLUS,
                ),
            ).rows()
            latest: dict[str, tuple[float, str]] = {}
            for row in rows:
                title_id = str(row.get("titleId") or "")
                if not title_id:
                    continue
                if row.get("signalType") == "history":
                    watched.add(title_id)
                    if float(row.get("progressPct") or 0) >= 85:
                        completed.add(title_id)
                    continue
                timestamp = float(row.get("timestamp") or 0)
                previous = latest.get(title_id)
                if previous is None or timestamp > previous[0]:
                    latest[title_id] = (
                        timestamp,
                        str(row.get("action") or ""),
                    )
            for title_id, (_, action) in latest.items():
                if action == "like":
                    liked.add(title_id)
                    disliked.discard(title_id)
                elif action == "dislike":
                    disliked.add(title_id)
                    liked.discard(title_id)
                elif action == "watchlist":
                    watchlist.add(title_id)
                elif action == "remove_watchlist":
                    watchlist.discard(title_id)
        except Exception:
            pass

        # Profile arrays are canonical in exclusive mode. This prevents legacy
        # interaction rows from resurrecting an out-of-policy like or My List item
        # after the one-time profile reconciliation above.
        if exclusive_content_policy(profile) is not None:
            liked.intersection_update(
                str(value) for value in profile.get("likedTitleIds", [])
            )
            watchlist.intersection_update(
                str(value) for value in profile.get("watchlistTitleIds", [])
            )

        return {
            "profile": profile,
            "liked": liked,
            "disliked": disliked,
            "watchlist": watchlist,
            "watched": watched,
            "completed": completed,
            "topPickExclusions": watched | liked | disliked | watchlist,
            "heroExclusions": watched | liked | disliked | watchlist,
        }

    # ------------------------------------------------------------------
    # Cached progressive home experience
    # ------------------------------------------------------------------
    def _cache_get(self, key: str, max_age: int) -> Any | None:
        try:
            doc = self.generated.get(key).content_as[dict]
            if time.time() - float(doc.get("updatedAt") or 0) <= max_age:
                return doc.get("payload")
        except Exception:
            pass
        return None

    def _cache_put(self, key: str, payload: Any) -> None:
        try:
            self.generated.upsert(
                key,
                {"type": "streamai_cache", "updatedAt": time.time(), "payload": payload},
            )
        except Exception:
            pass

    def invalidate_home_cache(self, login_id: str) -> None:
        keys = [
            f"home-shell::{login_id}",
            f"home-row::{login_id}::top-picks",
            f"home-row::{login_id}::because",
            f"home-row::{login_id}::continue",
            f"home-row::{login_id}::recently-watched",
            f"home-row::{login_id}::my-list",
            f"home-row::{login_id}::trending",
        ]
        for key in keys:
            try:
                self.generated.remove(key)
            except Exception:
                pass

    def home_shell(self, login_id: str) -> dict[str, Any]:
        cached = self._cache_get(f"home-shell::{login_id}", self.settings.home_cache_seconds)
        if cached and any(
            str(item.get("id")) == "my-list" for item in cached.get("rowDescriptors", [])
        ):
            cached["cache"] = "hit"
            return cached
        profile = self.get_profile(login_id)
        signals = self.viewer_signals(login_id)
        hero: dict[str, Any] | None = None
        cached_top = self._cache_get(
            f"home-row::{login_id}::top-picks", self.settings.row_cache_seconds
        )
        if cached_top:
            hero = next(
                (
                    item
                    for item in cached_top.get("items", [])
                    if item.get("id") not in signals["heroExclusions"]
                ),
                None,
            )
        if hero is None:
            trending = self._apply_entitlement_policy(
                self._trending_items(limit=24), login_id, remove_denied=True
            )
            hero = next(
                (item for item in trending if item.get("id") not in signals["heroExclusions"]),
                None,
            )
        if hero:
            hero = dict(hero)
            hero["recommendationSource"] = hero.get("recommendationSource") or "unseen_popularity"
            hero["heroReason"] = "Unseen title selected from viewer signals and catalogue ranking"
        payload = {
            "hero": hero,
            "profile": profile,
            "rowDescriptors": [
                {"id": "continue", "title": "Continue Watching", "subtitle": "Resume across devices and sessions"},
                {"id": "recently-watched", "title": "Recently Watched", "subtitle": "Authoritative structured viewing history"},
                {"id": "my-list", "title": "My List", "subtitle": "Titles saved through governed viewer actions"},
                {"id": "top-picks", "title": f"Top Picks for {profile.get('displayName') or login_id}", "subtitle": "Personalised unseen titles; liked, disliked and watched titles are excluded"},
                {"id": "because", "title": "Because You Watched", "subtitle": "Similar unseen titles from vector retrieval"},
                {"id": "trending", "title": "Trending Now", "subtitle": "Popularity-ranked catalogue; disliked titles are suppressed"},
            ],
            "cache": "miss",
        }
        self._cache_put(f"home-shell::{login_id}", payload)
        return payload

    def home_row(self, login_id: str, row_id: str) -> dict[str, Any]:
        if row_id not in {"continue", "recently-watched", "my-list", "top-picks", "because", "trending"}:
            raise ValueError(f"Unknown home row: {row_id}")
        cache_seconds = 30 if row_id in {"continue", "recently-watched", "my-list", "because"} else self.settings.row_cache_seconds
        if row_id == "trending":
            cache_seconds = 30
        key = f"home-row::{login_id}::{row_id}"
        cached = self._cache_get(key, cache_seconds)
        if cached:
            cached["cache"] = "hit"
            return cached

        profile = self.get_profile(login_id)
        if row_id == "continue":
            row = {
                "id": row_id,
                "title": "Continue Watching",
                "subtitle": "Resume across devices and sessions",
                "items": self._continue_watching(login_id),
            }
        elif row_id == "recently-watched":
            row = {
                "id": row_id,
                "title": "Recently Watched",
                "subtitle": "Authoritative structured viewing history",
                "items": self.list_watch_history(login_id=login_id, limit=16),
            }
        elif row_id == "my-list":
            signals = self.viewer_signals(login_id)
            items = self._fetch_title_ids(sorted(signals["watchlist"]))
            self._mark_viewer_state(items, signals)
            row = {
                "id": row_id,
                "title": "My List",
                "subtitle": "Titles saved through governed viewer actions",
                "items": items,
            }
        elif row_id == "top-picks":
            row = {
                "id": row_id,
                "title": f"Top Picks for {profile.get('displayName') or login_id}",
                "subtitle": "Personalised unseen titles; liked, disliked and watched titles are excluded",
                "items": self._top_picks(login_id),
            }
        elif row_id == "because":
            because = self._because_you_watched(login_id)
            row = {
                "id": row_id,
                "title": because.get("title", "Because You Watched"),
                "subtitle": "Vector similarity from the most recent watched title",
                "items": because.get("items", []),
            }
        else:
            signals = self.viewer_signals(login_id)
            entitled_trending = self._apply_entitlement_policy(
                self._trending_items(limit=24), login_id, remove_denied=True
            )
            items = [
                item
                for item in entitled_trending
                if item.get("id") not in signals["disliked"]
            ][:20]
            self._mark_viewer_state(items, signals)
            row = {
                "id": row_id,
                "title": "Trending Now",
                "subtitle": "Operational catalogue ranked by TMDB popularity",
                "items": items,
            }
        row["cache"] = "miss"
        self._cache_put(key, row)
        return row

    def home(self, login_id: str) -> dict[str, Any]:
        shell = self.home_shell(login_id)
        rows = [self.home_row(login_id, row["id"]) for row in shell["rowDescriptors"]]
        return {"hero": shell.get("hero"), "profile": shell.get("profile"), "rows": rows}

    def _trending_items(self, limit: int = 24) -> list[dict[str, Any]]:
        key = "home-row::global::trending"
        cached = self._cache_get(key, self.settings.trending_cache_seconds)
        if cached:
            return list(cached.get("items") or [])[:limit]
        items = self._query_titles(
            f"SELECT t.* FROM {self.titles_keyspace} AS t "
            "ORDER BY t.popularity DESC, t.voteAverage DESC LIMIT 40"
        )
        self._add_rank_scores(items, source="popularity")
        self._cache_put(key, {"items": items})
        return items[:limit]

    def _top_picks(self, login_id: str) -> list[dict[str, Any]]:
        # Home and chat now share the same evidence-gated recommender so the UI
        # cannot show a title that the assistant would reject as unrelated.
        return self.recommend_for_profile(
            login_id=login_id,
            mode="fts",
            limit=16,
            content_type=None,
        )["results"]

    def recommendation_query_for_profile(
        self,
        login_id: str,
        *,
        profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        profile = profile or self.get_profile(login_id)
        languages = [
            str(value) for value in profile.get("preferredLanguages", [])
            if str(value).strip().lower() not in {"", "english"}
        ]
        affinity_genres = [
            str(name)
            for name, score in sorted(
                dict(profile.get("genreAffinities") or {}).items(),
                key=lambda item: float(item[1] or 0),
                reverse=True,
            )
            if float(score or 0) > 0
        ][:6]
        affinity_themes = [
            str(name)
            for name, score in sorted(
                dict(profile.get("themeAffinities") or {}).items(),
                key=lambda item: float(item[1] or 0),
                reverse=True,
            )
            if float(score or 0) > 0
        ][:8]
        positive_terms = self._dedupe(
            [
                *profile.get("preferredGenres", []),
                *profile.get("preferredThemes", []),
                *profile.get("preferredPeople", []),
                *affinity_genres,
                *affinity_themes,
                *languages,
            ]
        )
        query = " ".join(positive_terms).strip() or "popular highly rated"
        return {
            "query": query,
            "profile": profile,
            "positiveTerms": positive_terms,
            "affinityGenres": affinity_genres,
            "affinityThemes": affinity_themes,
        }

    def _profile_match_evidence(
        self, doc: dict[str, Any], profile: dict[str, Any]
    ) -> dict[str, Any]:
        """Return auditable positive evidence for one recommendation candidate."""
        preferred_genres = self._normalised_values(profile.get("preferredGenres", []))
        preferred_themes = self._normalised_values(profile.get("preferredThemes", []))
        preferred_people = self._normalised_values(profile.get("preferredPeople", []))
        preferred_languages = self._normalised_values(
            value
            for value in profile.get("preferredLanguages", [])
            if str(value).strip().lower() not in {"", "english"}
        )
        affinity_genres = {
            re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()
            for name, score in dict(profile.get("genreAffinities") or {}).items()
            if float(score or 0) > 0
        }
        affinity_themes = {
            re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()
            for name, score in dict(profile.get("themeAffinities") or {}).items()
            if float(score or 0) > 0
        }

        genre_values = {self._normalise_value(value): str(value) for value in doc.get("genres", []) if value}
        keyword_values = {self._normalise_value(value): str(value) for value in doc.get("keywords", []) if value}
        people_values = {
            self._normalise_value(value): str(value)
            for value in [*doc.get("castNames", []), *doc.get("directorNames", [])]
            if value
        }
        language_values = self._normalised_values(
            [doc.get("originalLanguage"), *doc.get("spokenLanguages", [])]
        )
        overview = self._normalise_value(doc.get("overview") or "")

        matched_genres = sorted((preferred_genres | affinity_genres).intersection(genre_values))
        matched_themes = sorted((preferred_themes | affinity_themes).intersection(keyword_values))
        # A stored theme may appear in the synopsis even when TMDB did not emit
        # it as a keyword. This remains catalogue evidence, not model inference.
        for theme in sorted(preferred_themes | affinity_themes):
            if theme and theme not in matched_themes and theme in overview:
                matched_themes.append(theme)
        matched_people = sorted(preferred_people.intersection(people_values))
        matched_languages = sorted(preferred_languages.intersection(language_values))

        matched_fields: list[dict[str, Any]] = []
        if matched_genres:
            matched_fields.extend(
                {"field": "genres", "value": genre_values[value], "matchType": "profile_exact"}
                for value in matched_genres
            )
        if matched_themes:
            matched_fields.extend(
                {
                    "field": "keywords_or_overview",
                    "value": keyword_values.get(value, value.title()),
                    "matchType": "profile_exact",
                }
                for value in matched_themes
            )
        if matched_people:
            matched_fields.extend(
                {"field": "cast_or_creator", "value": people_values[value], "matchType": "profile_exact"}
                for value in matched_people
            )
        if matched_languages:
            matched_fields.extend(
                {"field": "language", "value": value, "matchType": "profile_exact"}
                for value in matched_languages
            )

        score = (
            len(matched_genres) * 3
            + len(matched_themes) * 2
            + len(matched_people) * 4
            + len(matched_languages)
        )
        reasons: list[str] = []
        if matched_genres:
            reasons.append("preferred genre" + ("s" if len(matched_genres) > 1 else "") + ": " + ", ".join(genre_values[value] for value in matched_genres))
        if matched_people:
            reasons.append("preferred person" + ("s" if len(matched_people) > 1 else "") + ": " + ", ".join(people_values[value] for value in matched_people))
        if matched_themes:
            reasons.append("preferred theme" + ("s" if len(matched_themes) > 1 else "") + ": " + ", ".join(keyword_values.get(value, value.title()) for value in matched_themes))
        if matched_languages:
            reasons.append("preferred language: " + ", ".join(matched_languages))
        return {
            "score": score,
            "summary": "Matched " + "; ".join(reasons) if reasons else "No direct profile evidence",
            "matchedFields": matched_fields,
        }

    @staticmethod
    def _normalise_value(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()

    def _profile_matched_candidates(
        self, docs: list[dict[str, Any]], profile: dict[str, Any]
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for item in docs:
            evidence = self._profile_match_evidence(item, profile)
            if int(evidence.get("score") or 0) <= 0:
                continue
            candidate = dict(item)
            candidate["profileMatchScore"] = int(evidence["score"])
            candidate["matchExplanation"] = {
                "summary": evidence["summary"],
                "matchedFields": evidence["matchedFields"],
            }
            candidate["recommendationSource"] = "profile_evidence"
            candidates.append(candidate)
        candidates.sort(
            key=lambda item: (
                int(item.get("profileMatchScore") or 0),
                float(item.get("searchScore") or 0),
                float(item.get("popularity") or 0),
                float(item.get("voteAverage") or 0),
            ),
            reverse=True,
        )
        return candidates

    def recommend_for_profile(
        self,
        *,
        login_id: str,
        mode: str | None = None,
        limit: int = 12,
        content_type: str | None = None,
        plan_filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return unseen titles with title-level evidence from Couchbase.

        Popularity is a ranking/tie-break signal, never proof that a title fits
        the viewer. When a mature profile has only two valid matches, the chat
        returns two rather than padding the answer with unrelated popular films.
        """
        started = time.perf_counter()
        viewer_context_started = time.perf_counter()
        viewer_context = self.viewer_signals(login_id)
        viewer_context_elapsed_ms = round(
            (time.perf_counter() - viewer_context_started) * 1000,
            1,
        )
        plan = self.recommendation_query_for_profile(
            login_id,
            profile=dict(viewer_context.get("profile") or {}),
        )
        requested_mode = mode or self.settings.chat_recommendation_mode
        profile = plan["profile"]
        positive_terms = list(plan["positiveTerms"])
        effective_filters = self._resolved_plan_filters(plan_filters or {})
        if content_type:
            effective_filters["contentType"] = content_type
        results: list[dict[str, Any]] = []
        trace: dict[str, Any] = {}
        raw_candidate_count = 0

        if positive_terms:
            # Profile signals remain auditable structured evidence. When the
            # caller selects hybrid/vector, the same signals also form a query
            # embedding so semantic candidate generation has a meaningful role;
            # final admission still requires exact profile evidence.
            result = self.search(
                query=plan["query"],
                mode=(
                    requested_mode
                    if requested_mode in {"fts", "vector", "hybrid"}
                    else "hybrid"
                ),
                login_id=login_id,
                content_type=content_type,
                limit=max(limit * 3, 24),
                purpose="recommendation",
                explicit_terms=positive_terms,
                plan_filters=effective_filters,
                viewer_signals_context=viewer_context,
            )
            raw = list(result.get("results") or [])
            raw_candidate_count += len(raw)
            results = self._profile_matched_candidates(raw, profile)
            trace = dict(result.get("trace") or {})

            if len(results) < limit:
                existing = {str(item.get("id")) for item in results}
                fallback_intent = self._analyse_query(plan["query"])
                fallback_intent["profileQuery"] = True
                fallback_intent["explicitTerms"] = positive_terms
                fallback_intent["terms"] = positive_terms
                fallback_intent["genres"] = []
                fallback_intent["genre"] = None
                fallback_intent["country"] = None
                fallback_intent["planFilters"] = effective_filters
                try:
                    exact_profile_pool = self._fallback_query(
                        query=plan["query"],
                        content_type=content_type,
                        limit=max(limit * 8, 80),
                        intent=fallback_intent,
                    )
                except Exception as exc:
                    # Search results remain usable if the wider Query fallback
                    # is unavailable; do not turn a partial recommendation
                    # into an assistant error.
                    exact_profile_pool = []
                    trace["profileSqlppFallbackError"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                exact_profile_pool = self._exclude_with_signals(
                    exact_profile_pool,
                    login_id,
                    "recommendation",
                    viewer_signals_context=viewer_context,
                )
                trace["profileSqlppFallbackCount"] = len(exact_profile_pool)
                raw_candidate_count += len(exact_profile_pool)
                for candidate in self._profile_matched_candidates(
                    exact_profile_pool, profile
                ):
                    item_id = str(candidate.get("id") or "")
                    if not item_id or item_id in existing:
                        continue
                    results.append(candidate)
                    existing.add(item_id)
                    if len(results) >= limit:
                        break

            if len(results) < limit:
                existing = {str(item.get("id")) for item in results}
                fallback = self._trending_items(max(limit * 4, 40))
                if content_type:
                    fallback = [item for item in fallback if item.get("contentType") == content_type]
                fallback = [
                    item
                    for item in fallback
                    if self._matches_plan_filters(item, effective_filters)
                ]
                fallback = self._exclude_with_signals(
                    fallback,
                    login_id,
                    "recommendation",
                    viewer_signals_context=viewer_context,
                )
                raw_candidate_count += len(fallback)
                for candidate in self._profile_matched_candidates(fallback, profile):
                    item_id = str(candidate.get("id") or "")
                    if not item_id or item_id in existing:
                        continue
                    results.append(candidate)
                    existing.add(item_id)
                    if len(results) >= limit:
                        break
        else:
            fallback = self._trending_items(max(limit * 4, 40))
            if content_type:
                fallback = [item for item in fallback if item.get("contentType") == content_type]
            fallback = [
                item
                for item in fallback
                if self._matches_plan_filters(item, effective_filters)
            ]
            results = self._exclude_with_signals(
                fallback,
                login_id,
                "recommendation",
                viewer_signals_context=viewer_context,
            )[:limit]
            raw_candidate_count = len(fallback)
            for item in results:
                item["recommendationSource"] = "catalogue_cold_start"
                item["matchExplanation"] = {
                    "summary": "Popular unseen title in the current catalogue",
                    "matchedFields": [
                        {"field": "popularity", "value": item.get("popularity"), "matchType": "ranking"},
                        {"field": "voteAverage", "value": item.get("voteAverage"), "matchType": "ranking"},
                    ],
                }

        results = results[:limit]
        trace.update(
            {
                "mode": trace.get("mode") or "catalogue_profile_evidence",
                "requestedMode": requested_mode,
                "candidateMode": (
                    f"{requested_mode}_profile_signals"
                    if positive_terms
                    else "catalogue_cold_start"
                ),
                "query": plan["query"],
                "profileDerivedQuery": True,
                "viewerContextElapsedMs": viewer_context_elapsed_ms,
                "coldStartFallback": not positive_terms,
                "positiveTerms": positive_terms,
                "negativeGenres": list(profile.get("dislikedGenres", [])),
                "negativeThemes": list(profile.get("dislikedThemes", [])),
                "structuredContentType": content_type,
                "structuredPlanFilters": effective_filters,
                "rawCandidateCount": raw_candidate_count,
                "evidenceMatchedCount": len(results),
                "profileEvidenceRequired": bool(positive_terms),
                "profileMatchLimited": bool(positive_terms and len(results) < limit),
                "returnedCount": len(results),
                "selectedTitleIds": [item.get("id") for item in results],
                "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
                "services": trace.get("services") or ["Search/FTS", "Data/KV"],
            }
        )
        self._write_trace(login_id, "profile_recommendation", trace)
        return {"results": results, "trace": trace, "profile": profile}

    # ------------------------------------------------------------------
    # Search and retrieval transparency
    # ------------------------------------------------------------------
    @classmethod
    def normalise_natural_query(cls, query: str) -> str:
        value = re.sub(r"\s+", " ", query.lower().strip())
        for pattern, replacement in cls.QUERY_NORMALISATIONS:
            value = re.sub(pattern, replacement, value)
        return re.sub(r"\s+", " ", value).strip()

    def analyse_request(self, query: str) -> dict[str, Any]:
        """Public deterministic intent analysis used by the chat router.

        The result is descriptive only. Catalogue existence and recommendation
        candidates are still decided exclusively by Couchbase retrieval.
        """
        return self._analyse_query(query)

    def _analyse_query(self, query: str) -> dict[str, Any]:
        lower = self.normalise_natural_query(query)
        country: dict[str, Any] | None = None
        country_token: str | None = None
        for token, spec in self.COUNTRY_INTENTS.items():
            if re.search(rf"\b{re.escape(token)}\b", lower):
                country = spec
                country_token = token
                break

        genres: list[str] = []
        # Longest aliases first prevents "fiction"-style partial handling and
        # preserves multi-word genres such as Science Fiction.
        for token, canonical in sorted(self.KNOWN_GENRES.items(), key=lambda item: len(item[0]), reverse=True):
            if re.search(rf"\b{re.escape(token)}s?\b", lower) and canonical not in genres:
                genres.append(canonical)

        request_language = bool(
            re.search(
                r"\b(?:recommend(?:ation)?s?|suggest|find|show|give|list|pick|choose|looking for|"
                r"want to watch|what should i watch|what can i watch|what are some|what have you got|"
                r"watch next|next watch|watch tonight|anything good|something good|help me choose|browse)\b",
                lower,
            )
        )
        movie_noun = bool(re.search(r"\b(?:movies|films)\b", lower)) or bool(
            request_language and re.search(r"\b(?:movie|film)\b", lower)
        )
        tv_noun = bool(re.search(r"\b(?:tv|television|series|shows)\b", lower))
        detected_content_type = "movie" if movie_noun and not tv_noun else "tv" if tv_noun and not movie_noun else None

        expanded: list[str] = []
        for token, values in self.QUERY_EXPANSIONS.items():
            if re.search(rf"\b{re.escape(token)}s?\b", lower):
                expanded.extend(values)

        cleaned = lower
        if country_token:
            cleaned = re.sub(rf"\b{re.escape(country_token)}\b", " ", cleaned)
        for token in sorted(self.KNOWN_GENRES, key=len, reverse=True):
            cleaned = re.sub(rf"\b{re.escape(token)}s?\b", " ", cleaned)
        cleaned = re.sub(
            r"\b(?:please|can|could|would|you|i|recommend(?:ation)?s?|suggest|find|show|give|list|pick|choose|"
            r"help|browse|me|some|any|a|an|the|what|which|who|where|when|how|do|does|did|is|are|was|were|"
            r"have|has|got|there|currently|good|great|best|top|popular|trending|highly|rated|new|latest|"
            r"next|watch|tonight|now|for|to|available|catalogue|catalog|movies?|films?|television|tv|"
            r"series|shows?|titles?|content|options?|choices?|selections?|that|with|something|anything)\b",
            " ",
            cleaned,
        )
        cleaned = re.sub(r"[^a-z0-9'&-]+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        next_watch = bool(
            re.search(
                r"\b(?:what should i watch|what can i watch|watch next|next watch|my next watch|"
                r"pick (?:a|my) next|something for tonight|watch tonight)\b",
                lower,
            )
        )
        generic_recommendation = bool(
            not cleaned
            and not genres
            and not country
            and (request_language or next_watch or detected_content_type)
        )

        # Broad natural-language recommendation prompts must not be searched as
        # literal titles. Only the meaningful residual terms are used.
        terms = self._dedupe([cleaned, *expanded])
        if not request_language and not genres and not country and not detected_content_type:
            terms = self._dedupe([query.strip(), cleaned, *expanded])

        return {
            "raw": query,
            "lower": lower,
            "normalisedQuery": lower,
            "country": country,
            "countryToken": country_token,
            "genre": genres[0] if genres else None,
            "genres": genres,
            "contentType": detected_content_type,
            "requestLanguage": request_language,
            "genericRecommendation": generic_recommendation,
            "nextWatch": next_watch,
            "personalisedRecommendation": bool(next_watch or generic_recommendation),
            "strictStructuredRequest": bool(genres or country or detected_content_type),
            "terms": [term for term in terms if term],
            "cleaned": cleaned,
            "expandedTerms": self._dedupe(expanded),
        }

    def _lexical_query(self, intent: dict[str, Any]) -> dict[str, Any]:
        disjuncts: list[dict[str, Any]] = []
        raw = str(intent["raw"])
        # Title fuzziness is only for direct title lookup. Recommendation
        # sentences and structured genre/country requests never get fuzzy title
        # clauses, which prevents weak unrelated matches.
        direct_title_lookup = not (
            intent.get("profileQuery")
            or intent.get("requestLanguage")
            or intent.get("strictStructuredRequest")
        )
        if direct_title_lookup:
            disjuncts.extend(
                [
                    {"match_phrase": raw, "field": "title", "boost": 12.0},
                    {"match": raw, "field": "title", "boost": 7.0, "fuzziness": 1},
                    {"match_phrase": raw, "field": "originalTitle", "boost": 8.0},
                    {"match": raw, "field": "originalTitle", "boost": 4.0, "fuzziness": 1},
                ]
            )
        for term in intent.get("terms", []):
            # The catalogue arrays are indexed with the keyword analyser. A
            # MatchQuery may analyse/lowercase the request and miss stored
            # values such as "Science Fiction". TermQuery preserves exact
            # values. Natural requests are normalised to lowercase, so people
            # fields also receive a title-cased candidate while TMDB keywords
            # retain the lowercase candidate.
            normalised_term = str(term).strip()
            canonical_genre = self.KNOWN_GENRES.get(self.normalise_natural_query(normalised_term))
            genre_term = canonical_genre or normalised_term.title()
            person_term = normalised_term if intent.get("profileQuery") else normalised_term.title()
            disjuncts.extend(
                [
                    {"match": normalised_term, "field": "overview", "boost": 1.0},
                    {"match": normalised_term, "field": "searchText", "boost": 1.5},
                    {"term": normalised_term, "field": "keywords", "boost": 5.0},
                    {"term": genre_term, "field": "genres", "boost": 6.0},
                    {"term": person_term, "field": "castNames", "boost": 3.0},
                    {"term": person_term, "field": "directorNames", "boost": 5.0},
                ]
            )
        return {"disjuncts": disjuncts, "min": 1}

    def _country_filter(self, country: dict[str, Any]) -> dict[str, Any]:
        values: list[dict[str, Any]] = []
        for code in country.get("countries", []):
            values.append({"term": code, "field": "originCountries"})
        for code in country.get("languages", []):
            values.append({"term": code, "field": "originalLanguage"})
        return {"disjuncts": values, "min": 1}

    def search(
        self,
        *,
        query: str,
        mode: str,
        login_id: str | None,
        content_type: str | None = None,
        limit: int | None = None,
        purpose: str = "search",
        explicit_terms: list[str] | None = None,
        plan_filters: dict[str, Any] | None = None,
        viewer_signals_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        requested_mode = mode if mode in {"fts", "vector", "hybrid"} else "hybrid"
        effective_mode = requested_mode
        size = min(limit or self.settings.search_result_limit, 50)
        started = time.perf_counter()
        intent = self._analyse_query(query)
        effective_plan_filters = self._resolved_plan_filters(plan_filters or {})
        intent["planFilters"] = effective_plan_filters
        effective_content_type = content_type or intent.get("contentType")
        if explicit_terms:
            cleaned_terms = self._dedupe(explicit_terms)
            intent["profileQuery"] = True
            intent["explicitTerms"] = cleaned_terms
            intent["terms"] = cleaned_terms
            # Profile signals are alternatives, not a structured AND filter.
            # A profile containing Science Fiction, Thriller and Crime means
            # "match any strong preference", not "every title must have all
            # three genres". Country/language words are handled the same way.
            intent["genres"] = []
            intent["genre"] = None
            intent["country"] = None
            intent["countryToken"] = None
            intent["strictStructuredRequest"] = bool(effective_content_type)

        viewer_profile: dict[str, Any] = {}
        if login_id:
            if viewer_signals_context is not None:
                viewer_profile = dict(
                    viewer_signals_context.get("profile") or {}
                )
            else:
                try:
                    viewer_profile = self.get_profile(login_id)
                except AttributeError:
                    # Lightweight unit-test/service doubles may intentionally omit
                    # the Couchbase profile collection. Production instances always
                    # initialise it, so never mask an AttributeError there.
                    if hasattr(self, "profiles"):
                        raise
        exclusive_policy = exclusive_content_policy(viewer_profile)
        exclusive_request_conflict = request_conflicts_with_exclusive_policy(
            intent.get("genres", []), exclusive_policy
        )
        structured_only = bool(
            (
                intent.get("genres")
                or intent.get("country")
                or effective_plan_filters
            )
            and not intent.get("terms")
            and not intent.get("expandedTerms")
        )

        vector: list[float] | None = None
        embedding_meta: dict[str, Any] = {
            "embeddingModel": None,
            "embeddingModelCalls": 0,
            "embeddingCacheHits": 0,
            "embeddingTimeouts": 0,
            "embeddingElapsedMs": 0.0,
            "embeddingError": None,
        }
        if requested_mode in {"vector", "hybrid"} and not structured_only:
            embed_started = time.perf_counter()
            try:
                if getattr(self, "_showcase_faults", {}).get("embedding_timeout"):
                    raise httpx.ReadTimeout("Presenter-injected embedding timeout")
                vector, embedding_meta = self.embed_with_trace(query)
            except Exception as exc:
                effective_mode = "fts"
                embedding_meta["embeddingModel"] = self.settings.embedding_model
                embedding_meta["embeddingModelCalls"] = 1
                embedding_meta["embeddingError"] = f"{type(exc).__name__}: {exc}"
                embedding_meta["embeddingTimeouts"] = 1 if isinstance(exc, httpx.TimeoutException) else 0
                embedding_meta["embeddingElapsedMs"] = round((time.perf_counter() - embed_started) * 1000, 1)
        elif requested_mode in {"vector", "hybrid"}:
            # Exact genre/country browsing and known profile signals are faster
            # and more reliable as deterministic Search/SQL++ retrieval. Vector
            # search remains available for free-form themes and "more like".
            effective_mode = "fts"
            embedding_meta["embeddingSkippedReason"] = "structured_filter_only"

        lexical = self._lexical_query(intent)
        lexical_clauses: list[dict[str, Any]] = []
        structured_filters: list[dict[str, Any]] = []
        country_only = bool(
            intent.get("country")
            and not intent.get("cleaned")
            and not intent.get("expandedTerms")
            and not intent.get("genre")
        )
        if effective_mode in {"fts", "hybrid"} and not country_only and lexical.get("disjuncts"):
            lexical_clauses.append(lexical)
        if intent.get("country"):
            structured_filters.append(self._country_filter(intent["country"]))
        for genre in intent.get("genres", []):
            structured_filters.append({"term": genre, "field": "genres"})
        if effective_content_type:
            structured_filters.append({"term": effective_content_type, "field": "contentType"})
        for genre in effective_plan_filters.get("genres") or []:
            if genre not in intent.get("genres", []):
                structured_filters.append({"term": genre, "field": "genres"})
        release_year = effective_plan_filters.get("releaseYear")
        release_year_min = effective_plan_filters.get("releaseYearMin")
        release_year_max = effective_plan_filters.get("releaseYearMax")
        if release_year is not None:
            structured_filters.append(
                {
                    "min": int(release_year),
                    "max": int(release_year),
                    "inclusive_min": True,
                    "inclusive_max": True,
                    "field": "releaseYear",
                }
            )
        elif release_year_min is not None or release_year_max is not None:
            year_filter: dict[str, Any] = {
                "inclusive_min": True,
                "inclusive_max": True,
                "field": "releaseYear",
            }
            if release_year_min is not None:
                year_filter["min"] = int(release_year_min)
            if release_year_max is not None:
                year_filter["max"] = int(release_year_max)
            structured_filters.append(year_filter)
        if effective_plan_filters.get("ratingMin") is not None:
            structured_filters.append(
                {
                    "min": float(effective_plan_filters["ratingMin"]),
                    "inclusive_min": True,
                    "field": "voteAverage",
                }
            )

        query_obj: dict[str, Any] | None = None
        if effective_mode in {"fts", "hybrid"}:
            query_clauses = [*lexical_clauses, *structured_filters]
            if query_clauses:
                query_obj = query_clauses[0] if len(query_clauses) == 1 else {"conjuncts": query_clauses}

        knn: list[dict[str, Any]] | None = None
        if vector is not None and effective_mode in {"vector", "hybrid"}:
            knn_item: dict[str, Any] = {
                "field": "embedding",
                "vector": vector,
                "k": max(size * 4, 40),
                "boost": 1.4,
            }
            if structured_filters:
                knn_item["filter"] = (
                    structured_filters[0]
                    if len(structured_filters) == 1
                    else {"conjuncts": structured_filters}
                )
            knn = [knn_item]

        payload: dict[str, Any] = {
            "size": max(size * 3, 30),
            "from": 0,
            "fields": [
                "title", "contentType", "releaseYear", "genres", "keywords",
                "originCountries", "originalLanguage",
            ],
            "explain": False,
        }
        if effective_mode in {"fts", "hybrid"}:
            payload["highlight"] = {
                "style": "html",
                "fields": ["title", "originalTitle", "overview", "keywords", "genres", "castNames", "directorNames"],
            }
        if query_obj is not None:
            payload["query"] = query_obj
        if knn:
            payload["knn"] = knn

        circuit_was_open = (
            time.monotonic()
            < float(getattr(self, "_search_circuit_open_until", 0.0))
        )
        try:
            if circuit_was_open:
                raise RuntimeError(
                    "Search circuit breaker open; using immediate SQL++ fallback"
                )
            if getattr(self, "_showcase_faults", {}).get("search_unavailable"):
                raise RuntimeError("Presenter-injected Search Service outage")
            response = self._search_client.post(
                f"{self.search_index_path}/query",
                json=payload,
                timeout=min(
                    float(self.settings.search_query_timeout_seconds),
                    float(
                        getattr(
                            self.settings,
                            "search_interactive_timeout_seconds",
                            1.5,
                        )
                    ),
                ),
            )
            response.raise_for_status()
            self._search_failure_count = 0
            self._search_circuit_open_until = 0.0
            body = response.json()
            hits = list(body.get("hits", []))
            docs = self._fetch_hits(hits, content_type=effective_content_type)
            docs = [doc for doc in docs if self._matches_structured_intent(doc, intent, effective_content_type)]
            exact_query_fallback = False
            structured_broadening = False
            if not docs and (
                intent.get("genres")
                or intent.get("country")
                or intent.get("profileQuery")
                or effective_plan_filters
                or (intent.get("requestLanguage") and intent.get("terms"))
            ):
                # Search indexes can still be warming or may have an older
                # mapping on an upgraded demo. First preserve the full hybrid
                # intent through SQL++, then safely broaden to authoritative
                # genre/country filters when optional descriptive words are too
                # narrow (for example “atmospheric Science Fiction”).
                docs = self._fallback_query(
                    query=query,
                    content_type=effective_content_type,
                    limit=max(size * 2, 30),
                    intent=intent,
                )
                docs = [
                    doc for doc in docs
                    if self._matches_structured_intent(doc, intent, effective_content_type)
                ]
                if not docs and (intent.get("genres") or intent.get("country")) and (
                    intent.get("terms") or intent.get("expandedTerms") or intent.get("explicitTerms")
                ):
                    docs = self._fallback_query(
                        query=query,
                        content_type=effective_content_type,
                        limit=max(size * 2, 30),
                        intent=self._structured_only_fallback_intent(intent),
                    )
                    docs = [
                        doc for doc in docs
                        if self._matches_structured_intent(doc, intent, effective_content_type)
                    ]
                    structured_broadening = bool(docs)
                self._add_rank_scores(
                    docs,
                    source="sqlpp_structured_broadening" if structured_broadening else "sqlpp_exact_fallback",
                )
                exact_query_fallback = True
            else:
                self._apply_search_scores(
                    docs,
                    hits,
                    source=effective_mode,
                    intent=intent,
                    requested_mode=requested_mode,
                    effective_mode=effective_mode,
                )
            ranking_strategy = "couchbase_search_score"
            if intent.get("genres") and not intent.get("terms"):
                docs.sort(
                    key=lambda item: (
                        float(item.get("popularity") or 0),
                        float(item.get("voteAverage") or 0),
                        float(item.get("searchScore") or 0),
                    ),
                    reverse=True,
                )
                ranking_strategy = "exact_genre_then_popularity_quality"
            before = len(docs)
            override_genres: list[str] = []
            if purpose in {"recommendation", "search"} and login_id and not intent.get("profileQuery"):
                disliked = self._normalised_values(viewer_profile.get("dislikedGenres", []))
                override_genres = [
                    str(value)
                    for value in intent.get("genres", [])
                    if re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip() in disliked
                ]
            docs = self._exclude_with_signals(
                docs,
                login_id,
                purpose,
                ignored_disliked_genres=override_genres,
                viewer_signals_context=viewer_signals_context,
            )
            excluded = before - len(docs)
            docs = docs[:size]
            final_mode = (
                "sqlpp_structured_broadening"
                if structured_broadening
                else "sqlpp_exact_fallback" if exact_query_fallback else effective_mode
            )
            inventory_count = 0
            if not docs and len(intent.get("genres") or []) == 1:
                try:
                    inventory_count = int(
                        self.count_titles(
                            content_type=effective_content_type,
                            genre=str(intent["genres"][0]),
                        ).get("count")
                        or 0
                    )
                except Exception:
                    inventory_count = 0
            self._finalise_display_scores(
                docs,
                requested_mode=requested_mode,
                effective_mode=final_mode,
                ranking_strategy=("sqlpp_popularity_quality" if exact_query_fallback else ranking_strategy),
                mode_reason=embedding_meta.get("embeddingSkippedReason"),
            )
            trace = {
                "mode": final_mode,
                "effectiveMode": final_mode,
                "requestedMode": requested_mode,
                "modeReason": embedding_meta.get("embeddingSkippedReason"),
                "rankingStrategy": "sqlpp_popularity_quality" if exact_query_fallback else ranking_strategy,
                "scoreMeaning": "Relative final rank score for this result set; not a probability.",
                "purpose": purpose,
                "query": query,
                "explicitTerms": intent.get("explicitTerms", []),
                "index": self.settings.catalogue_search_index,
                "candidateCount": len(hits),
                "returnedCount": len(docs),
                "catalogueInventoryCount": inventory_count,
                "structuredBroadeningApplied": structured_broadening,
                "excludedByViewerSignals": excluded,
                "selectedTitleIds": [doc.get("id") for doc in docs],
                "expandedTerms": intent.get("expandedTerms", []),
                "structuredIntent": intent.get("country", {}).get("label") if intent.get("country") else None,
                "structuredGenres": list(intent.get("genres", [])),
                "structuredContentType": effective_content_type,
                "structuredPlanFilters": effective_plan_filters,
                "temporaryPreferenceOverrides": [f"disliked genre: {value}" for value in override_genres],
                "exclusivePreferencePolicy": exclusive_policy,
                "exclusivePreferenceConflict": exclusive_request_conflict,
                "fallbackToFts": requested_mode != effective_mode,
                "searchCircuitOpen": False,
                **embedding_meta,
                "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
                "services": (
                    ["Query", "Index", "Data/KV"]
                    if exact_query_fallback
                    else (["Search/FTS"] if effective_mode in {"fts", "hybrid"} else [])
                    + (["Search/Vector"] if vector is not None else [])
                    + ["Data/KV"]
                ),
            }
            self._write_trace(login_id, "catalogue_search", trace)
            return {"results": docs, "trace": trace}
        except Exception as exc:
            if not circuit_was_open:
                self._search_failure_count = int(
                    getattr(self, "_search_failure_count", 0)
                ) + 1
                self._search_circuit_open_until = time.monotonic() + float(
                    getattr(
                        self.settings,
                        "search_circuit_breaker_seconds",
                        20.0,
                    )
                )
            docs = self._fallback_query(
                query=query,
                content_type=effective_content_type,
                limit=max(size * 2, 30),
                intent=intent,
            )
            docs = [doc for doc in docs if self._matches_structured_intent(doc, intent, effective_content_type)]
            structured_broadening = False
            if not docs and (intent.get("genres") or intent.get("country")) and (
                intent.get("terms") or intent.get("expandedTerms") or intent.get("explicitTerms")
            ):
                docs = self._fallback_query(
                    query=query,
                    content_type=effective_content_type,
                    limit=max(size * 2, 30),
                    intent=self._structured_only_fallback_intent(intent),
                )
                docs = [doc for doc in docs if self._matches_structured_intent(doc, intent, effective_content_type)]
                structured_broadening = bool(docs)
            self._add_rank_scores(
                docs,
                source="sqlpp_structured_broadening" if structured_broadening else "sqlpp_fallback",
            )
            before = len(docs)
            override_genres: list[str] = []
            if purpose in {"recommendation", "search"} and login_id and not intent.get("profileQuery"):
                disliked = self._normalised_values(viewer_profile.get("dislikedGenres", []))
                override_genres = [
                    str(value)
                    for value in intent.get("genres", [])
                    if re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip() in disliked
                ]
            docs = self._exclude_with_signals(
                docs,
                login_id,
                purpose,
                ignored_disliked_genres=override_genres,
                viewer_signals_context=viewer_signals_context,
            )[:size]
            fallback_mode = "sqlpp_structured_broadening" if structured_broadening else "sqlpp_fallback"
            self._finalise_display_scores(
                docs,
                requested_mode=requested_mode,
                effective_mode=fallback_mode,
                ranking_strategy="sqlpp_popularity_quality",
                mode_reason=f"Search failed: {type(exc).__name__}",
            )
            inventory_count = 0
            if not docs and len(intent.get("genres") or []) == 1:
                try:
                    inventory_count = int(
                        self.count_titles(
                            content_type=effective_content_type,
                            genre=str(intent["genres"][0]),
                        ).get("count")
                        or 0
                    )
                except Exception:
                    inventory_count = 0
            trace = {
                "mode": fallback_mode,
                "effectiveMode": fallback_mode,
                "requestedMode": requested_mode,
                "effectiveModeBeforeFallback": effective_mode,
                "modeReason": f"Search failed: {type(exc).__name__}",
                "rankingStrategy": "sqlpp_popularity_quality",
                "scoreMeaning": "Relative final rank score for this result set; not a probability.",
                "purpose": purpose,
                "query": query,
                "explicitTerms": intent.get("explicitTerms", []),
                "error": f"{type(exc).__name__}: {exc}",
                "returnedCount": len(docs),
                "catalogueInventoryCount": inventory_count,
                "structuredBroadeningApplied": structured_broadening,
                "excludedByViewerSignals": before - len(docs),
                "selectedTitleIds": [doc.get("id") for doc in docs],
                "expandedTerms": intent.get("expandedTerms", []),
                "structuredIntent": intent.get("country", {}).get("label") if intent.get("country") else None,
                "structuredGenres": list(intent.get("genres", [])),
                "structuredContentType": effective_content_type,
                "structuredPlanFilters": effective_plan_filters,
                "temporaryPreferenceOverrides": [f"disliked genre: {value}" for value in override_genres],
                "exclusivePreferencePolicy": exclusive_policy,
                "exclusivePreferenceConflict": exclusive_request_conflict,
                "fallbackToFts": requested_mode != effective_mode,
                "searchCircuitOpen": True,
                "searchFailureCount": int(
                    getattr(self, "_search_failure_count", 0)
                ),
                **embedding_meta,
                "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
                "services": ["Query", "Index", "Data/KV"],
            }
            self._write_trace(login_id, "catalogue_search_fallback", trace)
            return {"results": docs, "trace": trace}

    def _embedding_cache_key(self, text: str) -> str:
        normalised = re.sub(r"\s+", " ", text.strip().lower())
        digest = hashlib.sha256(
            f"{self.settings.embedding_model}:{normalised}".encode("utf-8")
        ).hexdigest()
        return f"query-embedding::{digest}"

    def embed_with_trace(self, text: str) -> tuple[list[float], dict[str, Any]]:
        started = time.perf_counter()
        key = self._embedding_cache_key(text)
        cached = self._cache_get(key, self.settings.embedding_query_cache_seconds)
        if isinstance(cached, list) and cached:
            return [float(value) for value in cached], {
                "embeddingModel": self.settings.embedding_model,
                "embeddingModelCalls": 0,
                "embeddingCacheHits": 1,
                "embeddingTimeouts": 0,
                "embeddingElapsedMs": round((time.perf_counter() - started) * 1000, 1),
                "embeddingError": None,
            }
        vector = self._embedding_provider.embed(text, input_type="query")
        self._cache_put(key, vector)
        return vector, {
            "embeddingModel": self.settings.embedding_model,
            "embeddingModelCalls": 1,
            "embeddingCacheHits": 0,
            "embeddingTimeouts": 0,
            "embeddingElapsedMs": round((time.perf_counter() - started) * 1000, 1),
            "embeddingError": None,
        }

    def embed(self, text: str) -> list[float]:
        vector, _ = self.embed_with_trace(text)
        return vector

    def _matches_country_intent(self, doc: dict[str, Any], country: dict[str, Any]) -> bool:
        countries = {str(item).upper() for item in doc.get("originCountries", [])}
        language = str(doc.get("originalLanguage") or "").lower()
        return bool(countries.intersection({str(x).upper() for x in country.get("countries", [])})) or language in {
            str(x).lower() for x in country.get("languages", [])
        }

    def _matches_structured_intent(
        self,
        doc: dict[str, Any],
        intent: dict[str, Any],
        content_type: str | None,
    ) -> bool:
        if content_type and str(doc.get("contentType") or "") != content_type:
            return False
        doc_genres = self._normalised_values(doc.get("genres", []))
        for genre in intent.get("genres", []):
            if re.sub(r"[^a-z0-9]+", " ", str(genre).lower()).strip() not in doc_genres:
                return False
        if intent.get("country") and not self._matches_country_intent(doc, intent["country"]):
            return False
        if not self._matches_plan_filters(
            doc, dict(intent.get("planFilters") or {})
        ):
            return False
        return True

    @staticmethod
    def _fragment_has_highlight(value: str) -> bool:
        return bool(
            re.search(
                r"<(?:mark\b|span\b[^>]*\bclass=[\"'][^\"']*(?:highlight|search-highlight)[^\"']*[\"'])",
                value,
                flags=re.IGNORECASE,
            )
        )

    @staticmethod
    def _strip_highlight_markup(value: str) -> str:
        return re.sub(r"<[^>]+>", "", value).strip()

    @classmethod
    def _highlight_terms(cls, value: str) -> list[str]:
        terms = re.findall(
            r"<(?:mark\b[^>]*|span\b[^>]*\bclass=[\"'][^\"']*(?:highlight|search-highlight)[^\"']*[\"'][^>]*)>(.*?)</(?:mark|span)>",
            value,
            flags=re.IGNORECASE | re.DOTALL,
        )
        return [cls._strip_highlight_markup(term) for term in terms if cls._strip_highlight_markup(term)]

    def _match_explanation(
        self,
        doc: dict[str, Any],
        hit: dict[str, Any] | None,
        intent: dict[str, Any],
        *,
        effective_mode: str = "fts",
        requested_mode: str | None = None,
    ) -> dict[str, Any]:
        requested_genres = list(intent.get("genres", []))
        if requested_genres and not intent.get("terms") and not intent.get("expandedTerms"):
            doc_genres = {str(value).lower(): str(value) for value in doc.get("genres", [])}
            matched = [
                {"field": "genres", "value": doc_genres.get(str(genre).lower(), genre), "matchType": "exact"}
                for genre in requested_genres
                if str(genre).lower() in doc_genres
            ]
            if matched:
                label = ", ".join(str(item["value"]) for item in matched)
                fields = list(matched)
                if intent.get("contentType"):
                    fields.append({"field": "contentType", "value": intent["contentType"], "matchType": "exact"})
                return {
                    "summary": f"Matched genre: {label}",
                    "matchedFields": fields,
                    "evidenceType": "structured_exact",
                    "effectiveMode": effective_mode,
                    "requestedMode": requested_mode or effective_mode,
                }
        if intent.get("country") and self._matches_country_intent(doc, intent["country"]):
            country = intent["country"]
            matched = []
            countries = set(doc.get("originCountries", []))
            for code in country.get("countries", []):
                if code in countries:
                    matched.append({"field": "originCountries", "value": code, "matchType": "exact"})
            if doc.get("originalLanguage") in country.get("languages", []):
                matched.append({"field": "originalLanguage", "value": doc.get("originalLanguage"), "matchType": "exact"})
            return {
                "summary": country.get("label"),
                "matchedFields": matched,
                "evidenceType": "structured_exact",
                "effectiveMode": effective_mode,
                "requestedMode": requested_mode or effective_mode,
            }

        fragments = (hit or {}).get("fragments") or {}
        matched_fields: list[dict[str, Any]] = []
        if effective_mode in {"fts", "hybrid"}:
            for field, values in fragments.items():
                fragment_values = values if isinstance(values, list) else [values]
                highlighted_fragments = [
                    str(value) for value in fragment_values if self._fragment_has_highlight(str(value))
                ]
                highlighted = [self._strip_highlight_markup(value) for value in highlighted_fragments]
                highlighted = [value for value in highlighted if value]
                highlight_terms = self._dedupe(
                    term for fragment in highlighted_fragments for term in self._highlight_terms(fragment)
                )
                if highlighted:
                    matched_term = highlight_terms[0] if highlight_terms else ""
                    expanded_terms = {str(value).lower() for value in intent.get("expandedTerms", [])}
                    raw_query = str(intent.get("raw") or "").lower().strip()
                    matched_fields.append(
                        {
                            "field": field,
                            "value": " … ".join(highlighted)[:300],
                            "matchedTerm": matched_term,
                            "queryTerm": raw_query,
                            "matchType": (
                                "expanded_lexical_term"
                                if matched_term.lower() in expanded_terms and matched_term.lower() not in raw_query
                                else "lexical_highlight"
                            ),
                        }
                    )

        if effective_mode in {"fts", "hybrid"} and not matched_fields:
            terms = [str(x).lower() for x in intent.get("expandedTerms", [])]
            terms.extend(str(x).lower() for x in intent.get("terms", []))
            terms = [term for term in self._dedupe(terms) if term]
            for field in ("title", "keywords", "genres", "overview"):
                value = doc.get(field)
                values = value if isinstance(value, list) else [value]
                actual_matches: list[tuple[str, str]] = []
                for candidate in values:
                    candidate_text = str(candidate or "")
                    candidate_lower = candidate_text.lower()
                    matched_term = next((term for term in terms if term in candidate_lower), None)
                    if matched_term:
                        actual_matches.append((candidate_text, matched_term))
                if actual_matches:
                    values_text = " · ".join(value for value, _ in actual_matches)[:220]
                    matched_term = actual_matches[0][1]
                    expanded_terms = {str(value).lower() for value in intent.get("expandedTerms", [])}
                    raw_query = str(intent.get("raw") or "").lower().strip()
                    matched_fields.append(
                        {
                            "field": field,
                            "value": values_text,
                            "matchedTerm": matched_term,
                            "queryTerm": raw_query,
                            "matchType": (
                                "expanded_lexical_term"
                                if matched_term in expanded_terms and matched_term not in raw_query
                                else "lexical_term"
                            ),
                        }
                    )

        semantic_field = {
            "field": "semanticQuery",
            "value": str(intent.get("raw") or ""),
            "matchType": "vector_similarity",
        }
        if effective_mode == "vector":
            summary = (
                "Semantic similarity only: nearest neighbour with no direct query term in visible metadata"
                if not matched_fields
                else "Semantic similarity across the catalogue embedding"
            )
            fields = [semantic_field]
            evidence_type = "semantic"
        elif effective_mode == "hybrid":
            fields = [*matched_fields[:4], semantic_field]
            summary = (
                "Hybrid match: lexical evidence plus semantic similarity"
                if matched_fields
                else "Hybrid result admitted by Vector Search only; no direct lexical term was found"
            )
            evidence_type = "hybrid" if matched_fields else "hybrid_vector_only"
        else:
            fields = matched_fields[:5]
            summary = (
                "Lexical match in " + ", ".join(item["field"] for item in fields[:3])
                if fields
                else "Search relevance"
            )
            evidence_type = "lexical"
        return {
            "summary": summary,
            "matchedFields": fields[:5],
            "evidenceType": evidence_type,
            "effectiveMode": effective_mode,
            "requestedMode": requested_mode or effective_mode,
            "query": str(intent.get("raw") or ""),
            "expandedTerms": list(intent.get("expandedTerms", [])),
            "directLexicalEvidence": bool(matched_fields),
            "semanticOnly": bool(effective_mode in {"vector", "hybrid"} and not matched_fields),
            "semanticScope": "title, type, genres, keywords, overview, directors and main cast",
        }

    def title(self, title_id: str, *, include_internal: bool = False) -> dict[str, Any]:
        doc = self.titles.get(title_id).content_as[dict]
        self._decorate(doc, include_internal=include_internal)
        return doc

    # ------------------------------------------------------------------
    # Authoritative history and interactions
    # ------------------------------------------------------------------
    def list_watch_history(
        self, *, login_id: str, content_type: str | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        statement = (
            f"SELECT h.titleId, h.progressPct, h.status, h.lastWatchedAt, h.device "
            f"FROM {self.history_keyspace} AS h "
            "WHERE h.viewerId=$viewerId AND h.progressPct > 0 "
            "ORDER BY h.lastWatchedAt DESC LIMIT $limit"
        )
        rows = list(
            self.cluster.query(
                statement,
                QueryOptions(
                    named_parameters={"viewerId": login_id, "limit": min(max(limit, 1), 100)},
                    readonly=True,
                    scan_consistency=QueryScanConsistency.REQUEST_PLUS,
                ),
            ).rows()
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                doc = self.title(str(row.get("titleId") or ""))
            except Exception:
                continue
            if content_type and doc.get("contentType") != content_type:
                continue
            doc["progressPct"] = float(row.get("progressPct") or 0)
            doc["watchStatus"] = row.get("status") or (
                "completed" if doc["progressPct"] >= 85 else "in_progress"
            )
            doc["lastWatchedAt"] = row.get("lastWatchedAt")
            doc["watchDevice"] = row.get("device")
            doc["recommendationSource"] = "watch_history"
            doc["viewerState"] = "watched"
            items.append(doc)
        self._write_trace(
            login_id,
            "watch_history_read",
            {"contentType": content_type, "returnedCount": len(items), "services": ["Query", "Index", "Data/KV"]},
        )
        return items

    def resolve_title(self, query: str, *, content_type: str | None = None) -> dict[str, Any]:
        value = query.strip()
        if not value:
            return {"match": None, "alternatives": [], "trace": {"mode": "fts_title_resolution"}}
        started = time.perf_counter()
        lexical: dict[str, Any] = {
            "disjuncts": [
                {"match_phrase": value, "field": "title", "boost": 20.0},
                {"match": value, "field": "title", "boost": 12.0, "fuzziness": 2},
                {"match": value, "field": "originalTitle", "boost": 8.0, "fuzziness": 2},
            ],
            "min": 1,
        }
        query_obj: dict[str, Any] = lexical
        if content_type:
            query_obj = {"conjuncts": [lexical, {"term": content_type, "field": "contentType"}]}
        payload = {"size": 5, "query": query_obj, "fields": ["title", "originalTitle", "contentType", "releaseYear"]}
        docs: list[dict[str, Any]] = []
        error: str | None = None
        try:
            response = self._search_client.post(f"{self.search_index_path}/query", json=payload, timeout=5.0)
            response.raise_for_status()
            hits = list(response.json().get("hits", []))
            docs = self._fetch_hits(hits, content_type=content_type)
            self._apply_search_scores(docs, hits, source="fts_title_resolution", intent=self._analyse_query(value))
            needle = self._normalize_title(value)
            for doc in docs:
                doc["titleResolutionScore"] = round(
                    SequenceMatcher(None, needle, self._normalize_title(str(doc.get("title") or ""))).ratio(), 4
                )
            docs.sort(key=lambda item: float(item.get("titleResolutionScore") or 0), reverse=True)
            # Search fuzziness is for candidate generation, not authority. Reject
            # weak title similarities so an unrelated catalogue item is never
            # presented as the requested title.
            docs = [
                doc for doc in docs
                if float(doc.get("titleResolutionScore") or 0.0) >= 0.72
                or needle == self._normalize_title(str(doc.get("originalTitle") or ""))
            ]
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        if not docs:
            docs = self._fuzzy_title_fallback(value, content_type=content_type, limit=5)
        trace = {
            "mode": "fts_title_resolution" if error is None else "fuzzy_sqlpp_fallback",
            "query": value,
            "index": self.settings.catalogue_search_index,
            "returnedCount": len(docs),
            "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
            "services": ["Search/FTS", "Data/KV"] if error is None else ["Query", "Index", "Data/KV"],
        }
        if error:
            trace["searchError"] = error
        return {"match": docs[0] if docs else None, "alternatives": docs[1:5], "trace": trace}

    @staticmethod
    def _normalize_title(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    def _fuzzy_title_fallback(self, query: str, *, content_type: str | None, limit: int) -> list[dict[str, Any]]:
        where = ""
        params: dict[str, Any] = {"limit": 5000}
        if content_type:
            where = " WHERE t.contentType=$contentType"
            params["contentType"] = content_type
        rows = self._query_titles(f"SELECT t.* FROM {self.titles_keyspace} AS t{where} LIMIT $limit", params)
        needle = self._normalize_title(query)
        scored: list[tuple[float, dict[str, Any]]] = []
        for doc in rows:
            candidate = self._normalize_title(str(doc.get("title") or ""))
            original = self._normalize_title(str(doc.get("originalTitle") or ""))
            score = max(
                SequenceMatcher(None, needle, candidate).ratio(),
                SequenceMatcher(None, needle, original).ratio() if original else 0.0,
            )
            if score >= 0.72:
                doc["titleResolutionScore"] = round(score, 4)
                doc["matchPct"] = round(score * 100)
                doc["recommendationSource"] = "fuzzy_sqlpp_fallback"
                doc["matchExplanation"] = {"summary": "Fuzzy title resolution", "matchedFields": [{"field": "title", "value": doc.get("title"), "matchType": "fuzzy"}]}
                scored.append((score, doc))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [doc for _, doc in scored[:limit]]


    def count_titles(
        self, *, content_type: str | None = None, genre: str | None = None
    ) -> dict[str, Any]:
        """Return an exact catalogue inventory count using structured filters."""
        where: list[str] = []
        params: dict[str, Any] = {}
        if content_type:
            where.append("t.contentType=$contentType")
            params["contentType"] = content_type
        if genre:
            where.append("ANY g IN t.genres SATISFIES LOWER(g)=LOWER($genre) END")
            params["genre"] = genre
        clause = " WHERE " + " AND ".join(where) if where else ""
        statement = f"SELECT RAW COUNT(1) FROM {self.titles_keyspace} AS t{clause}"
        result = self.cluster.query(statement, QueryOptions(named_parameters=params, readonly=True))
        count = int(next(iter(result.rows()), 0))
        return {
            "count": count,
            "contentType": content_type,
            "genre": genre,
            "queryMode": "sqlpp_aggregate",
        }

    def query_catalogue_analytics(
        self,
        *,
        measure: str,
        dimension: str,
        filters: dict[str, Any] | None = None,
        limit: int = 30,
        order: str = "desc",
    ) -> dict[str, Any]:
        """Execute a read-only aggregate from a strict, non-SQL tool schema."""
        dimension_fields = {
            "genre": ("genre", " UNNEST t.genres AS genre"),
            "release_year": ("t.releaseYear", ""),
            "original_language": ("t.originalLanguage", ""),
            "country": ("country", " UNNEST t.originCountries AS country"),
            "content_type": ("t.contentType", ""),
        }
        measure_expressions = {
            "count": "COUNT(1)",
            "average_rating": "AVG(t.voteAverage)",
            "average_runtime": "AVG(COALESCE(t.runtimeMinutes, t.episodeRuntimeMinutes))",
        }
        if dimension not in dimension_fields:
            raise ValueError(f"Unsupported analytics dimension: {dimension}")
        if measure not in measure_expressions:
            raise ValueError(f"Unsupported analytics measure: {measure}")
        filters = dict(filters or {})
        where: list[str] = []
        params: dict[str, Any] = {"limit": min(max(int(limit), 1), 50)}
        content_type = filters.get("contentType")
        genre_filter = filters.get("genre")
        release_year = filters.get("releaseYear")
        if content_type:
            where.append("t.contentType=$contentType")
            params["contentType"] = str(content_type)
        if genre_filter:
            where.append("ANY g IN t.genres SATISFIES LOWER(g)=LOWER($genre) END")
            params["genre"] = str(genre_filter)
        if release_year is not None:
            where.append("t.releaseYear=$releaseYear")
            params["releaseYear"] = int(release_year)
        field, unnest = dimension_fields[dimension]
        where.append(f"{field} IS VALUED")
        if measure == "average_rating":
            where.append("t.voteAverage IS VALUED")
        if measure == "average_runtime":
            where.append(
                "(t.runtimeMinutes IS VALUED OR t.episodeRuntimeMinutes IS VALUED)"
            )
        direction = "ASC" if str(order).lower() == "asc" else "DESC"
        statement = (
            f"SELECT {field} AS label, "
            f"{measure_expressions[measure]} AS metricValue "
            f"FROM {self.titles_keyspace} AS t{unnest} "
            f"WHERE {' AND '.join(where)} GROUP BY {field} "
            f"ORDER BY metricValue {direction}, label ASC LIMIT $limit"
        )
        result = self.cluster.query(
            statement,
            QueryOptions(
                named_parameters=params,
                readonly=True,
                scan_consistency=QueryScanConsistency.REQUEST_PLUS,
            ),
        )
        rows = [
            {"label": row.get("label"), "value": row.get("metricValue")}
            for row in result.rows()
        ]
        return {
            "measure": measure,
            "dimension": dimension,
            "filters": {
                "contentType": content_type,
                "genre": genre_filter,
                "releaseYear": release_year,
            },
            "rows": rows,
            "queryMode": "validated_sqlpp_analytics",
            "readOnly": True,
            "expandedBeyondSchema": False,
        }

    def top_rated_title(self, *, content_type: str | None = None) -> dict[str, Any] | None:
        where = "WHERE t.voteAverage IS VALUED"
        params: dict[str, Any] = {}
        if content_type:
            where += " AND t.contentType=$contentType"
            params["contentType"] = content_type
        statement = (
            f"SELECT t.* FROM {self.titles_keyspace} AS t {where} "
            "ORDER BY t.voteAverage DESC, t.voteCount DESC, t.popularity DESC LIMIT 1"
        )
        rows = self._query_titles(statement, params)
        if not rows:
            return None
        title = dict(rows[0])
        title["recommendationSource"] = "catalogue_rating"
        title["matchExplanation"] = {
            "summary": "Highest verified viewer rating in the current catalogue",
            "matchedFields": [
                {"field": "voteAverage", "value": title.get("voteAverage"), "matchType": "ranking"},
                {"field": "voteCount", "value": title.get("voteCount"), "matchType": "tie_break"},
            ],
        }
        return title

    def title_affinity(self, login_id: str, title_or_id: dict[str, Any] | str) -> dict[str, Any]:
        title = title_or_id if isinstance(title_or_id, dict) else self.title(str(title_or_id))
        profile = self.get_profile(login_id)
        title_id = str(title.get("id") or "")
        liked = {str(value) for value in profile.get("likedTitleIds", [])}
        disliked = {str(value) for value in profile.get("dislikedTitleIds", [])}
        if title_id and title_id in liked:
            return {"verdict": "liked", "score": 100, "reasons": ["already in liked titles"]}
        if title_id and title_id in disliked:
            return {"verdict": "disliked", "score": 0, "reasons": ["already in disliked titles"]}
        evidence = self._profile_match_evidence(title, profile)
        violations = self._preference_violations(title, profile)
        reasons: list[str] = []
        summary = str(evidence.get("summary") or "")
        if summary and summary != "No direct profile evidence":
            reasons.append(summary.removeprefix("Matched "))
        reasons.extend(str(value) for value in violations[:3])
        score = int(evidence.get("score") or 0)
        if score > 0 and violations:
            verdict = "mixed"
        elif violations:
            verdict = "unlikely"
        elif score > 0:
            verdict = "likely"
        else:
            verdict = "unknown"
        return {
            "verdict": verdict,
            "score": score,
            "reasons": reasons,
            "matchedFields": list(evidence.get("matchedFields") or []),
            "violations": violations,
        }

    def similar_to_title(
        self,
        *,
        login_id: str,
        title_query: str,
        content_type: str | None = None,
        limit: int = 12,
    ) -> dict[str, Any]:
        """Find catalogue titles similar to an authoritative catalogue seed."""
        started = time.perf_counter()
        resolution = self.resolve_title(title_query, content_type=content_type)
        seed = resolution.get("match")
        if not seed:
            trace = dict(resolution.get("trace") or {})
            trace.update({"mode": "similar_seed_unresolved", "seedQuery": title_query})
            return {"seed": None, "results": [], "trace": trace}

        internal = self.title(str(seed["id"]), include_internal=True)
        docs: list[dict[str, Any]] = []
        mode = "vector_similarity"
        error: str | None = None
        vector = internal.get("embedding")
        if vector:
            payload = {
                "size": max(limit * 4, 40),
                "knn": [{"field": "embedding", "vector": vector, "k": max(limit * 4, 40)}],
            }
            try:
                response = self._search_client.post(
                    f"{self.search_index_path}/query",
                    json=payload,
                    timeout=self.settings.search_query_timeout_seconds,
                )
                response.raise_for_status()
                hits = list(response.json().get("hits", []))
                docs = [
                    item for item in self._fetch_hits(hits, content_type=content_type or seed.get("contentType"))
                    if str(item.get("id")) != str(seed.get("id"))
                    and (not content_type or item.get("contentType") == content_type)
                ]
                self._apply_search_scores(
                    docs,
                    hits,
                    source="vector_similarity",
                    intent=self._analyse_query(str(seed.get("title") or title_query)),
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"

        if not docs:
            mode = "genre_similarity_fallback"
            genres = [str(value) for value in seed.get("genres", [])[:1] if value]
            if genres:
                query = " ".join(genres)
                intent = self._analyse_query(query)
                intent["genres"] = genres
                intent["genre"] = genres[0]
                docs = self._fallback_query(
                    query=query,
                    content_type=seed.get("contentType"),
                    limit=max(limit * 3, 30),
                    intent=intent,
                )
                docs = [item for item in docs if str(item.get("id")) != str(seed.get("id"))]

        docs = self._exclude_by_purpose(docs, login_id, "recommendation")[:limit]
        for doc in docs:
            doc["recommendationSource"] = mode
            doc.setdefault(
                "matchExplanation",
                {
                    "summary": f"Similar to {seed.get('title')}",
                    "matchedFields": [{"field": "seedTitle", "value": seed.get("title"), "matchType": mode}],
                },
            )
        trace = {
            "mode": mode,
            "seedQuery": title_query,
            "seedTitleId": seed.get("id"),
            "seedTitle": seed.get("title"),
            "returnedCount": len(docs),
            "selectedTitleIds": [doc.get("id") for doc in docs],
            "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
            "services": ["Search/Vector", "Data/KV"] if mode == "vector_similarity" else ["Query", "Index", "Data/KV"],
        }
        if error:
            trace["vectorError"] = error
        self._write_trace(login_id, "similar_title_recommendation", trace)
        return {"seed": seed, "results": docs, "trace": trace}

    def current_my_list(self, login_id: str, *, limit: int = 25) -> dict[str, Any]:
        """Return exact current watchlist records from authoritative viewer state."""
        started = time.perf_counter()
        signals = self.viewer_signals(login_id)
        profile_ids = [
            str(value) for value in signals.get("profile", {}).get("watchlistTitleIds", [])
            if value
        ]
        ordered_ids = self._dedupe(
            [*profile_ids, *sorted(str(value) for value in signals.get("watchlist", set()))]
        )[: min(max(limit, 1), 100)]
        docs = self._fetch_title_ids(ordered_ids)
        self._mark_viewer_state(docs, signals)
        docs = self._apply_entitlement_policy(docs, login_id, remove_denied=False)
        trace = {
            "mode": "authoritative_my_list",
            "hardConstraint": "profile.watchlistTitleIds",
            "requestedTitleIds": ordered_ids,
            "selectedTitleIds": [doc.get("id") for doc in docs],
            "returnedCount": len(docs),
            "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
            "services": ["Data/KV", "Query", "Index"],
        }
        self._write_trace(login_id, "current_my_list_query", trace)
        return {"results": docs, "trace": trace}

    def recent_my_list_additions(
        self, login_id: str, *, limit: int = 10
    ) -> dict[str, Any]:
        """Read completed My List receipts in reverse chronological order."""
        started = time.perf_counter()
        bounded = min(max(limit, 1), 25)
        rows: list[dict[str, Any]] = []
        source = "action_receipts"
        try:
            result = self.cluster.query(
                f"SELECT r.titleId, r.title, r.timestamp, r.status "
                f"FROM {self.action_receipts_keyspace} AS r "
                "WHERE r.viewerId=$viewerId AND r.action='watchlist' "
                "AND r.status='completed' ORDER BY r.timestamp DESC LIMIT $limit",
                QueryOptions(
                    named_parameters={
                        "viewerId": login_id,
                        "limit": min(bounded * 5, 100),
                    },
                    readonly=True,
                    scan_consistency=QueryScanConsistency.REQUEST_PLUS,
                ),
            )
            rows = [dict(row) for row in result.rows()]
        except Exception:
            source = "interaction_fallback"
            result = self.cluster.query(
                f"SELECT i.titleId, i.title, i.timestamp, 'completed' AS status "
                f"FROM {self.interactions_keyspace} AS i "
                "WHERE i.viewerId=$viewerId AND i.action='watchlist' "
                "ORDER BY i.timestamp DESC LIMIT $limit",
                QueryOptions(
                    named_parameters={
                        "viewerId": login_id,
                        "limit": min(bounded * 5, 100),
                    },
                    readonly=True,
                    scan_consistency=QueryScanConsistency.REQUEST_PLUS,
                ),
            )
            rows = [dict(row) for row in result.rows()]

        signals = self.viewer_signals(login_id)
        docs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            title_id = str(row.get("titleId") or "")
            if not title_id or title_id in seen:
                continue
            seen.add(title_id)
            try:
                doc = self.title(title_id)
            except Exception:
                continue
            doc["actionTimestamp"] = row.get("timestamp")
            doc["currentlyInMyList"] = title_id in signals.get("watchlist", set())
            docs.append(doc)
            if len(docs) >= bounded:
                break
        self._mark_viewer_state(docs, signals)
        docs = self._apply_entitlement_policy(docs, login_id, remove_denied=False)
        trace = {
            "mode": "recent_my_list_action_receipts",
            "receiptSource": source,
            "hardConstraint": "viewerId + action=watchlist + status=completed",
            "selectedTitleIds": [doc.get("id") for doc in docs],
            "returnedCount": len(docs),
            "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
            "services": ["Query", "Index", "Data/KV"],
        }
        self._write_trace(login_id, "recent_my_list_additions_query", trace)
        return {"results": docs, "trace": trace}

    @staticmethod
    def _mean_normalised_vector(vectors: list[list[float]]) -> list[float] | None:
        if not vectors:
            return None
        dimension = len(vectors[0])
        usable = [vector for vector in vectors if len(vector) == dimension and dimension]
        if not usable:
            return None
        centroid = [
            sum(float(vector[index]) for vector in usable) / len(usable)
            for index in range(dimension)
        ]
        magnitude = sum(value * value for value in centroid) ** 0.5
        return [value / magnitude for value in centroid] if magnitude else centroid

    def similar_to_title_ids(
        self,
        *,
        login_id: str,
        title_ids: list[str],
        content_type: str | None = None,
        limit: int = 12,
        source_label: str = "the previous results",
    ) -> dict[str, Any]:
        """Run one bounded multi-seed vector search over exact catalogue IDs."""
        started = time.perf_counter()
        seed_ids = self._dedupe(title_ids)[:5]
        internal_seeds: list[dict[str, Any]] = []
        for title_id in seed_ids:
            try:
                internal_seeds.append(self.title(title_id, include_internal=True))
            except Exception:
                continue
        public_seeds: list[dict[str, Any]] = []
        for item in internal_seeds:
            public_item = dict(item)
            self._decorate(public_item)
            public_seeds.append(public_item)

        vectors = [
            [float(value) for value in item.get("embedding", [])]
            for item in internal_seeds
            if item.get("embedding")
        ]
        centroid = self._mean_normalised_vector(vectors)
        docs: list[dict[str, Any]] = []
        mode = "multi_seed_vector_centroid"
        vector_error: str | None = None
        bounded_limit = min(max(limit, 1), 25)
        if centroid:
            payload = {
                "size": max(bounded_limit * 4, 40),
                "knn": [
                    {
                        "field": "embedding",
                        "vector": centroid,
                        "k": max(bounded_limit * 4, 40),
                    }
                ],
            }
            try:
                response = self._search_client.post(
                    f"{self.search_index_path}/query",
                    json=payload,
                    timeout=self.settings.search_query_timeout_seconds,
                )
                response.raise_for_status()
                hits = list(response.json().get("hits", []))
                seed_set = set(seed_ids)
                docs = [
                    item
                    for item in self._fetch_hits(hits, content_type=content_type)
                    if str(item.get("id")) not in seed_set
                ]
                self._apply_search_scores(
                    docs,
                    hits,
                    source=mode,
                    intent=self._analyse_query(
                        " ".join(str(seed.get("title") or "") for seed in public_seeds)
                    ),
                )
            except Exception as exc:
                vector_error = f"{type(exc).__name__}: {exc}"

        if not docs and public_seeds:
            mode = "multi_seed_genre_fallback"
            genres = self._dedupe(
                genre
                for seed in public_seeds
                for genre in seed.get("genres", [])[:2]
            )[:4]
            if genres:
                intent = self._analyse_query(" ".join(genres))
                intent["genres"] = genres
                docs = self._fallback_query(
                    query=" ".join(genres),
                    content_type=content_type,
                    limit=max(bounded_limit * 4, 40),
                    intent=intent,
                )
                seed_set = set(seed_ids)
                docs = [
                    item for item in docs if str(item.get("id")) not in seed_set
                ]

        docs = self._exclude_by_purpose(docs, login_id, "recommendation")[:bounded_limit]
        seed_names = [str(item.get("title")) for item in public_seeds if item.get("title")]
        for doc in docs:
            doc["recommendationSource"] = mode
            explanation = dict(doc.get("matchExplanation") or {})
            explanation["summary"] = (
                f"Vector similarity to the combined seed set: {', '.join(seed_names)}"
            )
            explanation["matchedFields"] = [
                {"field": "seedTitle", "value": name, "matchType": mode}
                for name in seed_names
            ]
            doc["matchExplanation"] = explanation

        trace = {
            "mode": mode,
            "sourceLabel": source_label,
            "seedAggregation": "mean_normalised_centroid",
            "hardConstraint": "exact seed title IDs; seed titles excluded from output",
            "seedTitleIds": [seed.get("id") for seed in public_seeds],
            "seedTitles": seed_names,
            "returnedCount": len(docs),
            "selectedTitleIds": [doc.get("id") for doc in docs],
            "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
            "services": (
                ["Search/Vector", "Data/KV"]
                if mode == "multi_seed_vector_centroid"
                else ["Query", "Index", "Data/KV"]
            ),
        }
        if vector_error:
            trace["vectorError"] = vector_error
        self._write_trace(login_id, "multi_seed_similarity", trace)
        return {"seeds": public_seeds, "results": docs, "trace": trace}

    def similar_to_my_list(
        self, login_id: str, *, content_type: str | None = None, limit: int = 12
    ) -> dict[str, Any]:
        my_list = self.current_my_list(login_id, limit=25)
        return self.similar_to_title_ids(
            login_id=login_id,
            title_ids=[
                str(item.get("id")) for item in my_list.get("results", []) if item.get("id")
            ],
            content_type=content_type,
            limit=limit,
            source_label="My List",
        )

    def filter_grounded_results(
        self,
        *,
        login_id: str,
        title_ids: list[str],
        filter_kind: str,
    ) -> dict[str, Any]:
        """Intersect a follow-up filter with exact preceding result IDs."""
        started = time.perf_counter()
        requested_ids = self._dedupe(title_ids)[:25]
        docs = self._fetch_title_ids(requested_ids)
        signals = self.viewer_signals(login_id)
        entitlement = self.ensure_entitlement(login_id)
        profile = signals.get("profile") or self.get_profile(login_id)
        filtered: list[dict[str, Any]] = []
        for doc in docs:
            title_id = str(doc.get("id") or "")
            decision = self.check_entitlement(
                login_id,
                doc,
                entitlement=entitlement,
                profile=profile,
                include_progress=False,
            )
            doc["entitlement"] = decision
            doc["includedInPlan"] = bool(decision.get("includedInPlan"))
            doc["availabilityStatus"] = decision.get("decision")
            allowed = bool(decision.get("allowed"))
            include = {
                "playable": bool(decision.get("includedInPlan")),
                "available": allowed,
                "in_my_list": allowed and title_id in signals.get("watchlist", set()),
                "missing_from_my_list": allowed
                and title_id not in signals.get("watchlist", set()),
                "watched": allowed and title_id in signals.get("watched", set()),
                "unwatched": allowed and title_id not in signals.get("watched", set()),
                "liked": allowed and title_id in signals.get("liked", set()),
                "movies_only": allowed and doc.get("contentType") == "movie",
                "series_only": allowed and doc.get("contentType") == "tv",
                "top_rated": allowed,
            }.get(filter_kind, False)
            if include:
                filtered.append(doc)

        if filter_kind == "top_rated" and filtered:
            filtered = [
                max(
                    filtered,
                    key=lambda item: (
                        float(item.get("voteAverage") or -1),
                        int(item.get("voteCount") or 0),
                    ),
                )
            ]
        self._mark_viewer_state(filtered, signals)
        trace = {
            "mode": "contextual_result_intersection",
            "filter": filter_kind,
            "hardConstraint": "candidate titleId must be in previous grounded result IDs",
            "inputTitleIds": requested_ids,
            "selectedTitleIds": [doc.get("id") for doc in filtered],
            "returnedCount": len(filtered),
            "expandedBeyondPreviousResults": False,
            "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
            "services": ["Data/KV", "Query", "Index"],
        }
        self._write_trace(login_id, "contextual_result_filter", trace)
        return {"results": filtered, "trace": trace}

    def record_interaction(
        self,
        *,
        login_id: str,
        title_id: str,
        action: str,
        progress_pct: float | None = None,
    ) -> dict[str, Any]:
        title = self.title(title_id)
        entitlement_decision = self.check_entitlement(login_id, title)
        if action in {"like", "watchlist"} and not entitlement_decision.get("allowed"):
            operation = "liked" if action == "like" else "added to the watchlist"
            raise ValueError(
                f"{title.get('title')} cannot be {operation}: {entitlement_decision.get('reason')}"
            )
        if action in {"play", "progress", "complete"} and not entitlement_decision.get("includedInPlan"):
            reason = entitlement_decision.get("reason") or "not included in the current plan"
            raise ValueError(
                f"{title.get('title')} cannot be played for this viewer: {reason}"
            )
        now = time.time()
        interaction = {
            "type": "viewer_interaction",
            "viewerId": login_id,
            "titleId": title_id,
            "action": action,
            "progressPct": progress_pct,
            "timestamp": now,
            "title": title.get("title"),
            "contentType": title.get("contentType"),
            "genres": title.get("genres", []),
            "keywords": title.get("keywords", [])[:8],
        }
        self.interactions.upsert(
            f"interaction::{login_id}::{int(now * 1000)}::{uuid.uuid4().hex[:6]}", interaction
        )
        profile = self.get_profile(login_id)
        memory_facts: list[str] = []

        if action in {"play", "progress", "complete"}:
            history = {
                "type": "watch_event",
                "viewerId": login_id,
                "titleId": title_id,
                "progressPct": 100.0 if action == "complete" else float(progress_pct or 5.0),
                "status": "completed" if action == "complete" else "in_progress",
                "lastWatchedAt": now,
                "device": "web-demo",
            }
            self.watch_history.upsert(f"watch::{login_id}::{title_id}", history)

        liked = set(str(x) for x in profile.get("likedTitleIds", []))
        disliked = set(str(x) for x in profile.get("dislikedTitleIds", []))
        watchlist = set(str(x) for x in profile.get("watchlistTitleIds", []))
        genre_affinities = dict(profile.get("genreAffinities") or {})
        theme_affinities = dict(profile.get("themeAffinities") or {})

        if action == "like":
            liked.add(title_id)
            disliked.discard(title_id)
            memory_facts.append(f"Viewer likes {title.get('title')}.")
            delta = 2
        elif action == "dislike":
            disliked.add(title_id)
            liked.discard(title_id)
            watchlist.discard(title_id)
            memory_facts.append(f"Viewer dislikes {title.get('title')}.")
            delta = -3
        else:
            delta = 0

        if delta:
            for genre in title.get("genres", [])[:4]:
                genre_affinities[str(genre)] = int(genre_affinities.get(str(genre), 0)) + delta
            for keyword in title.get("keywords", [])[:6]:
                theme_affinities[str(keyword)] = int(theme_affinities.get(str(keyword), 0)) + delta
        if action == "watchlist":
            watchlist.add(title_id)
        elif action == "remove_watchlist":
            watchlist.discard(title_id)

        self._write_profile_snapshot(login_id, profile, f"before_interaction:{action}")
        profile["likedTitleIds"] = sorted(liked)
        profile["dislikedTitleIds"] = sorted(disliked)
        profile["watchlistTitleIds"] = sorted(watchlist)
        profile["genreAffinities"] = genre_affinities
        profile["themeAffinities"] = theme_affinities
        profile["recommendationVersion"] = int(profile.get("recommendationVersion") or 0) + 1
        profile["updatedAt"] = now
        self.profiles.upsert(f"profile::{login_id}", profile)
        self._write_profile_snapshot(login_id, profile, f"after_interaction:{action}")
        self.invalidate_home_cache(login_id)
        receipt = {
            "type": "streamai_action_receipt",
            "viewerId": login_id,
            "titleId": title_id,
            "title": title.get("title"),
            "action": action,
            "timestamp": now,
            "entitlement": entitlement_decision,
            "status": "completed",
        }
        try:
            self.action_receipts.upsert(
                f"action::{login_id}::{int(now * 1000)}::{uuid.uuid4().hex[:8]}", receipt
            )
        except Exception:
            pass
        return {
            **interaction,
            "profile": profile,
            "title": title,
            "entitlement": entitlement_decision,
            "actionReceipt": receipt,
            "memoryFacts": memory_facts,
            "invalidatedRows": ["continue", "recently-watched", "my-list", "top-picks", "because", "trending"],
        }

    def apply_preferences(self, login_id: str, preferences: list[dict[str, Any]]) -> dict[str, Any]:
        profile = self.get_profile(login_id)
        self._write_profile_snapshot(login_id, profile, "before_preference_update")
        exclusive_content = any(
            bool(pref.get("exclusive"))
            and str(pref.get("sentiment") or "like").lower() == "like"
            and str(pref.get("kind") or "").lower() in {"genre", "theme"}
            for pref in preferences
        )
        if exclusive_content:
            # “I only like …” is an explicit replacement of positive content
            # tastes, not a browse request. Negative/safety preferences remain.
            profile["preferredGenres"] = []
            profile["preferredThemes"] = []
            profile["exclusivePreferenceMode"] = "content"
            profile.pop("exclusivePolicyEnforcementVersion", None)
            profile.pop("exclusivePolicyReconciledAt", None)
        elif any(
            str(pref.get("sentiment") or "like").lower() == "like"
            and str(pref.get("kind") or "").lower() in {"genre", "theme"}
            for pref in preferences
        ):
            profile.pop("exclusivePreferenceMode", None)
        for pref in preferences:
            kind = str(pref.get("kind") or "").lower()
            sentiment = str(pref.get("sentiment") or "like").lower()
            value = pref.get("value")
            if value in (None, ""):
                continue
            if kind == "genre":
                self._apply_pair(profile, "preferredGenres", "dislikedGenres", str(value), sentiment)
            elif kind == "theme":
                self._apply_pair(profile, "preferredThemes", "dislikedThemes", str(value), sentiment)
            elif kind == "person":
                self._apply_pair(profile, "preferredPeople", "dislikedPeople", str(value), sentiment)
            elif kind == "language":
                values = set(profile.get("preferredLanguages", []))
                if sentiment == "like":
                    values.add(str(value))
                else:
                    values.discard(str(value))
                profile["preferredLanguages"] = sorted(values)
            elif kind == "content_type":
                values = set(profile.get("preferredContentTypes", []))
                if sentiment == "like":
                    values.add(str(value))
                else:
                    values.discard(str(value))
                profile["preferredContentTypes"] = sorted(values)
            elif kind == "runtime":
                profile["maxRuntimeMinutes"] = int(value)
            elif kind == "safety" and str(value) == "graphic_violence":
                profile["avoidGraphicViolence"] = sentiment != "remove"
            elif kind == "safety" and str(value) == "adult_content":
                profile["avoidAdultContent"] = sentiment != "remove"
        if exclusive_content:
            self._reconcile_exclusive_profile_state(profile)
        profile["recommendationVersion"] = int(profile.get("recommendationVersion") or 0) + 1
        profile["updatedAt"] = time.time()
        self.profiles.upsert(f"profile::{login_id}", profile)
        self._write_profile_snapshot(login_id, profile, "after_preference_update")
        self.invalidate_home_cache(login_id)
        return profile

    @staticmethod
    def _apply_pair(profile: dict[str, Any], positive_key: str, negative_key: str, value: str, sentiment: str) -> None:
        positive = set(str(x) for x in profile.get(positive_key, []))
        negative = set(str(x) for x in profile.get(negative_key, []))
        if sentiment in {"dislike", "avoid", "negative"}:
            negative.add(value)
            positive.discard(value)
        elif sentiment in {"remove", "neutral"}:
            positive.discard(value)
            negative.discard(value)
        else:
            positive.add(value)
            negative.discard(value)
        profile[positive_key] = sorted(positive)
        profile[negative_key] = sorted(negative)

    def replace_preferences(self, login_id: str, values: dict[str, Any]) -> dict[str, Any]:
        profile = self.get_profile(login_id)
        self._write_profile_snapshot(login_id, profile, "before_profile_replace")
        mappings = {
            "preferred_genres": "preferredGenres",
            "disliked_genres": "dislikedGenres",
            "preferred_themes": "preferredThemes",
            "disliked_themes": "dislikedThemes",
            "preferred_people": "preferredPeople",
            "disliked_people": "dislikedPeople",
            "preferred_languages": "preferredLanguages",
            "preferred_content_types": "preferredContentTypes",
        }
        for source, target in mappings.items():
            profile[target] = self._dedupe(values.get(source, []))
        profile["maxRuntimeMinutes"] = values.get("max_runtime_minutes")
        profile["avoidGraphicViolence"] = bool(values.get("avoid_graphic_violence"))
        profile["avoidAdultContent"] = bool(values.get("avoid_adult_content"))
        profile["recommendationVersion"] = int(profile.get("recommendationVersion") or 0) + 1
        profile["updatedAt"] = time.time()
        self.profiles.upsert(f"profile::{login_id}", profile)
        self._write_profile_snapshot(login_id, profile, "after_profile_replace")
        self.invalidate_home_cache(login_id)
        return profile

    def profile_view(self, login_id: str) -> dict[str, Any]:
        signals = self.viewer_signals(login_id)
        profile = signals["profile"]
        liked = self._fetch_title_ids(sorted(signals["liked"]))
        disliked = self._fetch_title_ids(sorted(signals["disliked"]))
        watchlist = self._fetch_title_ids(sorted(signals["watchlist"]))
        history = self.list_watch_history(login_id=login_id, limit=40)
        entitlement = self.ensure_entitlement(login_id)
        return {
            "profile": profile,
            "entitlement": entitlement,
            "likedTitles": liked,
            "dislikedTitles": disliked,
            "watchlistTitles": watchlist,
            "watchHistory": history,
            "summary": {
                "liked": len(liked),
                "disliked": len(disliked),
                "watchlist": len(watchlist),
                "watched": len(history),
            },
        }

    def profile_memory_blocks(self, login_id: str) -> list[dict[str, Any]]:
        profile = self.get_profile(login_id)
        facts: list[str] = []
        for value in profile.get("preferredGenres", []):
            facts.append(f"Viewer prefers {value} content.")
        for value in profile.get("dislikedGenres", []):
            facts.append(f"Viewer dislikes {value} content.")
        for value in profile.get("preferredThemes", []):
            facts.append(f"Viewer prefers {value}-themed content.")
        for value in profile.get("dislikedThemes", []):
            facts.append(f"Viewer dislikes {value}-themed content.")
        for value in profile.get("preferredPeople", []):
            facts.append(f"Viewer likes titles involving {value}.")
        for value in profile.get("dislikedPeople", []):
            facts.append(f"Viewer prefers to avoid titles involving {value}.")
        for value in profile.get("preferredContentTypes", []):
            facts.append(f"Viewer prefers {value} content types.")
        for value in profile.get("preferredLanguages", []):
            facts.append(f"Viewer prefers {value}-language content.")
        if profile.get("maxRuntimeMinutes"):
            facts.append(f"Viewer generally prefers titles under {profile['maxRuntimeMinutes']} minutes.")
        if profile.get("avoidGraphicViolence"):
            facts.append("Viewer prefers to avoid graphic violence.")
        if profile.get("avoidAdultContent"):
            facts.append("Viewer prefers to avoid sexual and adult content.")
        blocks = []
        for fact in self._dedupe(facts):
            blocks.append(
                {
                    "block_id": None,
                    "session_id": None,
                    "fact": fact,
                    "user_content": None,
                    "assistant_content": None,
                    "summary": None,
                    "status": "operational_profile_only",
                    "stored_in_agent_memory": False,
                    "rel_score": None,
                    "annotations": {
                        "memory_scope": "long_term",
                        "memory_type": "viewer_preference",
                        "source": "structured_viewer_profile",
                    },
                }
            )
        return blocks

    # ------------------------------------------------------------------
    # Agent Memory value telemetry
    # ------------------------------------------------------------------
    def _metrics_runtime(self) -> dict[str, Any]:
        config = getattr(self, "settings", None)
        provider = str(getattr(config, "chat_provider", "unknown"))
        label = {"capella_model_service": "Capella Model Service", "ollama": "Local Ollama",
                 "openai_compatible": "OpenAI-compatible service", "disabled": "Chat model disabled"}.get(provider, "Configured model service")
        model = str(getattr(config, "chat_model", "") or "")
        return {"provider": provider, "providerLabel": label, "chatModel": model,
                "description": f"{label} · {model}" if model else label,
                "embeddingProvider": getattr(config, "embedding_provider", None),
                "embeddingModel": getattr(config, "embedding_model", None),
                "scope": "Inference HTTP attempts through the deployment usage gateway; includes Agent Memory, ingestion, setup and retries.",
                "usageGatewayConfigured": bool(usage_reporter.url())}

    def get_ai_metrics(self, login_id: str) -> dict[str, Any]:
        # Older per-viewer aggregates are retained in Couchbase for audit only.
        # Never mix their estimated savings or incomplete counts into new usage.
        return self._finalise_ai_metrics({"viewerId": login_id})

    def reset_ai_metrics(self, login_id: str) -> dict[str, Any]:
        usage_reporter.new_window()
        return self.get_ai_metrics(login_id)

    def record_ai_metric(self, login_id: str, event: dict[str, Any]) -> dict[str, Any]:
        details = event.get("details") or {}
        recorded = usage_reporter.record(login_id, {
            "category": event.get("category", "interaction"),
            "label": event.get("label") or event.get("responseMode"),
            "responseMs": event.get("responseMs"),
            "firstChunkMs": event.get("firstChunkMs"),
            "plannerRequested": bool(event.get("plannerLlmCalls")),
            "plannerDecision": bool(details.get("planSource")),
            "planCacheHit": details.get("planCacheHitType") in {"exact", "semantic"},
            "contextFactCount": int(details.get("longTermFactsSupplied") or 0),
            "success": event.get("category") != "chat_failed",
        })
        result = self.get_ai_metrics(login_id)
        result["observationRecorded"] = recorded
        return result

    def _finalise_ai_metrics(self, document: dict[str, Any]) -> dict[str, Any]:
        viewer = str(document.get("viewerId") or "")
        measured = usage_reporter.snapshot(viewer)
        # Managed cloud inference does not traverse the local usage gateway.
        # This is a scoped gateway comparison; external work is disclosed separately.
        measured["comparisonScope"] = "all_routed_model_work"
        measured["overallSavingsMeasured"] = False
        measured["unmeteredServices"] = []
        configuration = getattr(self, "settings", None)
        if getattr(configuration, "ai_functions_enabled", False):
            measured["unmeteredServices"].append("AI Functions")
        if getattr(configuration, "data_processing_mode", "") == "capella_workflow":
            measured["unmeteredServices"].append("managed Data Processing")
        return {"schemaVersion": 2, "viewerId": viewer, "runtime": self._metrics_runtime(),
                "measured": measured, "telemetryAvailable": bool(measured.get("available")),
                "legacyMetricsKey": f"ai-metrics::{viewer}",
                "legacyMetricsNotice": "Earlier aggregates are retained for audit only. They omitted background work and included hypothetical savings. They are not used in this dashboard or updated by this revision."}

    # ------------------------------------------------------------------
    # Row helpers
    # ------------------------------------------------------------------
    def _continue_watching(self, login_id: str) -> list[dict[str, Any]]:
        statement = (
            f"SELECT h.titleId, h.progressPct, h.lastWatchedAt FROM {self.history_keyspace} AS h "
            "WHERE h.viewerId=$viewerId AND h.progressPct > 0 AND h.progressPct < 85 "
            "ORDER BY h.lastWatchedAt DESC LIMIT 12"
        )
        rows = list(
            self.cluster.query(
                statement,
                QueryOptions(named_parameters={"viewerId": login_id}, readonly=True, scan_consistency=QueryScanConsistency.REQUEST_PLUS),
            ).rows()
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                doc = self.title(str(row["titleId"]))
            except Exception:
                continue
            doc["progressPct"] = float(row.get("progressPct") or 0)
            doc["recommendationSource"] = "continue_watching"
            doc["viewerState"] = "watching"
            items.append(doc)
        return self._apply_entitlement_policy(items, login_id, remove_denied=True)

    def _because_you_watched(self, login_id: str) -> dict[str, Any]:
        # Use the most recent real watch event, not only completed titles. This
        # keeps the row useful during a live demo where the presenter may have
        # started a title but not artificially advanced it to 85 percent.
        rows = list(
            self.cluster.query(
                f"SELECT h.titleId, h.progressPct, h.status FROM {self.history_keyspace} AS h "
                "WHERE h.viewerId=$viewerId AND h.progressPct > 0 "
                "ORDER BY h.lastWatchedAt DESC LIMIT 1",
                QueryOptions(
                    named_parameters={"viewerId": login_id},
                    readonly=True,
                    scan_consistency=QueryScanConsistency.REQUEST_PLUS,
                ),
            ).rows()
        )
        if not rows:
            return {"title": "Because You Watched", "items": []}
        progress = float(rows[0].get("progressPct") or 0)
        seed = self.title(str(rows[0]["titleId"]), include_internal=True)
        row_title = (
            f"Because You Watched {seed.get('title')}"
            if progress >= 85
            else f"Because You Started {seed.get('title')}"
        )
        vector = seed.get("embedding")
        if not vector:
            return {"title": row_title, "items": []}
        payload = {"size": 50, "knn": [{"field": "embedding", "vector": vector, "k": 50}]}
        try:
            response = self._search_client.post(f"{self.search_index_path}/query", json=payload)
            response.raise_for_status()
            hits = list(response.json().get("hits", []))
            docs = [doc for doc in self._fetch_hits(hits) if doc.get("id") != seed.get("id")]
            self._apply_search_scores(docs, hits, source="vector_similarity", intent=self._analyse_query(str(seed.get("title") or "")))
            docs = self._exclude_by_purpose(docs, login_id, "recommendation")
            return {"title": row_title, "items": docs[:16]}
        except Exception:
            genres = seed.get("genres", [])[:1]
            if not genres:
                return {"title": row_title, "items": []}
            docs = self._fallback_query(
                query=str(genres[0]),
                content_type=seed.get("contentType"),
                limit=30,
                intent=self._analyse_query(str(genres[0])),
            )
            docs = self._exclude_by_purpose(docs, login_id, "recommendation")
            return {"title": row_title, "items": docs[:16]}

    def likes_dislikes_summary(self, login_id: str) -> dict[str, Any]:
        profile = self.get_profile(login_id)
        liked = self._fetch_title_ids([str(value) for value in profile.get("likedTitleIds", [])])
        disliked = self._fetch_title_ids([str(value) for value in profile.get("dislikedTitleIds", [])])
        return {"profile": profile, "likedTitles": liked, "dislikedTitles": disliked}

    def _query_titles(self, statement: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result = self.cluster.query(statement, QueryOptions(named_parameters=parameters or {}, readonly=True))
        rows = [dict(row) for row in result.rows()]
        for row in rows:
            self._decorate(row)
        return rows

    @classmethod
    def normalise_plan_query(cls, query: str) -> str:
        value = cls.normalise_natural_query(query)
        value = re.sub(r"[^a-z0-9]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    def _plan_cache_key(self, query: str) -> str:
        material = ":".join(
            [
                str(getattr(self.settings, "app_version", "1.0.0")),
                self.settings.assistant_planner_version,
                self.settings.assistant_tool_schema_version,
                self.normalise_plan_query(query),
            ]
        )
        return f"plan::{hashlib.sha256(material.encode('utf-8')).hexdigest()}"

    def get_cached_plan_exact(self, query: str) -> dict[str, Any] | None:
        if not self.settings.plan_cache_enabled:
            return None
        key = self._plan_cache_key(query)
        try:
            document = dict(self.plan_cache.get(key).content_as[dict])
        except DocumentNotFoundException:
            return None
        now = time.time()
        if (
            str(document.get("plannerVersion")) != self.settings.assistant_planner_version
            or str(document.get("toolSchemaVersion"))
            != self.settings.assistant_tool_schema_version
            or float(document.get("expiresAt") or 0) <= now
        ):
            return None
        document["hitCount"] = int(document.get("hitCount") or 0) + 1
        document["lastUsedAt"] = now
        self.plan_cache.upsert(key, document)
        document["cacheDocumentId"] = key
        return document

    def mark_plan_cache_hit(self, document_id: str) -> None:
        try:
            document = dict(
                self.plan_cache.get(document_id).content_as[dict]
            )
            document["hitCount"] = int(document.get("hitCount") or 0) + 1
            document["lastUsedAt"] = time.time()
            self.plan_cache.upsert(document_id, document)
        except DocumentNotFoundException:
            return

    def find_cached_plans_semantic(
        self, query: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        if not (
            self.settings.plan_cache_enabled
            and self.settings.plan_cache_semantic_enabled
        ):
            return []
        vector, embedding_trace = self.embed_with_trace(query)
        size = min(
            max(int(limit or self.settings.plan_cache_candidate_limit), 1), 20
        )
        payload = {
            "size": size,
            "from": 0,
            "fields": [
                "normalisedQuery",
                "appVersion",
                "plannerVersion",
                "toolSchemaVersion",
                "embeddingModel",
                "expiresAt",
                "semanticEligible",
            ],
            "knn": [
                {
                    "field": "queryEmbedding",
                    "vector": vector,
                    "k": max(size * 4, 20),
                }
            ],
        }
        response = self._plan_search_client.post(
            self.plan_cache_index_path,
            json=payload,
            timeout=self.settings.search_query_timeout_seconds,
        )
        response.raise_for_status()
        now = time.time()
        candidates: list[dict[str, Any]] = []
        for hit in list(response.json().get("hits") or []):
            document_id = str(hit.get("id") or "")
            if not document_id:
                continue
            try:
                document = dict(
                    self.plan_cache.get(document_id).content_as[dict]
                )
            except DocumentNotFoundException:
                continue
            if (
                not bool(document.get("semanticEligible"))
                or str(document.get("appVersion"))
                != str(getattr(self.settings, "app_version", "1.0.0"))
                or str(document.get("plannerVersion"))
                != self.settings.assistant_planner_version
                or str(document.get("toolSchemaVersion"))
                != self.settings.assistant_tool_schema_version
                or str(document.get("embeddingModel"))
                != self.settings.embedding_model
                or float(document.get("expiresAt") or 0) <= now
            ):
                continue
            document["cacheDocumentId"] = document_id
            document["similarity"] = float(hit.get("score") or 0)
            document["embeddingTrace"] = embedding_trace
            candidates.append(document)
        return candidates

    def cache_assistant_plan(
        self,
        *,
        query: str,
        plan: dict[str, Any],
        guard: dict[str, Any],
        source: str,
        semantic_eligible: bool,
    ) -> dict[str, Any]:
        if not self.settings.plan_cache_enabled:
            return {"stored": False, "reason": "disabled"}
        key = self._plan_cache_key(query)
        now = time.time()
        embedding: list[float] | None = None
        embedding_trace: dict[str, Any] = {}
        if semantic_eligible and self.settings.plan_cache_semantic_enabled:
            try:
                embedding, embedding_trace = self.embed_with_trace(query)
            except Exception as exc:
                embedding_trace = {
                    "embeddingError": f"{type(exc).__name__}: {exc}"
                }
                semantic_eligible = False
        document = {
            "type": "assistant_plan_cache",
            "normalisedQuery": self.normalise_plan_query(query),
            "queryEmbedding": embedding,
            "planTemplate": dict(plan),
            "guard": dict(guard),
            "source": source,
            "semanticEligible": semantic_eligible,
            "appVersion": str(
                getattr(self.settings, "app_version", "1.0.0")
            ),
            "plannerVersion": self.settings.assistant_planner_version,
            "toolSchemaVersion": self.settings.assistant_tool_schema_version,
            "embeddingModel": self.settings.embedding_model,
            "embeddingDimensions": len(embedding or []),
            "createdAt": now,
            "lastUsedAt": now,
            "hitCount": 0,
            "expiresAt": now + self.settings.plan_cache_ttl_seconds,
        }
        self.plan_cache.upsert(key, document)
        return {
            "stored": True,
            "cacheDocumentId": key,
            "semanticEligible": semantic_eligible,
            "embeddingTrace": embedding_trace,
        }

    def plan_cache_status(self) -> dict[str, Any]:
        keyspace = self._keyspace(
            self.settings.recommendations_scope,
            self.settings.plan_cache_collection,
        )
        rows = list(
            self.cluster.query(
                f"SELECT COUNT(1) AS total, "
                "SUM(CASE WHEN p.semanticEligible=true THEN 1 ELSE 0 END) AS semantic, "
                "SUM(IFMISSINGORNULL(p.hitCount, 0)) AS hits "
                f"FROM {keyspace} AS p WHERE p.type='assistant_plan_cache'",
                QueryOptions(readonly=True),
            ).rows()
        )
        row = rows[0] if rows else {}
        response = self._plan_search_client.get(
            self.plan_cache_index_path,
            timeout=self.settings.search_query_timeout_seconds,
        )
        return {
            "enabled": self.settings.plan_cache_enabled,
            "semanticEnabled": self.settings.plan_cache_semantic_enabled,
            "collection": (
                f"{self.settings.recommendations_scope}."
                f"{self.settings.plan_cache_collection}"
            ),
            "searchIndex": self.settings.plan_cache_search_index,
            "searchHealthy": response.status_code == 200,
            "plans": int(row.get("total") or 0),
            "semanticPlans": int(row.get("semantic") or 0),
            "cacheHits": int(row.get("hits") or 0),
            "appVersion": str(
                getattr(self.settings, "app_version", "1.0.0")
            ),
            "plannerVersion": self.settings.assistant_planner_version,
            "toolSchemaVersion": self.settings.assistant_tool_schema_version,
            "embeddingModel": self.settings.embedding_model,
            "semanticThreshold": self.settings.plan_cache_semantic_threshold,
            "ttlSeconds": self.settings.plan_cache_ttl_seconds,
        }

    @staticmethod
    def _resolved_plan_filters(filters: dict[str, Any]) -> dict[str, Any]:
        resolved = dict(filters or {})
        current_year = time.gmtime().tm_year
        period = str(resolved.get("releasePeriod") or "")
        if period == "LAST_YEAR":
            resolved["releaseYear"] = current_year - 1
            resolved["releaseYearMin"] = current_year - 1
            resolved["releaseYearMax"] = current_year - 1
        elif period == "THIS_YEAR":
            resolved["releaseYear"] = current_year
            resolved["releaseYearMin"] = current_year
            resolved["releaseYearMax"] = current_year
        elif period == "RECENT":
            resolved["releaseYear"] = None
            resolved["releaseYearMin"] = current_year - 1
            resolved["releaseYearMax"] = current_year
        return resolved

    def _matches_plan_filters(
        self, doc: dict[str, Any], filters: dict[str, Any]
    ) -> bool:
        filters = self._resolved_plan_filters(filters)
        content_type = filters.get("contentType")
        if content_type and str(doc.get("contentType") or "") != content_type:
            return False
        title_contains = str(filters.get("titleContains") or "").strip().lower()
        if title_contains and title_contains not in str(doc.get("title") or "").lower() and title_contains not in str(
            doc.get("originalTitle") or ""
        ).lower():
            return False
        year = doc.get("releaseYear")
        exact_year = filters.get("releaseYear")
        if exact_year is not None and int(year or 0) != int(exact_year):
            return False
        year_min = filters.get("releaseYearMin")
        if year_min is not None and int(year or 0) < int(year_min):
            return False
        year_max = filters.get("releaseYearMax")
        if year_max is not None and int(year or 0) > int(year_max):
            return False
        doc_genres = self._normalised_values(doc.get("genres", []))
        for genre in filters.get("genres") or []:
            if self._normalise_value(genre) not in doc_genres:
                return False
        people = self._normalised_values(
            [*doc.get("castNames", []), *doc.get("directorNames", [])]
        )
        if filters.get("people") and not people.intersection(
            self._normalised_values(filters.get("people") or [])
        ):
            return False
        languages = self._normalised_values(
            [doc.get("originalLanguage"), *doc.get("spokenLanguages", [])]
        )
        if filters.get("languages") and not languages.intersection(
            self._normalised_values(filters.get("languages") or [])
        ):
            return False
        countries = {str(value).upper() for value in doc.get("originCountries", [])}
        if filters.get("countries") and not countries.intersection(
            {str(value).upper() for value in filters.get("countries") or []}
        ):
            return False
        rating_min = filters.get("ratingMin")
        if rating_min is not None and float(doc.get("voteAverage") or 0) < float(
            rating_min
        ):
            return False
        runtime_max = filters.get("runtimeMax")
        runtime = doc.get("runtimeMinutes") or doc.get("episodeRuntimeMinutes")
        if runtime_max is not None and (
            runtime is None or float(runtime) > float(runtime_max)
        ):
            return False
        return True

    def query_catalogue_plan(
        self,
        *,
        login_id: str,
        plan: dict[str, Any],
        search_mode: str = "hybrid",
    ) -> dict[str, Any]:
        """Execute an allowlisted planner result against fresh catalogue data."""
        started = time.perf_counter()
        filters = self._resolved_plan_filters(dict(plan.get("filters") or {}))
        content_type = plan.get("contentType")
        if content_type:
            filters["contentType"] = content_type
        limit = min(max(int(plan.get("limit") or 12), 1), 50)
        semantic_query = str(plan.get("semanticQuery") or "").strip()
        personalised = bool(plan.get("personalise"))
        if personalised:
            # A generic profile recommendation is already grounded by exact
            # genres, people, themes and exclusions. Do not pay for a query
            # embedding unless the user also supplied a free-form semantic
            # concept such as "atmospheric" or "slow-burn".
            recommendation_mode = search_mode if semantic_query else "fts"
            recommendation = self.recommend_for_profile(
                login_id=login_id,
                mode=recommendation_mode,
                limit=limit,
                content_type=content_type,
                plan_filters=filters,
            )
            trace = dict(recommendation.get("trace") or {})
            trace.update(
                {
                    "queryMode": "governed_plan_personalised",
                    "profileRetrievalMode": recommendation_mode,
                    "executedPlan": plan,
                    "resolvedFilters": filters,
                }
            )
            return {
                "results": list(recommendation.get("results") or []),
                "trace": trace,
                "profile": dict(recommendation.get("profile") or {}),
            }
        if semantic_query and not filters.get("titleContains"):
            result = self.search(
                query=semantic_query,
                mode=search_mode,
                login_id=login_id,
                content_type=content_type,
                limit=limit,
                purpose="search",
                plan_filters=filters,
            )
            trace = dict(result.get("trace") or {})
            trace.update(
                {
                    "queryMode": "governed_plan_search",
                    "executedPlan": plan,
                    "resolvedFilters": filters,
                }
            )
            return {"results": list(result.get("results") or []), "trace": trace}

        params: dict[str, Any] = {"limit": limit}
        conditions: list[str] = []
        if content_type:
            conditions.append("t.contentType=$contentType")
            params["contentType"] = content_type
        if filters.get("titleContains"):
            conditions.append(
                "(CONTAINS(LOWER(t.title), LOWER($titleContains)) OR "
                "CONTAINS(LOWER(IFMISSINGORNULL(t.originalTitle, '')), LOWER($titleContains)))"
            )
            params["titleContains"] = str(filters["titleContains"])
        if filters.get("titleEquals"):
            conditions.append(
                "(LOWER(t.title)=LOWER($titleEquals) OR "
                "LOWER(IFMISSINGORNULL(t.originalTitle, ''))=LOWER($titleEquals))"
            )
            params["titleEquals"] = str(filters["titleEquals"])
        if filters.get("releaseYear") is not None:
            conditions.append("t.releaseYear=$releaseYear")
            params["releaseYear"] = int(filters["releaseYear"])
        else:
            if filters.get("releaseYearMin") is not None:
                conditions.append("t.releaseYear >= $releaseYearMin")
                params["releaseYearMin"] = int(filters["releaseYearMin"])
            if filters.get("releaseYearMax") is not None:
                conditions.append("t.releaseYear <= $releaseYearMax")
                params["releaseYearMax"] = int(filters["releaseYearMax"])
        for index, genre in enumerate(filters.get("genres") or []):
            key = f"planGenre{index}"
            conditions.append(
                f"ANY g IN IFMISSINGORNULL(t.genres, []) SATISFIES LOWER(g)=LOWER(${key}) END"
            )
            params[key] = str(genre)
        if filters.get("people"):
            params["people"] = [
                str(value).lower() for value in filters.get("people") or []
            ]
            conditions.append(
                "(ANY p IN IFMISSINGORNULL(t.castNames, []) SATISFIES LOWER(p) IN $people END "
                "OR ANY p IN IFMISSINGORNULL(t.directorNames, []) SATISFIES LOWER(p) IN $people END)"
            )
        if filters.get("languages"):
            params["languages"] = [
                str(value).lower() for value in filters.get("languages") or []
            ]
            conditions.append(
                "(LOWER(t.originalLanguage) IN $languages OR "
                "ANY l IN IFMISSINGORNULL(t.spokenLanguages, []) SATISFIES LOWER(l) IN $languages END)"
            )
        if filters.get("countries"):
            params["countries"] = [
                str(value).upper() for value in filters.get("countries") or []
            ]
            conditions.append(
                "ANY c IN IFMISSINGORNULL(t.originCountries, []) SATISFIES UPPER(c) IN $countries END"
            )
        if filters.get("ratingMin") is not None:
            conditions.append("t.voteAverage >= $ratingMin")
            params["ratingMin"] = float(filters["ratingMin"])
        if filters.get("runtimeMax") is not None:
            conditions.append(
                "COALESCE(t.runtimeMinutes, t.episodeRuntimeMinutes) <= $runtimeMax"
            )
            params["runtimeMax"] = int(filters["runtimeMax"])
        where = " AND ".join(conditions) if conditions else "TRUE"
        order_map = {
            "rating_desc": (
                "t.voteAverage DESC, t.voteCount DESC, t.popularity DESC"
            ),
            "release_year_desc": (
                "t.releaseYear DESC, t.popularity DESC, t.voteAverage DESC"
            ),
            "popularity_desc": "t.popularity DESC, t.voteAverage DESC",
            "relevance": "t.popularity DESC, t.voteAverage DESC",
        }
        order = order_map.get(
            str(plan.get("sort") or "relevance"),
            order_map["relevance"],
        )
        statement = (
            f"SELECT t.* FROM {self.titles_keyspace} AS t WHERE {where} "
            f"ORDER BY {order} LIMIT $limit"
        )
        docs = self._query_titles(statement, params)
        docs = [
            doc for doc in docs if self._matches_plan_filters(doc, filters)
        ]
        try:
            viewer_context = self.viewer_signals(login_id)
        except AttributeError:
            if hasattr(self, "profiles"):
                raise
            viewer_context = {
                "profile": {},
                "liked": set(),
                "disliked": set(),
                "watchlist": set(),
                "watched": set(),
                "completed": set(),
                "topPickExclusions": set(),
                "heroExclusions": set(),
            }
        disliked_genres = self._normalised_values(
            dict(viewer_context.get("profile") or {}).get(
                "dislikedGenres", []
            )
        )
        explicitly_requested_dislikes = [
            str(genre)
            for genre in filters.get("genres") or []
            if self._normalise_value(genre) in disliked_genres
        ]
        docs = self._exclude_with_signals(
            docs,
            login_id,
            "search",
            ignored_disliked_genres=explicitly_requested_dislikes,
            viewer_signals_context=viewer_context,
        )[:limit]
        self._add_rank_scores(docs, source="governed_plan_sqlpp")
        for doc in docs:
            doc["recommendationSource"] = "governed_catalogue_plan"
            doc["matchExplanation"] = {
                "summary": "Matched the assistant's validated catalogue plan",
                "matchedFields": [
                    {
                        "field": "plan",
                        "value": plan.get("planSignature"),
                        "matchType": "validated_query_plan",
                    }
                ],
            }
        trace = {
            "mode": "governed_plan_sqlpp",
            "effectiveMode": "governed_plan_sqlpp",
            "queryMode": "validated_sqlpp_catalogue_plan",
            "rankingStrategy": str(plan.get("sort") or "relevance"),
            "executedPlan": plan,
            "resolvedFilters": filters,
            "returnedCount": len(docs),
            "selectedTitleIds": [doc.get("id") for doc in docs],
            "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
            "services": ["Query", "Index", "Data/KV"],
        }
        self._write_trace(login_id, "catalogue_plan_execution", trace)
        return {"results": docs, "trace": trace}

    @staticmethod
    def _structured_only_fallback_intent(intent: dict[str, Any]) -> dict[str, Any]:
        """Preserve authoritative filters while dropping optional descriptive terms."""
        broadened = dict(intent)
        broadened["terms"] = []
        broadened["expandedTerms"] = []
        broadened["explicitTerms"] = []
        broadened["cleaned"] = ""
        return broadened

    def _fallback_query(
        self,
        *,
        query: str,
        content_type: str | None,
        limit: int,
        intent: dict[str, Any],
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": min(max(limit, 1), 100)}
        conditions: list[str] = []
        country = intent.get("country")
        if country:
            params["countries"] = country.get("countries", [])
            params["languages"] = country.get("languages", [])
            conditions.append(
                "(ANY c IN IFMISSINGORNULL(t.originCountries, []) SATISFIES c IN $countries END "
                "OR t.originalLanguage IN $languages)"
            )
        for index, genre in enumerate(intent.get("genres", [])):
            key = f"genre{index}"
            params[key] = genre.lower()
            conditions.append(
                f"ANY g IN IFMISSINGORNULL(t.genres, []) SATISFIES LOWER(g)=${key} END"
            )
        terms = intent.get("explicitTerms") or intent.get("expandedTerms") or intent.get("terms") or []
        terms = [str(term).lower() for term in terms if term]
        if terms and not (country and not intent.get("expandedTerms") and not intent.get("cleaned")):
            params["patterns"] = [f"%{term}%" for term in terms]
            conditions.append("ANY p IN $patterns SATISFIES LOWER(t.searchText) LIKE p END")
        if content_type:
            conditions.append("t.contentType=$contentType")
            params["contentType"] = content_type
        plan_filters = self._resolved_plan_filters(
            dict(intent.get("planFilters") or {})
        )
        for index, genre in enumerate(plan_filters.get("genres") or []):
            key = f"planFilterGenre{index}"
            conditions.append(
                f"ANY g IN IFMISSINGORNULL(t.genres, []) SATISFIES LOWER(g)=LOWER(${key}) END"
            )
            params[key] = str(genre)
        if plan_filters.get("countries"):
            params["planCountries"] = [
                str(value).upper()
                for value in plan_filters.get("countries") or []
            ]
            conditions.append(
                "ANY c IN IFMISSINGORNULL(t.originCountries, []) "
                "SATISFIES UPPER(c) IN $planCountries END"
            )
        if plan_filters.get("languages"):
            params["planLanguages"] = [
                str(value).lower()
                for value in plan_filters.get("languages") or []
            ]
            conditions.append(
                "(LOWER(t.originalLanguage) IN $planLanguages OR "
                "ANY l IN IFMISSINGORNULL(t.spokenLanguages, []) "
                "SATISFIES LOWER(l) IN $planLanguages END)"
            )
        if plan_filters.get("people"):
            params["planPeople"] = [
                str(value).lower() for value in plan_filters.get("people") or []
            ]
            conditions.append(
                "(ANY p IN IFMISSINGORNULL(t.castNames, []) "
                "SATISFIES LOWER(p) IN $planPeople END OR "
                "ANY p IN IFMISSINGORNULL(t.directorNames, []) "
                "SATISFIES LOWER(p) IN $planPeople END)"
            )
        if plan_filters.get("releaseYear") is not None:
            conditions.append("t.releaseYear=$planReleaseYear")
            params["planReleaseYear"] = int(plan_filters["releaseYear"])
        else:
            if plan_filters.get("releaseYearMin") is not None:
                conditions.append("t.releaseYear >= $planReleaseYearMin")
                params["planReleaseYearMin"] = int(
                    plan_filters["releaseYearMin"]
                )
            if plan_filters.get("releaseYearMax") is not None:
                conditions.append("t.releaseYear <= $planReleaseYearMax")
                params["planReleaseYearMax"] = int(
                    plan_filters["releaseYearMax"]
                )
        if plan_filters.get("ratingMin") is not None:
            conditions.append("t.voteAverage >= $planRatingMin")
            params["planRatingMin"] = float(plan_filters["ratingMin"])
        if plan_filters.get("runtimeMax") is not None:
            conditions.append(
                "COALESCE(t.runtimeMinutes, t.episodeRuntimeMinutes) <= $planRuntimeMax"
            )
            params["planRuntimeMax"] = int(plan_filters["runtimeMax"])
        where = " AND ".join(conditions) if conditions else "TRUE"
        statement = (
            f"SELECT t.* FROM {self.titles_keyspace} AS t WHERE {where} "
            "ORDER BY t.popularity DESC, t.voteAverage DESC LIMIT $limit"
        )
        docs = self._query_titles(statement, params)
        for doc in docs:
            doc["matchExplanation"] = self._match_explanation(doc, None, intent)
        return docs

    def _fetch_hits(self, hits: list[dict[str, Any]], content_type: str | None = None) -> list[dict[str, Any]]:
        title_ids = [
            str(hit.get("id"))
            for hit in hits
            if hit.get("id")
        ]
        return [
            doc
            for doc in self._fetch_title_ids(title_ids)
            if not content_type or doc.get("contentType") == content_type
        ]

    def _fetch_title_ids(self, title_ids: list[str]) -> list[dict[str, Any]]:
        ordered_ids = list(dict.fromkeys(str(value) for value in title_ids if value))[:100]
        if not ordered_ids:
            return []
        try:
            multi_result = self.titles.get_multi(ordered_ids)
            result_map = getattr(multi_result, "results", multi_result)
            docs: list[dict[str, Any]] = []
            for title_id in ordered_ids:
                item = result_map.get(title_id) if hasattr(result_map, "get") else None
                if item is None:
                    continue
                doc = dict(item.content_as[dict])
                self._decorate(doc)
                docs.append(doc)
            return docs
        except Exception:
            # Test doubles and older SDKs may not expose multi-get.
            pass

        docs: list[dict[str, Any]] = []
        for title_id in ordered_ids:
            try:
                docs.append(self.title(title_id))
            except Exception:
                continue
        return docs

    def _decorate(self, doc: dict[str, Any], *, include_internal: bool = False) -> None:
        poster = doc.get("posterPath")
        backdrop = doc.get("backdropPath")
        doc["posterUrl"] = f"{self.settings.tmdb_image_base}{poster}" if poster else None
        doc["backdropUrl"] = f"{self.settings.tmdb_backdrop_base}{backdrop}" if backdrop else None
        if not include_internal:
            # Embeddings and source text are useful inside Couchbase but make API
            # responses enormous. They are never sent to the browser.
            doc.pop("embedding", None)
            doc.pop("embeddingText", None)
            doc.pop("searchText", None)

    def _apply_search_scores(
        self,
        docs: list[dict[str, Any]],
        hits: list[dict[str, Any]],
        *,
        source: str,
        intent: dict[str, Any],
        requested_mode: str | None = None,
        effective_mode: str | None = None,
    ) -> None:
        hit_by_id = {str(hit.get("id")): hit for hit in hits}
        actual_mode = effective_mode or source
        explanation_mode = (
            "hybrid" if "hybrid" in actual_mode
            else "vector" if "vector" in actual_mode
            else "fts"
        )
        for doc in docs:
            hit = hit_by_id.get(str(doc.get("id")))
            raw = float((hit or {}).get("score") or 0.0)
            doc["searchScore"] = raw
            doc["recommendationSource"] = actual_mode
            doc["matchExplanation"] = self._match_explanation(
                doc,
                hit,
                intent,
                effective_mode=explanation_mode,
                requested_mode=requested_mode or actual_mode,
            )
            doc["matchExplanation"]["effectiveMode"] = actual_mode
            doc["matchExplanation"]["rawSearchScore"] = raw
        self._finalise_display_scores(
            docs,
            requested_mode=requested_mode or actual_mode,
            effective_mode=actual_mode,
            ranking_strategy="couchbase_search_score",
        )

    def _add_rank_scores(self, docs: list[dict[str, Any]], *, source: str) -> None:
        for doc in docs:
            self._decorate(doc)
            doc["recommendationSource"] = source
            doc.setdefault(
                "matchExplanation",
                {
                    "summary": "Catalogue popularity and quality ranking",
                    "matchedFields": [
                        {"field": "popularity", "value": doc.get("popularity"), "matchType": "ranking"},
                        {"field": "voteAverage", "value": doc.get("voteAverage"), "matchType": "ranking"},
                    ],
                    "evidenceType": "ranking",
                    "effectiveMode": source,
                    "requestedMode": source,
                },
            )
        self._finalise_display_scores(
            docs,
            requested_mode=source,
            effective_mode=source,
            ranking_strategy="catalogue_popularity_quality",
        )

    def _finalise_display_scores(
        self,
        docs: list[dict[str, Any]],
        *,
        requested_mode: str,
        effective_mode: str,
        ranking_strategy: str,
        mode_reason: str | None = None,
    ) -> None:
        """Assign a monotonic presentation score after all final ranking steps.

        Couchbase Search scores are retained in ``searchScore`` for inspection.
        ``matchPct`` is deliberately a relative score within the displayed result
        set, not a calibrated probability. Assigning it after sorting, viewer
        exclusions and limiting prevents a lower badge appearing before a higher
        one in the carousel.
        """
        if not docs:
            return
        count = len(docs)
        high, low = 98, 68
        step = (high - low) / max(count - 1, 1)
        top_raw_score = max((float(doc.get("searchScore") or 0.0) for doc in docs), default=0.0)
        for rank, doc in enumerate(docs, start=1):
            pct = high if count == 1 else round(high - (rank - 1) * step)
            doc["matchPct"] = int(max(low, min(high, pct)))
            doc["rankPosition"] = rank
            doc["requestedSearchMode"] = requested_mode
            doc["effectiveSearchMode"] = effective_mode
            doc["rankingStrategy"] = ranking_strategy
            doc["matchScoreMeaning"] = "Relative final rank score; not a probability."
            if mode_reason:
                doc["searchModeReason"] = mode_reason
            explanation = dict(doc.get("matchExplanation") or {})
            explanation["requestedMode"] = requested_mode
            explanation["effectiveMode"] = effective_mode
            explanation["rankingStrategy"] = ranking_strategy
            explanation["scoreMeaning"] = doc["matchScoreMeaning"]
            raw_score = float(doc.get("searchScore") or 0.0)
            if top_raw_score > 0:
                explanation["rawScoreRelativeToTopPct"] = round(raw_score / top_raw_score * 100, 1)
            explanation["rankPosition"] = rank
            matched = list(explanation.get("matchedFields") or [])
            lexical = next((item for item in matched if str(item.get("matchType") or "").startswith(("lexical", "expanded_lexical"))), None)
            mode_label = str(effective_mode).replace("_", " ").upper()
            if lexical:
                matched_term = str(lexical.get("matchedTerm") or lexical.get("value") or "")
                field = str(lexical.get("field") or "metadata")
                if lexical.get("matchType") == "expanded_lexical_term":
                    explanation["cardEvidence"] = f"FTS {field} matched ‘{matched_term}’ via query expansion"
                else:
                    explanation["cardEvidence"] = f"FTS {field} matched ‘{matched_term}’"
            elif explanation.get("semanticOnly"):
                explanation["cardEvidence"] = (
                    f"{mode_label} nearest neighbour only · no direct query term in visible metadata"
                )
            elif explanation.get("evidenceType") == "structured_exact":
                explanation["cardEvidence"] = explanation.get("summary")
            else:
                explanation["cardEvidence"] = explanation.get("summary") or f"Ranked by {mode_label}"
            doc["matchExplanation"] = explanation
            if effective_mode == "fts" and requested_mode == "hybrid" and mode_reason:
                doc["recommendationSource"] = "fts_structured"
            else:
                doc["recommendationSource"] = effective_mode

    @staticmethod
    def _normalised_values(values: Iterable[Any]) -> set[str]:
        return {
            re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
            for value in values
            if value
        }

    def _preference_violations(
        self,
        doc: dict[str, Any],
        profile: dict[str, Any],
        ignored_disliked_genres: Iterable[Any] = (),
    ) -> list[str]:
        violations: list[str] = []
        genres = self._normalised_values(doc.get("genres", []))
        keywords = self._normalised_values(doc.get("keywords", []))
        people = self._normalised_values(
            [*doc.get("castNames", []), *doc.get("directorNames", [])]
        )
        disliked_genres = self._normalised_values(profile.get("dislikedGenres", []))
        disliked_genres -= self._normalised_values(ignored_disliked_genres)
        disliked_themes = self._normalised_values(profile.get("dislikedThemes", []))
        disliked_people = self._normalised_values(profile.get("dislikedPeople", []))
        exclusive_conflict = exclusive_content_conflict(doc, profile)
        if exclusive_conflict is not None:
            violations.append(str(exclusive_conflict["violation"]))
        for value in sorted(genres.intersection(disliked_genres)):
            violations.append(f"disliked genre: {value}")
        for value in sorted(keywords.intersection(disliked_themes)):
            violations.append(f"disliked theme: {value}")
        for value in sorted(people.intersection(disliked_people)):
            violations.append(f"disliked person: {value}")
        if profile.get("avoidGraphicViolence"):
            graphic_terms = {"graphic violence", "gore", "splatter", "extreme violence"}
            for value in sorted(keywords.intersection(graphic_terms)):
                violations.append(f"safety preference: {value}")
        if profile.get("avoidAdultContent"):
            adult_exact_terms = {
                "adult", "adult content", "sexual content", "sexual themes",
                "sex", "sex scene", "sex scenes", "explicit sex", "nudity",
                "female nudity", "male nudity", "erotic", "erotica",
                "pornography", "pornographic",
            }
            adult_signal_fragments = {
                "adult content", "sexual content", "sexual themes", "sex scene",
                "explicit sex", "nudity", "erotic", "pornograph",
            }
            if bool(doc.get("adult")):
                violations.append("safety preference: adult title")
            for keyword in sorted(keywords):
                if keyword in adult_exact_terms or any(
                    fragment in keyword for fragment in adult_signal_fragments
                ):
                    violations.append(f"safety preference: {keyword}")
        return list(dict.fromkeys(violations))

    def _exclude_by_purpose(
        self,
        docs: list[dict[str, Any]],
        login_id: str | None,
        purpose: str,
        *,
        ignored_disliked_genres: Iterable[Any] = (),
        viewer_signals_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not login_id:
            return docs
        signals = viewer_signals_context or self.viewer_signals(login_id)
        excluded = signals["disliked"] if purpose == "search" else signals["topPickExclusions"]
        result: list[dict[str, Any]] = []
        for doc in docs:
            if doc.get("id") in excluded:
                continue
            violations = self._preference_violations(
                doc,
                signals.get("profile", {}),
                ignored_disliked_genres=ignored_disliked_genres,
            )
            exclusive_denial = any(
                item.startswith("exclusive preference:") for item in violations
            )
            if exclusive_denial or (
                purpose in {"recommendation", "search"} and violations
            ):
                continue
            result.append(doc)
        try:
            result = self._apply_entitlement_policy(
                result,
                login_id,
                remove_denied=purpose == "recommendation",
                # Recommendations can include an allowed purchase/rental title.
                # "Can I watch/play this?" routes still require includedInPlan.
                require_included=False,
                profile=dict(signals.get("profile") or {}),
            )
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            result = self._apply_entitlement_policy(
                result,
                login_id,
                remove_denied=purpose == "recommendation",
            )
        self._mark_viewer_state(result, signals)
        return result

    def _exclude_with_signals(
        self,
        docs: list[dict[str, Any]],
        login_id: str | None,
        purpose: str,
        *,
        ignored_disliked_genres: Iterable[Any] = (),
        viewer_signals_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Use one viewer snapshot while retaining compatibility with doubles."""
        try:
            return self._exclude_by_purpose(
                docs,
                login_id,
                purpose,
                ignored_disliked_genres=ignored_disliked_genres,
                viewer_signals_context=viewer_signals_context,
            )
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            return self._exclude_by_purpose(docs, login_id, purpose)

    @staticmethod
    def _mark_viewer_state(docs: list[dict[str, Any]], signals: dict[str, Any]) -> None:
        for doc in docs:
            title_id = doc.get("id")
            if title_id in signals["disliked"]:
                doc["viewerState"] = "disliked"
            elif title_id in signals["liked"]:
                doc["viewerState"] = "liked"
            elif title_id in signals["watched"]:
                doc["viewerState"] = "watched"
            elif title_id in signals["watchlist"]:
                doc["viewerState"] = "watchlist"

    def _write_trace(self, login_id: str | None, trace_type: str, payload: dict[str, Any]) -> None:
        try:
            now = time.time()
            self.traces.upsert(
                f"trace::{int(now * 1000)}::{uuid.uuid4().hex[:8]}",
                {"type": trace_type, "viewerId": login_id, "timestamp": now, **payload},
            )
        except Exception:
            pass
