"""An explicit hypothetical baseline, never a measured savings or billing claim."""
from __future__ import annotations
from decimal import Decimal, InvalidOperation

DEFAULTS = {
    "llmRequestsPerChat": 1,
    "inputTokensPerLlmRequest": 1000,
    "outputTokensPerLlmRequest": 200,
    "embeddingRequestsPerChat": 0,
    "embeddingTokensPerChat": 0,
    "llmInputUsdPerMillion": 2.5,
    "llmOutputUsdPerMillion": 10.0,
    "embeddingInputUsdPerMillion": 0.10,
}
COUNTS = {"llmRequestsPerChat", "inputTokensPerLlmRequest", "outputTokensPerLlmRequest",
          "embeddingRequestsPerChat", "embeddingTokensPerChat"}


def validate_assumptions(values):
    if not isinstance(values, dict) or set(values) != set(DEFAULTS):
        raise ValueError("Supply every baseline assumption and reference rate")
    clean = {}
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Assumptions must be numeric")
        try:
            number = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("Invalid numeric assumption") from exc
        if not number.is_finite() or number < 0 or number > 1_000_000:
            raise ValueError("Assumptions must be finite values from 0 to 1,000,000")
        if key in COUNTS and number != number.to_integral_value():
            raise ValueError("Request and token assumptions must be whole numbers")
        if key.endswith("RequestsPerChat") and number > 100:
            raise ValueError("Request assumptions cannot exceed 100 per chat")
        clean[key] = int(number) if key in COUNTS else float(number)
    if bool(clean["embeddingRequestsPerChat"]) != bool(clean["embeddingTokensPerChat"]):
        raise ValueError("Embedding requests and tokens must both be zero, or both positive")
    return clean


def estimate(rows, assumptions=None):
    params = validate_assumptions(DEFAULTS if assumptions is None else assumptions)
    turns = sum(e.get("kind") == "activity" and e.get("category") == "chat_turn" for e in rows)
    failures = sum(e.get("category") == "chat_failed" for e in rows)
    models = [e for e in rows if e.get("kind") == "model_request"]
    llm = [e for e in models if e.get("operation") == "chat/completions"]
    embeddings = [e for e in models if e.get("operation") == "embeddings"]
    def measured(records, fields):
        missing = sum(e.get("status") != "succeeded" or any((e.get("usage") or {}).get(f) is None for f in fields) for e in records)
        return missing, {f: sum((e.get("usage") or {}).get(f) or 0 for e in records) if not missing else None for f in fields}
    llm_missing, lt = measured(llm, ("inputTokens", "outputTokens"))
    embed_missing, et = measured(embeddings, ("inputTokens",))
    baseline = {"llmRequests": turns * params["llmRequestsPerChat"],
                "embeddingRequests": turns * params["embeddingRequestsPerChat"],
                "embeddingInputTokens": turns * params["embeddingTokensPerChat"]}
    baseline["llmInputTokens"] = baseline["llmRequests"] * params["inputTokensPerLlmRequest"]
    baseline["llmOutputTokens"] = baseline["llmRequests"] * params["outputTokensPerLlmRequest"]
    observed = {"llmRequests": len(llm), "embeddingRequests": len(embeddings),
                "llmInputTokens": lt["inputTokens"], "llmOutputTokens": lt["outputTokens"],
                "embeddingInputTokens": et["inputTokens"]}
    def cost(part):
        if any(part.get(k) is None for k in ("llmInputTokens", "llmOutputTokens", "embeddingInputTokens")):
            return None
        total = sum(Decimal(str(part[k])) * Decimal(str(params[price])) for k, price in (
            ("llmInputTokens", "llmInputUsdPerMillion"), ("llmOutputTokens", "llmOutputUsdPerMillion"),
            ("embeddingInputTokens", "embeddingInputUsdPerMillion"))) / Decimal(1_000_000)
        return float(total)
    baseline["referenceCostUsd"] = cost(baseline)
    observed["referenceCostUsd"] = cost(observed)
    for part in (baseline, observed):
        part["llmTokens"] = None if part["llmInputTokens"] is None or part["llmOutputTokens"] is None else part["llmInputTokens"] + part["llmOutputTokens"]
    differences = {key: (float(Decimal(str(value)) - Decimal(str(observed[key]))) if key == "referenceCostUsd" else value - observed[key]) if observed[key] is not None and turns else None for key,value in baseline.items()}
    return {"basis": "hypothetical_baseline", "currency": "USD", "assumptions": params,
            "assumptionsOrigin": "operator_configured" if assumptions is not None else "illustrative_defaults",
            "scope": "All routed model work, all viewers, current reporting window", "scopeId": "all_routed_model_work", "overallSavingsMeasured": False, "completedChats": turns, "failedChats": failures,
            "baseline": baseline, "observed": observed, "estimatedDifference": differences,
            "missingLlmUsage": llm_missing, "missingEmbeddingUsage": embed_missing,
            "llmReductionPct": 100*differences["llmTokens"]/baseline["llmTokens"] if differences["llmTokens"] is not None and baseline["llmTokens"] else None,
            "referenceCostReductionPct": 100*differences["referenceCostUsd"]/baseline["referenceCostUsd"] if differences["referenceCostUsd"] is not None and baseline["referenceCostUsd"] else None,
            "provisional": True,
            "limitations": "An assumed baseline for completed chats is compared with ALL routed model work in this window. AI Functions and other calls outside the gateway are excluded, not zero. Routed work includes background memory, setup, ingestion and retries. The baseline assigns no other work beyond the entered assumptions. Quality equivalence has not been tested. Queued background work may still add usage. Reference prices are hypothetical USD rates, not Capella prices or billed savings. Negative differences mean extra usage or reference cost; missing usage suppresses the affected estimates."}
