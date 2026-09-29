"""Durable measured usage and separately labelled hypothetical comparisons."""
from __future__ import annotations

import json
import math
import time
import uuid
from collections import defaultdict
from comparison import estimate, validate_assumptions
from storage import StorageUnavailable, STATE_TYPE
from memory_trial import report as trial_report


def reported_usage(body: dict, operation: str) -> dict:
    raw = body.get("usage") if isinstance(body, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    def integer(*names):
        for name in names:
            value = raw.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None
    return {"inputTokens": integer("prompt_tokens", "input_tokens"),
            "outputTokens": 0 if operation == "embeddings" else integer("completion_tokens", "output_tokens"),
            "totalTokens": integer("total_tokens"),
            "cachedInputTokens": ((raw.get("prompt_tokens_details") or {}).get("cached_tokens") if isinstance(raw.get("prompt_tokens_details"), dict) else None),
            "source": "provider_response" if raw else "unavailable"}


class Ledger:
    def __init__(self, store, *, recover=True):
        self.store = store
        now = time.time()
        self.store.initialize_state({
            "type": STATE_TYPE, "schemaVersion": 3, "ledgerId": store.namespace,
            "instrumentationStartedAt": now, "windowStartedAt": now,
            "couchbasePrimarySince": now, "includeLegacyMirrors": False,
            "historyCoverage": "Couchbase-primary records from this ledger's first start; earlier mirrors are retained separately.",
            "controlEvents": [],
        })
        if recover:
            # One gateway per namespace. A process restart cannot infer token use
            # for calls whose completion was never acknowledged by Couchbase.
            for event in self.events():
                if event.get("status") == "in_flight":
                    event.update(status="unknown_outcome", interruption="gateway_restart")
                    self.put(event)
            trial = self._state().get("memoryTrial")
            if trial and trial.get("status") == "running":
                from memory_trial import update
                update(self, trial["id"], {"action": "finish", "success": False,
                       "error": "Gateway restarted during the comparison. Rerun after memory work settles."})

    def close(self):
        self.store.close()

    def _state(self):
        state = self.store.get_state()
        if state is None:
            raise StorageUnavailable("Couchbase usage settings are missing")
        return state

    def meta(self, key):
        return self._state()[key]

    def comparison_assumptions(self):
        return self._state().get("comparisonAssumptions")

    def set_comparison_assumptions(self, values):
        clean = validate_assumptions(values)
        event = {"id": "control::" + uuid.uuid4().hex, "kind": "activity",
                 "category": "comparison_assumptions_changed", "timestamp": time.time(),
                 "assumptions": clean}
        def change(state):
            state["comparisonAssumptions"] = clean
            state.setdefault("controlEvents", []).append(event)
            return state
        # The setting and its audit event commit atomically in the same document.
        self.store.update_state(change)
        return clean

    def new_window(self):
        now = time.time()
        event = {"id": "control::" + uuid.uuid4().hex, "kind": "activity",
                 "category": "reporting_window_started", "timestamp": now}
        def change(state):
            state["windowStartedAt"] = now
            state.setdefault("controlEvents", []).append(event)
            return state
        self.store.update_state(change)
        return now

    def put(self, event):
        json.dumps(event, allow_nan=False)
        self.store.put_event(event)

    def activity(self, data):
        event = {**data, "id": "activity::" + str(data.get("id") or uuid.uuid4().hex),
                 "kind": "activity", "timestamp": time.time()}
        self.put(event)
        return event["id"]

    def events(self, since=0, *, state=None):
        state = self._state() if state is None else state
        rows = self.store.events(since, include_legacy=state.get("includeLegacyMirrors", False))
        rows += [e for e in state.get("controlEvents", []) if e["timestamp"] >= since]
        return sorted(rows, key=lambda e: (e["timestamp"], e["id"]))

    def health(self):
        state = self._state()  # A real KV read, not process liveness alone.
        return {"healthy": True, "storage": "couchbase", "keyspace": self.store.keyspace,
                "ledgerId": self.store.namespace,
                "instrumentationStartedAt": state["instrumentationStartedAt"]}

    def summary(self, viewer=None):
        state = self._state()
        since = state["windowStartedAt"]
        rows = self.events(since, state=state)
        requests = [r for r in rows if r["kind"] == "model_request"]
        activity = [r for r in rows if r["kind"] == "activity" and (viewer is None or r.get("viewerId") == viewer)]
        def totals(records):
            final = [r for r in records if r.get("status") != "in_flight"]
            durations = sorted(r["durationMs"] for r in final if r.get("durationMs") is not None)
            fields = ("inputTokens", "outputTokens")
            known = {f: sum((r.get("usage") or {}).get(f) or 0 for r in records) for f in fields}
            missing = sum(r.get("status") != "succeeded" or any((r.get("usage") or {}).get(f) is None for f in fields) for r in records)
            return {"requests": len(records), "succeeded": sum(r.get("status") == "succeeded" for r in records),
                    "failed": sum(r.get("status") in {"http_error", "transport_error", "stream_error", "provider_error"} for r in records),
                    "unknownOutcome": sum(r.get("status") == "unknown_outcome" for r in records),
                    "inFlight": sum(r.get("status") == "in_flight" for r in records),
                    "chatRequests": sum(r.get("operation") == "chat/completions" for r in records),
                    "embeddingRequests": sum(r.get("operation") == "embeddings" for r in records),
                    "reportedInputTokens": known["inputTokens"], "reportedOutputTokens": known["outputTokens"],
                    "inputUsageReports": sum((r.get("usage") or {}).get("inputTokens") is not None for r in records),
                    "chatOutputUsageReports": sum(r.get("operation") == "chat/completions" and (r.get("usage") or {}).get("outputTokens") is not None for r in records),
                    "requestsWithMissingUsage": missing, "usageComplete": missing == 0,
                    "meanDurationMs": round(sum(durations)/len(durations), 1) if durations else None,
                    "p95DurationMs": round(durations[math.ceil(len(durations)*0.95)-1], 1) if durations else None,
                    "durationSampleCount": len(durations)}
        groups = defaultdict(list)
        for r in requests:
            groups[(r["source"], r["operation"], r.get("model") or "unspecified")].append(r)
        external = [e for e in rows if e.get("category") == "ai_functions"]
        generations = [e for e in external if e.get("action") == "generate"]
        chats = [e for e in activity if e.get("category") == "chat_turn"]
        latencies = [e["responseMs"] for e in chats if isinstance(e.get("responseMs"), (int, float))]
        planned = [e for e in chats if e.get("plannerDecision")]
        reused = [e for e in planned if e.get("planCacheHit")]
        memory = [e for e in rows if e.get("category") == "memory_http"]
        ops = [e for e in activity if e.get("category") in {"chat_turn", "chat_failed", "catalogue_search", "viewer_interaction", "profile_update", "semantic_memory_search"}]
        return {"schemaVersion": 3, "instrumentationStartedAt": state["instrumentationStartedAt"],
                "windowStartedAt": since, "observedAt": time.time(), "modelScope": "deployment", "activityViewer": viewer,
                "totals": totals(requests), "bySource": [{"source": k[0], "operation": k[1], "model": k[2], **totals(v)} for k,v in sorted(groups.items())],
                "comparison": estimate(rows, state.get("comparisonAssumptions")),
                "memorySavings": trial_report(state.get("memoryTrial"), self.events(state["memoryTrial"]["startedAt"], state=state) if state.get("memoryTrial") else []),
                "aiFunctions": {"generationAttempts": len(generations),
                    "completed": sum(e.get("status") == "succeeded" for e in generations),
                    "failed": sum(e.get("status") == "failed" for e in generations),
                    "pendingOrUnknown": sum(e.get("status") not in {"succeeded", "failed"} for e in generations),
                    "functionResults": sum(e.get("functionsReturned") or 0 for e in generations if e.get("status") == "succeeded"),
                    "guideReuses": sum(e.get("status") == "reused" for e in external),
                    "chatReuses": sum(e.get("action") == "chat_reuse" and e.get("status") == "reused" for e in external),
                    "modelRequests": None, "tokens": None, "cost": None,
                    "coverage": "Application-observed guide operations only. Capella AI Functions do not return token usage to this meter. Earlier runs and external callers are not included."},
                "activity": {"recordedOperations": len(ops), "completedChatTurns": len(chats),
                    "chatFailures": sum(e.get("category") == "chat_failed" for e in activity),
                    "chatTurnsWithoutPlannerRequest": sum(not e.get("plannerRequested") for e in chats),
                    "plannerDecisions": len(planned), "plansReused": len(reused),
                    "planReuseRatePct": round(100*len(reused)/len(planned), 1) if planned else None,
                    "contextFactOccurrencesSupplied": sum(e.get("contextFactCount") or 0 for e in chats),
                    "meanServerAnswerMs": round(sum(latencies)/len(latencies), 1) if latencies else None,
                    "answerTimeSamples": len(latencies)},
                "memoryApi": {"requests": len(memory), "succeeded": sum(e.get("success") is True for e in memory),
                    "failed": sum(e.get("success") is False for e in memory),
                    "inFlight": sum(e.get("status") == "in_flight" for e in memory),
                    "unknownOutcome": sum(e.get("status") == "unknown_outcome" for e in memory),
                    "healthRequests": sum(e.get("resource") == "health" for e in memory),
                    "writeRequestsAccepted": sum(e.get("writeRequested") and e.get("success") is True for e in memory),
                    "meaning": "HTTP requests through the memory gateway, including health checks. Accepted writes are not a guarantee of completed background enrichment."},
                "recentRequests": list(reversed(requests[-20:])), "recentActivity": list(reversed(activity[-20:])),
                "persistence": {"storage": "couchbase", "healthy": True, "keyspace": self.store.keyspace,
                    "ledgerId": self.store.namespace, "durability": "majority_and_persist_to_active",
                    "couchbasePrimarySince": state["couchbasePrimarySince"],
                    "historyCoverage": state["historyCoverage"]},
                "billing": {"amount": None, "savings": None, "status": "not_measured",
                    "basis": "Capella Model Service is billed by provisioned capacity and clock hours. Token counts are not a bill. No measured cost-saving comparison has been run."},
                "coverage": {"historicalModelUsage": "unavailable_before_instrumentation",
                    "requestCounts": "Every inference HTTP attempt routed through this gateway, including retries and health probes.",
                    "tokens": "Provider-reported usage only. Missing usage is unknown, never estimated as zero.",
                    "outsideGateway": "External callers, managed workflows/AI Functions that bypass this gateway, local Agent Catalog embedding compute and infrastructure are not measured here."}}
