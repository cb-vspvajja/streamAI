from __future__ import annotations

import re
import time
import threading
import uuid
from typing import Any

from couchbase.options import QueryOptions
from .ai_enrichment import VERSION, FUNCTIONS, FIELDS, current_guide, response_text, source_fingerprint
from . import usage_reporter


class CapellaAIServices:
    """Optional native Capella AI Functions and Data Processing integration.

    Agent Memory, Agent Catalog and MCP work with Capella or self-managed Server.
    AI Functions and managed Data Processing workflows are enabled explicitly
    because they require Capella-side model/workflow configuration.
    """

    def __init__(self, settings: Any, cluster: Any) -> None:
        self.settings = settings
        self.cluster = cluster
        self._last_ai_function_at: float | None = None
        self._last_ai_function_error: str | None = None
        self._guide_locks = [threading.Lock() for _ in range(64)]

    def snapshot(self) -> dict[str, Any]:
        return {
            "aiFunctions": {
                "enabled": bool(self.settings.ai_functions_enabled),
                "healthy": bool(not self.settings.ai_functions_enabled or not self._last_ai_function_error),
                "native": True,
                "lastInvocationAt": self._last_ai_function_at,
                "error": self._last_ai_function_error,
                "availability": "capella_only",
                "detail": (
                    "Native Capella SQL++ default:ai_classification, ai_summary and ai_sentiment"
                    if self.settings.ai_functions_enabled
                    else "Capella-only: enable AI Functions and associate a model to generate viewing guides"
                ),
            },
            "dataProcessing": {
                "enabled": self.settings.data_processing_mode == "capella_workflow",
                "healthy": bool(
                    self.settings.data_processing_mode != "capella_workflow"
                    or self.settings.data_processing_workflow_id
                ),
                "native": self.settings.data_processing_mode == "capella_workflow",
                "mode": self.settings.data_processing_mode,
                "workflowId": self.settings.data_processing_workflow_id or None,
                "detail": (
                    "Consumes embeddings produced by a Capella AI Services workflow"
                    if self.settings.data_processing_mode == "capella_workflow"
                    else "Bundled Python loader/vectorizer"
                ),
            },
        }

    def _collections(self):
        bucket = self.cluster.bucket(self.settings.content_bucket)
        titles = bucket.scope(self.settings.catalogue_scope).collection(self.settings.titles_collection)
        events = bucket.scope(self.settings.telemetry_scope).collection(self.settings.ai_metrics_collection)
        return titles, events

    def viewing_guide(self, title_id: str) -> dict[str, Any]:
        titles, _ = self._collections()
        doc = titles.get(title_id).content_as[dict]
        guide = current_guide(doc)
        return {"titleId": title_id, "title": doc.get("title"), "originalOverview": doc.get("overview") or "",
                "guide": guide, "stale": bool(doc.get("aiEnrichment") and not guide),
                "enabled": bool(self.settings.ai_functions_enabled)}

    def _publish_event(self, events, event, viewer_id):
        # Write intent before inference so failures cannot disappear from the audit.
        events.upsert("ai-function-event::" + event["id"], {"type": "streamai_ai_function_event", **event})
        usage_reporter.record_external(viewer_id, {"category": "ai_functions", **event})

    def enrich_title(self, title_id: str, *, refresh: bool = False, viewer_id: str = "") -> dict[str, Any]:
        title_id = str(title_id or "").strip()
        if not title_id:
            raise ValueError("title_id is required")
        # Bound lock storage; concurrent clicks on the same title reuse the first result.
        with self._guide_locks[sum(title_id.encode()) % len(self._guide_locks)]:
            return self._enrich_title(title_id, refresh=refresh, viewer_id=viewer_id)

    def _enrich_title(self, title_id: str, *, refresh: bool, viewer_id: str) -> dict[str, Any]:
        from couchbase.options import ReplaceOptions
        titles, events = self._collections()
        existing = titles.get(title_id)
        doc = existing.content_as[dict]
        cached = current_guide(doc)
        recipe = {"temperature": self.settings.ai_functions_temperature,
                  "maxTokens": self.settings.ai_functions_max_tokens,
                  "maxWords": getattr(self.settings, "ai_functions_max_words", 120)}
        started = time.perf_counter()
        event = {"id": uuid.uuid4().hex, "titleId": title_id, "startedAt": time.time(),
                 "status": "in_flight", "action": "generate", "functions": list(FUNCTIONS),
                 "tokenUsage": None, "modelRequests": None, "functionsReturned": 0}
        if cached and cached.get("recipe") == recipe and not refresh:
            event.update(status="reused", action="reuse", executionId=cached["executionId"],
                         completedAt=time.time(), functionsRequested=0, modelRequests=0,
                         tokenUsage={"inputTokens": 0, "outputTokens": 0})
            self._publish_event(events, event, viewer_id)
            return {"result": {"id": title_id, "title": doc.get("title"), **{k: cached[k] for k in FIELDS}},
                    "guide": cached, "originalOverview": doc.get("overview") or "", "cacheHit": True,
                    "durationMs": round((time.perf_counter()-started)*1000, 1),
                    "execution": "Saved viewing guide from Couchbase", "actionId": event["id"]}
        if not self.settings.ai_functions_enabled:
            raise RuntimeError("AI Functions are disabled. Enable them in Capella and set AI_FUNCTIONS_ENABLED=true to generate a guide.")
        event["functionsRequested"] = len(FUNCTIONS)
        self._publish_event(events, event, viewer_id)
        bucket = self._identifier(self.settings.content_bucket)
        scope = self._identifier(self.settings.catalogue_scope)
        collection = self._identifier(self.settings.titles_collection)
        statement = f"""
        SELECT RAW {{
          "id": META(t).id,
          "title": t.title,
          "originalOverview": IFMISSINGORNULL(t.overview, ""),
          "classification": default:ai_classification({{
            "text": CONCAT(t.title, ". ", IFMISSINGORNULL(t.overview, "")),
            "labels": ["uplifting", "cerebral", "high-tension", "family-friendly", "dark", "slow-burn"],
            "temperature": $temperature,
            "max_tokens": $maxTokens
          }}),
          "summary": default:ai_summary({{
            "text": IFMISSINGORNULL(t.overview, t.title),
            "temperature": $temperature,
            "max_words": $maxWords
          }}),
          "sentiment": default:ai_sentiment({{
            "text": IFMISSINGORNULL(t.overview, t.title),
            "temperature": $temperature,
            "max_tokens": $maxTokens
          }})
        }}
        FROM `{bucket}`.`{scope}`.`{collection}` AS t
        USE KEYS $titleId
        WHERE t.title = $sourceTitle AND IFMISSINGORNULL(t.overview, "") = $sourceOverview
        """
        try:
            result = self.cluster.query(statement, QueryOptions(
                named_parameters={"titleId": title_id, **recipe, "sourceTitle": doc.get("title"),
                                  "sourceOverview": doc.get("overview") or ""},
                read_only=True, adhoc=False, client_context_id="streamai-ai::" + event["id"],
            ))
            rows = list(result.rows())
            if not rows:
                raise ValueError("The title changed during generation or is no longer available. Open it again and retry.")
            normalized = {key: response_text(rows[0].get(key)) for key in FIELDS}
            query_id = None
            try:
                query_id = result.metadata().request_id()
            except (AttributeError, TypeError):
                pass
            guide = {"version": VERSION, "status": "succeeded", **normalized,
                     "sourceFingerprint": source_fingerprint(doc), "recipe": recipe,
                     "executionId": event["id"], "queryRequestId": query_id,
                     "generatedAt": time.time(), "functions": list(FUNCTIONS),
                     "durationMs": round((time.perf_counter()-started)*1000, 1),
                     "model": "Model associated with AI Functions in Capella",
                     "tokenUsage": None, "source": "Capella SQL++ AI Functions"}
            # CAS protects catalogue edits, including authoritative entitlement fields.
            titles.replace(title_id, {**doc, "aiEnrichment": guide}, ReplaceOptions(cas=existing.cas))
            event.update(status="succeeded", completedAt=time.time(), executionId=event["id"],
                         queryRequestId=query_id, functionsReturned=len(FIELDS), durationMs=guide["durationMs"], persisted=True)
            self._publish_event(events, event, viewer_id)
            self._last_ai_function_at = guide["generatedAt"]
            self._last_ai_function_error = None
            return {"result": {"id": title_id, "title": doc.get("title"), **normalized}, "guide": guide,
                    "originalOverview": doc.get("overview") or "", "cacheHit": False,
                    "durationMs": guide["durationMs"], "component": "Couchbase AI Data Plane AI Functions",
                    "execution": "SQL++ AI Functions", "actionId": event["id"]}
        except Exception as exc:
            detail = ("Grant Query Curl Access to the application's cluster credential."
                      if "query_external_access" in str(exc) else str(exc)[:700])
            self._last_ai_function_error = f"{type(exc).__name__}: {detail}"
            event.update(status="failed", completedAt=time.time(), error=self._last_ai_function_error)
            try:
                self._publish_event(events, event, viewer_id)
            except Exception:
                pass  # The durable in-flight event still marks an incomplete operation.
            raise RuntimeError("AI viewing guide could not be completed. " + self._last_ai_function_error) from exc

    def data_processing_status(self) -> dict[str, Any]:
        bucket = self._identifier(self.settings.content_bucket)
        scope = self._identifier(self.settings.catalogue_scope)
        collection = self._identifier(self.settings.titles_collection)
        statement = f"""
        SELECT RAW {{
          "documents": COUNT(1),
          "embedded": COUNT(CASE WHEN t.embedding IS VALUED THEN 1 ELSE NULL END),
          "validDimensions": COUNT(CASE WHEN ISARRAY(t.embedding) AND ARRAY_LENGTH(t.embedding) = $expectedDimensions THEN 1 ELSE NULL END),
          "dimension": MAX(CASE WHEN t.embedding IS VALUED THEN ARRAY_LENGTH(t.embedding) ELSE 0 END),
          "models": ARRAY_DISTINCT(ARRAY_AGG(t.embeddingMetadata.model))
        }}
        FROM `{bucket}`.`{scope}`.`{collection}` AS t
        """
        rows = list(self.cluster.query(statement, QueryOptions(readonly=True, adhoc=False, named_parameters={"expectedDimensions": self.settings.embedding_dimensions})).rows())
        data = rows[0] if rows else {}
        return {
            **self.snapshot()["dataProcessing"],
            "catalogue": data,
            "readyForVectorSearch": int(data.get("documents") or 0) > 0 and int(data.get("validDimensions") or 0) == int(data.get("documents") or 0),
        }

    @staticmethod
    def _identifier(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", str(value)):
            raise ValueError(f"Unsafe Couchbase identifier: {value!r}")
        return str(value)
