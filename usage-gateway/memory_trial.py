"""Paired, observed memory-policy trials. No assumed calls or token counts."""
from copy import deepcopy
from decimal import Decimal
import time
import uuid
from comparison import DEFAULTS

PHASES = ("previous", "optimized")
PRICE_KEYS = ("llmInputUsdPerMillion", "llmOutputUsdPerMillion", "embeddingInputUsdPerMillion")

def measured(records, prices):
    llm = [e for e in records if e.get("operation") == "chat/completions"]
    emb = [e for e in records if e.get("operation") == "embeddings"]
    missing = sum(e.get("status") != "succeeded" or any(
        (e.get("usage") or {}).get(k) is None for k in ("inputTokens", "outputTokens")) for e in records)
    def tokens(es, key):
        return None if missing else sum((e.get("usage") or {}).get(key, 0) for e in es)
    a, b, c = tokens(llm, "inputTokens"), tokens(llm, "outputTokens"), tokens(emb, "inputTokens")
    cost = None if missing else float(sum(Decimal(str(n)) * Decimal(str(prices[k]))
        for n, k in zip((a,b,c), PRICE_KEYS)) / Decimal(1_000_000))
    return {"llmRequests":len(llm), "embeddingRequests":len(emb), "llmInputTokens":a,
            "llmOutputTokens":b, "llmTokens":None if missing else a+b, "embeddingInputTokens":c,
            "referenceCostUsd":cost, "missingUsage":missing, "requestIds":[e["id"] for e in records]}

def report(trial, rows):
    if not trial:
        return {"status":"not_run", "basis":"measured_memory_policy_trial"}
    result = deepcopy(trial)
    prices = trial["prices"]
    model = [r for r in rows if r.get("kind") == "model_request"]
    all_work = [r for r in model if trial["startedAt"] <= r["timestamp"] <= trial.get("finishedAt", time.time())]
    result["totalTrialUsage"] = measured(all_work, prices)
    result["phases"] = {}
    all_phase_ids = set()
    problems = []
    for name in PHASES:
        phase = trial.get("phases", {}).get(name)
        if not phase:
            problems.append("Both policies have not finished.")
            continue
        end = phase.get("finishedAt", time.time())
        # Tagged calls include delayed/retried work even after the nominal end.
        # Untagged background calls within each phase are included, never hidden.
        work = [r for r in model if (r.get("trialId")==trial["id"] and r.get("trialPhase")==name)
                or (r.get("trialId") != trial["id"] and phase["startedAt"] <= r["timestamp"] < end)]
        tagged = [r for r in work if r.get("trialId")==trial["id"]]
        all_phase_ids.update(r["id"] for r in work)
        totals = measured(work, prices)
        proof = phase.get("proof") or {}
        valid_proof = (proof.get("contentMatches") is True and proof.get("readyBlocks") == trial["turns"] + trial["facts"]
                       and proof.get("fingerprint") == trial["fingerprint"])
        # Every turn and the optional fact batch must have an observed embedding.
        tagged_embeddings = sum(r.get("operation")=="embeddings" for r in tagged)
        if not valid_proof or tagged_embeddings < trial["turns"] + bool(trial["facts"]):
            problems.append("Original-content or correlated embedding verification is incomplete.")
        if name == "previous" and not any(r.get("operation")=="chat/completions" for r in tagged):
            problems.append("The previous policy has no correlated LLM request.")
        if phase.get("finishedAt") and any(r["timestamp"] >= phase["finishedAt"] for r in tagged):
            problems.append("Late model work was detected; rerun after processing has settled.")
        if totals["missingUsage"]:
            problems.append("Some model attempts failed or did not report complete usage.")
        if any(r.get("source")!="agent_memory" or r.get("trialId") not in {None, trial["id"]} for r in work):
            problems.append("Other application/model activity overlapped this trial.")
        result["phases"][name] = {**phase, **totals, "correlatedRequests":len(tagged),
                                 "backgroundRequests":len(work)-len(tagged), "contentVerified":valid_proof}
    external = [e for e in rows if e.get("category")=="ai_functions"
                and trial["startedAt"] <= e["timestamp"] <= trial.get("finishedAt", time.time())
                and e.get("action")=="generate"]
    if external:
        problems.append("Unmetered AI Functions generation overlapped this trial.")
    result["otherTrialOverhead"] = measured([r for r in all_work if r["id"] not in all_phase_ids], prices)
    eligible = trial.get("status") == "completed" and not problems and len(result["phases"]) == 2
    result["validComparison"] = eligible
    result["problems"] = list(dict.fromkeys(problems))
    result["difference"] = {}
    if eligible:
        a, b = (result["phases"][k] for k in PHASES)
        result["difference"] = {key: float(Decimal(str(a[key]))-Decimal(str(b[key]))) if key=="referenceCostUsd"
                                else a[key]-b[key] for key in ("llmRequests","llmTokens","embeddingRequests","embeddingInputTokens","referenceCostUsd")}
        result["llmReductionPct"] = 100 * result["difference"]["llmTokens"] / a["llmTokens"] if a["llmTokens"] else None
    result["basis"] = "measured_memory_policy_trial"
    result["scope"] = "Same conversation saved with the previous and selective Agent Memory policies."
    result["limitations"] = ("Measures memory ingestion only, not a no-AIDP comparison or whole-application savings. "
        "Both policies retain identical originals and ready memory blocks. Selective processing omits LLM summaries of short turns and explicit facts; summary-derived retrieval quality is not evaluated. "
        "Each phase includes routed background work and retries. Outside-phase model overhead is shown separately. "
        "Dollar values use the displayed illustrative rates, not Capella billing. AI Functions, infrastructure and external callers are not priced.")
    return result

