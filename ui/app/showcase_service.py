from __future__ import annotations

import difflib
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from couchbase.n1ql import QueryScanConsistency
from couchbase.options import QueryOptions

from .agent_service import GovernedAgentService
from .llm_service import LocalModelService


class ShowcaseService:
    """Presenter-facing orchestration for the StreamAI v2 showcase.

    The service stores scenario state, experiments, evaluation runs and profile
    snapshots in Couchbase. It deliberately reuses the production catalogue,
    profile, entitlement, trace and metrics services rather than maintaining a
    parallel demo-only data model.
    """

    PERSONAS: dict[str, dict[str, Any]] = {
        "executive": {
            "name": "Executive viewer",
            "description": "Cerebral science fiction and thrillers; avoids horror.",
            "profile": {
                "preferredGenres": ["Science Fiction", "Thriller", "Crime"],
                "dislikedGenres": ["Horror"],
                "preferredThemes": ["Atmospheric", "Technology", "Political intrigue"],
                "dislikedThemes": ["Graphic violence"],
                "preferredPeople": ["Christopher Nolan", "Denis Villeneuve"],
                "dislikedPeople": [],
                "preferredLanguages": ["English"],
                "preferredContentTypes": ["movie", "tv"],
                "avoidGraphicViolence": True,
                "avoidAdultContent": False,
                "maxRuntimeMinutes": 190,
            },
            "entitlement": {"regionCode": "GB", "subscriptionTier": "standard", "maxParentalRating": "18", "deviceType": "web-demo"},
        },
        "family": {
            "name": "Family household",
            "description": "Animation, family and adventure with a 12 rating limit.",
            "profile": {
                "preferredGenres": ["Animation", "Family", "Adventure"],
                "dislikedGenres": ["Horror", "Crime"],
                "preferredThemes": ["Animals", "Friendship", "Coming of age"],
                "dislikedThemes": ["Graphic violence", "Adult content"],
                "preferredPeople": [],
                "dislikedPeople": [],
                "preferredLanguages": ["English"],
                "preferredContentTypes": ["movie"],
                "avoidGraphicViolence": True,
                "avoidAdultContent": True,
                "maxRuntimeMinutes": 130,
            },
            "entitlement": {"regionCode": "GB", "subscriptionTier": "premium", "maxParentalRating": "12", "deviceType": "living-room-tv"},
        },
        "guest": {
            "name": "Guest viewer",
            "description": "Minimal cold-start profile for discovery demonstrations.",
            "profile": {
                "preferredGenres": [], "dislikedGenres": [], "preferredThemes": [], "dislikedThemes": [],
                "preferredPeople": [], "dislikedPeople": [], "preferredLanguages": ["English"],
                "preferredContentTypes": [], "avoidGraphicViolence": False, "avoidAdultContent": False,
                "maxRuntimeMinutes": None,
            },
            "entitlement": {"regionCode": "GB", "subscriptionTier": "basic", "maxParentalRating": "18", "deviceType": "web-demo"},
        },
    }

    DEMO_STEPS: list[dict[str, Any]] = [
        {"id": "health", "title": "Verify the AI Data Plane", "prompt": None, "components": ["Data/KV", "Query", "Search/Vector", "Agent Memory", "Agent Catalog", "MCP", "Agent Tracer"], "expected": "All required local components show Ready."},
        {"id": "preference", "title": "Capture durable preferences", "prompt": "I like atmospheric science fiction and intelligent thrillers, but I do not like graphic horror.", "components": ["Agent Memory", "Data/KV", "Agent Tracer"], "expected": "Preferences are acknowledged and queued to durable memory."},
        {"id": "memory", "title": "Prove memory across sessions", "prompt": "What kind of films do I like?", "components": ["Agent Memory", "Agent Catalog", "MCP", "Data/KV"], "expected": "The profile is returned without replaying the earlier conversation."},
        {"id": "profile-route", "title": "Prove deterministic intent routing", "prompt": "Do I like horror?", "components": ["Agent Catalog", "Data/KV", "Agent Tracer"], "expected": "No — Horror is recorded as a genre to avoid."},
        {"id": "search-lab", "title": "Compare FTS, Vector and Hybrid", "prompt": "atmospheric political thriller", "action": "search_lab", "components": ["Search/FTS", "Search/Vector", "Model Runtime"], "expected": "Search Lab shows mode-specific results, latency, evidence and overlap without routing the phrase through chat."},
        {"id": "recommend", "title": "Explain a personalised recommendation", "prompt": "Suggest something atmospheric, cerebral and thoughtful, but not frightening.", "components": ["Search/Vector", "Data/KV", "Entitlement Policy"], "expected": "Only grounded, profile-compatible titles are shown."},
        {"id": "entitlement", "title": "Run an entitlement decision", "prompt": "Is the first one included in my plan?", "components": ["Agent Catalog", "MCP", "Data/KV", "Entitlement Policy"], "expected": "The answer is determined from region, tier and rating documents."},
        {"id": "action", "title": "Execute a governed action", "prompt": "Add the first one to my likes.", "components": ["Data/KV", "Agent Memory", "Action Receipt"], "expected": "The exact grounded title ID is liked without an LLM call."},
        {"id": "trace", "title": "Replay the complete agent session", "prompt": None, "components": ["Agent Tracer", "Agent Catalog", "MCP"], "expected": "The full conversation and every tool call are visible."},
        {"id": "resilience", "title": "Demonstrate controlled recovery", "prompt": "Suggest an atmospheric science-fiction film.", "components": ["Resilience Policy", "Search/FTS", "Agent Tracer"], "expected": "Injected embedding failure falls back to FTS while preserving grounding."},
        {"id": "value", "title": "Close with measurable value", "prompt": None, "components": ["AI Value Telemetry", "Agent Memory"], "expected": "Show observed model requests including background memory, provider-reported tokens, reporting coverage and measured latency. Billing and savings are not measured."},
    ]

    def __init__(self, settings: Any, catalogue: Any, agent_catalog_gateway: Any = None, capella_ai_services: Any = None) -> None:
        self.settings = settings
        self.catalogue = catalogue
        self.agent_catalog_gateway = agent_catalog_gateway
        self.capella_ai_services = capella_ai_services
        bucket = catalogue.cluster.bucket(settings.content_bucket)
        telemetry = bucket.scope(settings.telemetry_scope)
        self.showcase_state = telemetry.collection(settings.showcase_state_collection)
        self.experiments = telemetry.collection(settings.experiments_collection)
        self.evaluations = telemetry.collection(settings.evaluations_collection)
        self.profile_snapshots = telemetry.collection(settings.profile_snapshots_collection)
        self._faults: dict[str, bool] = {
            "mcp_unavailable": False,
            "embedding_timeout": False,
            "model_unavailable": False,
            "search_unavailable": False,
            "agent_memory_unavailable": False,
        }

    @property
    def profile_snapshots_keyspace(self) -> str:
        return self.catalogue._keyspace(self.settings.telemetry_scope, self.settings.profile_snapshots_collection)

    @property
    def experiments_keyspace(self) -> str:
        return self.catalogue._keyspace(self.settings.telemetry_scope, self.settings.experiments_collection)

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": self.settings.app_version,
            "enabled": bool(self.settings.showcase_enabled),
            "presenterMode": bool(self.settings.presenter_mode_enabled),
            "steps": self.DEMO_STEPS,
            "personas": [{"id": key, "name": value["name"], "description": value["description"]} for key, value in self.PERSONAS.items()],
            "faults": dict(self._faults),
            "capella": self.capella_profile(),
        }

    def set_faults(self, login_id: str, faults: dict[str, Any]) -> dict[str, Any]:
        for key in list(self._faults):
            if key in faults:
                self._faults[key] = bool(faults[key])
        document = {
            "type": "streamai_showcase_state", "viewerId": login_id, "faults": dict(self._faults),
            "updatedAt": time.time(), "appVersion": self.settings.app_version,
        }
        self.showcase_state.upsert(f"showcase::{login_id}", document)
        self.catalogue.set_showcase_faults(self._faults)
        return document

    def faults(self) -> dict[str, bool]:
        return dict(self._faults)

    def seed_persona(self, login_id: str, persona_id: str) -> dict[str, Any]:
        if persona_id not in self.PERSONAS:
            raise ValueError(f"Unknown persona: {persona_id}")
        persona = self.PERSONAS[persona_id]
        before = self.catalogue.get_profile(login_id)
        self.write_profile_snapshot(login_id, before, "before_persona_seed")
        profile = dict(before)
        for key, value in persona["profile"].items():
            profile[key] = value
        profile["likedTitleIds"] = []
        profile["dislikedTitleIds"] = []
        profile["watchlistTitleIds"] = []
        profile["genreAffinities"] = {}
        profile["themeAffinities"] = {}
        profile["recommendationVersion"] = int(profile.get("recommendationVersion") or 0) + 1
        profile["updatedAt"] = time.time()
        self.catalogue.profiles.upsert(f"profile::{login_id}", profile)
        entitlement = {**self.catalogue.ensure_entitlement(login_id), **persona["entitlement"], "updatedAt": time.time()}
        self.catalogue.entitlements.upsert(f"entitlement::{login_id}", entitlement)
        self.catalogue.invalidate_home_cache(login_id)
        self.write_profile_snapshot(login_id, profile, f"persona_seed:{persona_id}")
        return {"persona": persona_id, "profile": profile, "entitlement": entitlement}

    def reset_demo(self, login_id: str, persona_id: str = "executive") -> dict[str, Any]:
        bucket = self.settings.content_bucket
        statements = [
            (self.catalogue.interactions_keyspace, "viewerId"),
            (self.catalogue.history_keyspace, "viewerId"),
            (self.catalogue.traces_keyspace, "viewerId"),
            (self.catalogue._keyspace(self.settings.operations_scope, self.settings.action_receipts_collection), "viewerId"),
        ]
        for keyspace, field in statements:
            self.catalogue.cluster.query(
                f"DELETE FROM {keyspace} AS d WHERE d.{field}=$viewerId",
                QueryOptions(named_parameters={"viewerId": login_id}),
            ).execute()
        self.catalogue.reset_ai_metrics(login_id)
        self.set_faults(login_id, {key: False for key in self._faults})
        result = self.seed_persona(login_id, persona_id)
        result["reset"] = True
        result["bucket"] = bucket
        return result

    def write_profile_snapshot(self, login_id: str, profile: dict[str, Any], cause: str) -> str:
        now = time.time()
        doc_id = f"profile-snapshot::{login_id}::{int(now*1000)}::{uuid.uuid4().hex[:6]}"
        self.profile_snapshots.upsert(doc_id, {
            "type": "streamai_profile_snapshot", "viewerId": login_id, "cause": cause,
            "timestamp": now, "recommendationVersion": profile.get("recommendationVersion"),
            "profile": self._redact(profile),
        })
        return doc_id

    def profile_timeline(self, login_id: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.catalogue.cluster.query(
            f"SELECT META(s).id AS id, s.* FROM {self.profile_snapshots_keyspace} AS s "
            "WHERE s.viewerId=$viewerId ORDER BY s.timestamp DESC LIMIT $limit",
            QueryOptions(named_parameters={"viewerId": login_id, "limit": max(1, min(limit, 100))}, readonly=True, scan_consistency=QueryScanConsistency.REQUEST_PLUS),
        ).rows()
        return list(rows)

    def search_lab(self, login_id: str, query: str, content_type: str | None, limit: int = 8) -> dict[str, Any]:
        modes = ["fts", "vector", "hybrid"]
        results: dict[str, Any] = {}
        for mode in modes:
            started = time.perf_counter()
            try:
                payload = self.catalogue.search(query=query, mode=mode, login_id=login_id, content_type=content_type, limit=limit, purpose="search")
                results[mode] = {**payload, "wallClockMs": round((time.perf_counter()-started)*1000, 1), "error": None}
            except Exception as exc:
                results[mode] = {"results": [], "trace": {"requestedMode": mode, "effectiveMode": "failed"}, "wallClockMs": round((time.perf_counter()-started)*1000, 1), "error": f"{type(exc).__name__}: {exc}"}
        intent = self.catalogue._analyse_query(query)
        auto_mode = "fts" if (intent.get("genres") or intent.get("country")) and not intent.get("semanticTerms") else "hybrid"
        auto = results[auto_mode]
        id_sets = {mode: {str(item.get("id")) for item in data.get("results", [])} for mode, data in results.items()}
        overlap = {
            "ftsVector": len(id_sets["fts"] & id_sets["vector"]),
            "ftsHybrid": len(id_sets["fts"] & id_sets["hybrid"]),
            "vectorHybrid": len(id_sets["vector"] & id_sets["hybrid"]),
        }
        experiment = {
            "type": "streamai_search_experiment", "experimentId": uuid.uuid4().hex,
            "viewerId": login_id, "query": query, "contentType": content_type, "timestamp": time.time(),
            "autoMode": auto_mode, "overlap": overlap,
            "metrics": {mode: {"latencyMs": data.get("wallClockMs"), "candidateCount": data.get("trace", {}).get("candidateCount"), "effectiveMode": data.get("trace", {}).get("effectiveMode"), "resultCount": len(data.get("results", [])), "error": data.get("error")} for mode, data in results.items()},
        }
        self.experiments.upsert(f"search-experiment::{experiment['experimentId']}", experiment)
        return {"query": query, "autoMode": auto_mode, "auto": auto, "modes": results, "overlap": overlap, "experimentId": experiment["experimentId"], "intent": intent}

    def recommendation_evidence(self, login_id: str, title_id: str, item: dict[str, Any] | None = None) -> dict[str, Any]:
        title = self.catalogue.title(title_id)
        profile = self.catalogue.get_profile(login_id)
        evidence = self.catalogue._profile_match_evidence(title, profile)
        violations = self.catalogue._preference_violations(title, profile)
        entitlement = self.catalogue.check_entitlement(login_id, title)
        search_score = float((item or {}).get("searchScore") or 0)
        mode = str((item or {}).get("effectiveSearchMode") or (item or {}).get("recommendationSource") or "profile")
        components = [
            {"name": "Profile genre affinity", "score": min(25, len([x for x in evidence.get("matchedFields", []) if x.get("field") == "genres"]) * 10), "kind": "positive"},
            {"name": "Theme and synopsis affinity", "score": min(20, len([x for x in evidence.get("matchedFields", []) if "keyword" in str(x.get("field"))]) * 7), "kind": "positive"},
            {"name": "People affinity", "score": min(18, len([x for x in evidence.get("matchedFields", []) if x.get("field") == "cast_or_creator"]) * 9), "kind": "positive"},
            {"name": "Search score contribution (heuristic)", "score": max(0, min(25, round(search_score * 3))) if search_score else 0, "kind": "positive"},
            {"name": "Catalogue rating contribution (heuristic)", "score": min(12, round(float(title.get("voteAverage") or 0))), "kind": "positive"},
        ]
        positive = sum(int(x["score"]) for x in components)
        penalty = min(50, len(violations) * 25)
        total = max(0, min(100, positive - penalty))
        return {
            "title": title, "score": total, "scoreMeaning": "Heuristic demo points from profile matches, available search score and catalogue rating, minus preference penalties. Missing inputs add no points. Not measured relevance, accuracy or a probability.",
            "components": components, "violations": violations, "entitlement": entitlement,
            "eligible": not violations and bool(entitlement.get("allowed")), "profileEvidence": evidence,
            "mode": mode,
        }

    def simulate_entitlement(self, login_id: str, title_id: str, scenario: dict[str, Any]) -> dict[str, Any]:
        requested_title = str(title_id or "").strip()
        if not requested_title:
            raise ValueError("Select a title card or enter a title/catalogue ID.")
        try:
            title = self.catalogue.title(requested_title)
        except Exception:
            resolution = self.catalogue.resolve_title(requested_title)
            title = resolution.get("match")
            if not title:
                raise ValueError(f"No exact catalogue title matched ‘{requested_title}’.")
        baseline_entitlement = self.catalogue.ensure_entitlement(login_id)
        simulated = {**baseline_entitlement}
        mappings = {"region": "regionCode", "tier": "subscriptionTier", "parentalRating": "maxParentalRating", "deviceType": "deviceType", "roamingAllowed": "roamingAllowed"}
        for source, target in mappings.items():
            if source in scenario and scenario[source] is not None:
                simulated[target] = scenario[source]
        baseline = self.catalogue.check_entitlement(login_id, title, entitlement=baseline_entitlement)
        decision = self.catalogue.check_entitlement(login_id, title, entitlement=simulated)
        return {"title": title, "requestedTitle": requested_title, "baseline": baseline, "scenario": simulated, "decision": decision, "mutated": False}

    def governance(self) -> dict[str, Any]:
        snapshot = self.agent_catalog_gateway.snapshot() if self.agent_catalog_gateway else {"healthy": False, "tools": []}
        prompt = self.agent_catalog_gateway.prompt_content if self.agent_catalog_gateway else None
        root = Path(__file__).resolve().parents[1]
        prompt_path = root / "agent_catalog" / "prompts" / "streaming_assistant_system.prompt"
        tool_path = root / "agent_catalog" / "tools" / "streamai_mcp_tools.py"
        prompt_source = prompt_path.read_text() if prompt_path.exists() else (prompt or "")
        tool_source = tool_path.read_text() if tool_path.exists() else ""
        baseline_lines = [
            "Ground every title in the verified catalogue.",
            "Use entitlement data for availability decisions.",
            "Use deterministic profile routes before the LLM.",
        ]
        current_lines = [line.strip() for line in (prompt or prompt_source).splitlines() if line.strip()]
        diff = list(difflib.unified_diff(baseline_lines, current_lines[:40], fromfile="baseline-policy", tofile="active-prompt", lineterm=""))
        return {
            "catalog": snapshot,
            "prompt": {"name": self.settings.agent_catalog_prompt_name, "content": prompt or prompt_source, "sha256": hashlib.sha256(prompt_source.encode()).hexdigest(), "source": str(prompt_path.relative_to(root)) if prompt_path.exists() else None},
            "toolsSource": {"sha256": hashlib.sha256(tool_source.encode()).hexdigest(), "source": str(tool_path.relative_to(root)) if tool_path.exists() else None},
            "diff": diff[:120], "deepLinks": self.capella_profile().get("deepLinks", {}),
            "principle": "Agent Catalog versions tools and prompts; the application framework executes them.",
        }

    def run_evaluation(self, login_id: str) -> dict[str, Any]:
        cases: list[dict[str, Any]] = []
        def add(name: str, passed: bool, detail: str) -> None:
            cases.append({"name": name, "passed": bool(passed), "detail": detail})
        pref = LocalModelService.extract_profile_preference_question("Do I like horror?")
        add("Profile question routing", bool(pref and pref.get("subject", "").lower() == "horror"), json.dumps(pref, default=str))
        action = GovernedAgentService.extract_action_request("Add the first one to my likes", {"resultTitleIds": ["movie::one"], "resultTitles": ["One"]})
        add("Ordinal governed action", bool(action and action.get("resolvedTitleId") == "movie::one"), json.dumps(action, default=str))
        context_intent = LocalModelService.extract_contextual_request(
            "Show me the ones I can watch from this list"
        )
        add(
            "Previous-result intersection routing",
            context_intent == {
                "kind": "filter_previous_results",
                "filter": "playable",
            },
            json.dumps(context_intent, default=str),
        )
        missing_action = GovernedAgentService.extract_action_request(
            "Add the ones missing in my list",
            {
                "resultTitleIds": ["movie::one", "movie::two"],
                "resultTitles": ["One", "Two"],
            },
        )
        add(
            "Collective exact-ID action",
            bool(
                missing_action
                and missing_action.get("selectionMode")
                == "previous_missing_from_watchlist"
                and missing_action.get("resolvedTitleIds")
                == ["movie::one", "movie::two"]
            ),
            json.dumps(missing_action, default=str),
        )
        analytics = LocalModelService.extract_catalogue_analytics_request(
            "How many movies are in the catalogue by genre?"
        )
        add(
            "Validated catalogue analytics routing",
            bool(
                analytics
                and analytics.get("measure") == "count"
                and analytics.get("dimension") == "genre"
                and (analytics.get("filters") or {}).get("contentType") == "movie"
            ),
            json.dumps(analytics, default=str),
        )
        add(
            "Generic viewer-profile recall",
            LocalModelService.is_profile_question("What do I like?"),
            "what do i like -> structured viewer profile",
        )
        title_question = LocalModelService.extract_catalogue_title_question("Is Supergirl included in my plan?")
        title = (title_question or {}).get("title")
        add("Entitlement title extraction", title == "Supergirl", str(title_question))
        genre_intent = self.catalogue._analyse_query("Thriller")
        add("Exact genre shortcut", genre_intent.get("genres") == ["Thriller"], json.dumps(genre_intent, default=str))
        semantic_intent = self.catalogue._analyse_query("atmospheric and cerebral")
        add("Semantic query detection", bool(semantic_intent.get("cleaned")), json.dumps(semantic_intent, default=str))
        try:
            health = self.catalogue.health()
            add("Catalogue grounding available", bool(health.get("catalogue_count", 0) > 0), json.dumps(health, default=str))
            add("Search index available", bool(health.get("search_healthy")), json.dumps(health, default=str))
        except Exception as exc:
            add("Catalogue grounding available", False, str(exc))
        passed = sum(1 for case in cases if case["passed"])
        run = {
            "type": "streamai_evaluation_run", "runId": uuid.uuid4().hex, "viewerId": login_id,
            "timestamp": time.time(), "appVersion": self.settings.app_version,
            "scope": "Rule and availability checks; not an LLM quality or recommendation benchmark.",
            "passed": passed, "total": len(cases), "passRatePct": round(passed / max(len(cases), 1) * 100, 1), "cases": cases,
        }
        self.evaluations.upsert(f"evaluation::{run['runId']}", run)
        return run

    def supporting_documents(self, login_id: str, title_id: str | None = None, trace_id: str | None = None) -> dict[str, Any]:
        docs: list[dict[str, Any]] = []
        profile = self.catalogue.get_profile(login_id)
        docs.append({"label": "Viewer profile", "bucket": self.settings.content_bucket, "scope": self.settings.viewers_scope, "collection": self.settings.profiles_collection, "id": f"profile::{login_id}", "document": self._redact(profile)})
        entitlement = self.catalogue.ensure_entitlement(login_id)
        docs.append({"label": "Entitlement", "bucket": self.settings.content_bucket, "scope": self.settings.operations_scope, "collection": self.settings.entitlements_collection, "id": f"entitlement::{login_id}", "document": self._redact(entitlement)})
        if title_id:
            title = self.catalogue.title(title_id)
            docs.append({"label": "Catalogue title", "bucket": self.settings.content_bucket, "scope": self.settings.catalogue_scope, "collection": self.settings.titles_collection, "id": title_id, "document": self._redact(title)})
        if trace_id:
            trace = self.catalogue.get_agent_trace(login_id, trace_id)
            docs.append({"label": "Agent trace", "bucket": self.settings.content_bucket, "scope": self.settings.recommendations_scope, "collection": self.settings.traces_collection, "id": trace_id, "document": self._redact(trace)})
        return {"documents": docs, "redacted": True}

    def capella_profile(self) -> dict[str, Any]:
        configured = self.settings.deployment_target == "capella"
        snapshot = self.capella_ai_services.snapshot() if self.capella_ai_services else {}
        deep_links = {
            "console": self.settings.capella_console_base_url,
            "agentTracer": self.settings.capella_agent_tracer_url or None,
            "toolsHub": self.settings.capella_tools_hub_url or None,
            "promptsHub": self.settings.capella_prompts_hub_url or None,
            "modelService": self.settings.capella_model_service_url or None,
        }
        return {
            "configured": configured,
            "deploymentTarget": self.settings.deployment_target,
            "projectId": self.settings.capella_project_id or None,
            "clusterId": self.settings.capella_cluster_id or None,
            "deepLinks": deep_links,
            "services": snapshot,
            "localTruth": "AI Functions and managed Data Processing remain disabled locally; Agent Memory, Agent Catalog, MCP and operational Search remain demonstrable.",
        }

    @classmethod
    def _redact(cls, value: Any) -> Any:
        sensitive = {"password", "pin", "pin_hash", "pin_salt", "token", "api_key", "authorization", "embedding"}
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if str(key).lower() in sensitive or any(part in str(key).lower() for part in ("password", "token", "secret")):
                    result[key] = "[redacted]"
                else:
                    result[key] = cls._redact(item)
            return result
        if isinstance(value, list):
            if len(value) > 50:
                return [*value[:50], f"… {len(value)-50} more"]
            return [cls._redact(item) for item in value]
        if isinstance(value, str) and len(value) > 5000:
            return value[:5000] + "…"
        return value