def start(ledger, payload):
    turns, facts = payload.get("turns"), payload.get("facts")
    fp = payload.get("fingerprint", "")
    import re
    if type(turns) is not int or not 2 <= turns <= 4 or type(facts) is not int or not 0 <= facts <= 6 or not re.fullmatch("[0-9a-f]{64}",fp):
        raise ValueError("A trial needs 2–4 complete short turns, at most six facts and a content fingerprint.")
    now=time.time()
    trial={"id":uuid.uuid4().hex,"startedAt":now,"status":"running","turns":turns,"facts":facts,
           "fingerprint":fp,"phases":{},"prices":{k:(ledger.comparison_assumptions() or DEFAULTS)[k] for k in PRICE_KEYS}}
    def change(state):
        old=state.get("memoryTrial")
        if old and old.get("status")=="running" and now-old["startedAt"]<1800:
            raise ValueError("A memory comparison is already running.")
        state["memoryTrial"]=trial
        return state
    ledger.store.update_state(change)
    ledger.put({"id":"memory-trial::"+trial["id"],"kind":"activity","category":"memory_trial","timestamp":now,"trial":trial})
    return trial

def update(ledger, run_id, payload):
    def change(state):
        trial=state.get("memoryTrial")
        if not trial or trial["id"]!=run_id or trial["status"]!="running":
            raise ValueError("This trial is no longer active.")
        action=payload.get("action")
        phase=payload.get("phase")
        now=time.time()
        if action=="start_phase":
            if phase not in PHASES or phase in trial["phases"] or trial.get("activePhase"):
                raise ValueError("Invalid or overlapping trial phase.")
            trial["activePhase"]=phase
            trial["phases"][phase]={"startedAt":now}
        elif action=="finish_phase":
            if phase!=trial.get("activePhase"):
                raise ValueError("Wrong trial phase.")
            proof=payload.get("proof", {})
            if (set(proof)!={"readyBlocks","contentMatches","fingerprint"}
                or type(proof["readyBlocks"]) is not int or type(proof["contentMatches"]) is not bool
                or proof["fingerprint"]!=trial["fingerprint"]):
                raise ValueError("Invalid memory verification proof.")
            trial["phases"][phase].update(finishedAt=now,proof=proof)
            trial.pop("activePhase",None)
        elif action=="finish":
            if payload.get("success") is True and (trial.get("activePhase") or set(trial["phases"])!=set(PHASES)):
                raise ValueError("Both phases must complete first.")
            trial.update(status="completed" if payload.get("success") is True else "failed",finishedAt=now,
                         error=str(payload.get("error") or "")[:200],cleanupSucceeded=payload.get("cleanupSucceeded") is True)
            trial.pop("activePhase",None)
        else:
            raise ValueError("Unknown trial action.")
        return state
    state=ledger.store.update_state(change)
    trial=state["memoryTrial"]
    ledger.put({"id":"memory-trial::"+trial["id"],"kind":"activity","category":"memory_trial","timestamp":trial["startedAt"],"trial":trial})
    return trial
