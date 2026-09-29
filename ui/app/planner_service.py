from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from .catalogue_service import CatalogueService
from .config import Settings
from .llm_service import LocalModelService


class AssistantPlannerService:
    """Create, validate and reuse governed catalogue plans.

    The planner model interprets language only. Every returned plan is reduced
    to an allowlisted schema before the catalogue service can execute it.
    """

    ALLOWED_INTENTS = {
        "catalogue_query",
        "recommendation",
        "similarity",
        "catalogue_analytics",
        "unknown",
    }
    ALLOWED_SORTS = {
        "relevance",
        "rating_desc",
        "popularity_desc",
        "release_year_desc",
        "personalised",
    }
    ALLOWED_PERIODS = {"LAST_YEAR", "THIS_YEAR", "RECENT"}
    ALLOWED_MEASURES = {"count", "average_rating", "average_runtime"}
    ALLOWED_GROUPS = {
        "genre",
        "release_year",
        "original_language",
        "country",
        "content_type",
    }
    NUMBER_WORDS = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "fifteen": 15,
        "twenty": 20,
    }

    def __init__(
        self,
        settings: Settings,
        catalogue: CatalogueService,
        model: LocalModelService,
    ) -> None:
        self.settings = settings
        self.catalogue = catalogue
        self.model = model

    @staticmethod
    def _string(value: Any) -> str | None:
        text = str(value or "").strip()
        return text if text and text.lower() not in {"null", "none"} else None

    @staticmethod
    def _integer(value: Any, *, minimum: int, maximum: int) -> int | None:
        if value is None or value == "":
            return None
        try:
            return min(max(int(value), minimum), maximum)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _number(value: Any, *, minimum: float, maximum: float) -> float | None:
        if value is None or value == "":
            return None
        try:
            return min(max(float(value), minimum), maximum)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _requested_limit(cls, lower: str, *, default: int = 12) -> int:
        patterns = (
            r"\b(?:show|find|list|give|recommend|suggest)(?: me)? (?:the )?"
            r"(?P<count>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|"
            r"eleven|twelve|fifteen|twenty)\b",
            r"\b(?:top|highest|best) "
            r"(?P<count>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|"
            r"eleven|twelve|fifteen|twenty)\b",
        )
        token: str | None = None
        for pattern in patterns:
            match = re.search(pattern, lower)
            if match:
                token = match.group("count")
                break
        if token is None:
            return default
        value = int(token) if token.isdigit() else cls.NUMBER_WORDS.get(token, default)
        return min(max(value, 1), 20)

    @staticmethod
    def _title_contains(lower: str) -> str | None:
        patterns = (
            r"(?:movies?|films?|series|shows?|titles?) (?:that |which )?"
            r"(?:have|has|contain|contains|include|includes|with) "
            r"(?P<term>.+?) (?:in|within) (?:the )?(?:name|title)",
            r"(?:movies?|films?|series|shows?|titles?) (?:whose )?"
            r"(?:name|title) (?:has|contains|includes) (?P<term>.+)",
            r"(?P<term>.+?) (?:in|within) (?:the )?(?:name|title) "
            r"(?:of )?(?:movies?|films?|series|shows?|titles?)",
        )
        for pattern in patterns:
            match = re.search(pattern, lower)
            if not match:
                continue
            term = re.sub(
                r"^(?:the word|word|called|named)\s+", "", match.group("term")
            )
            term = term.strip(" \"'“”‘’.,!?")
            if term:
                return term
        return None

    @staticmethod
    def _release_period(lower: str) -> tuple[str | None, int | None]:
        if re.search(r"\b(?:last|previous) year\b", lower):
            return "LAST_YEAR", None
        if re.search(r"\bthis year\b", lower):
            return "THIS_YEAR", None
        year = re.search(
            r"\b(?:released|came out|from)(?: in)? ((?:19|20)\d{2})\b", lower
        )
        if year:
            return None, int(year.group(1))
        if re.search(
            r"\b(?:new|latest|recent|newly released)\s+"
            r"(?:movies?|films?|series|shows?|titles?|content)\b",
            lower,
        ):
            return "RECENT", None
        return None, None

    def _guard_for_query(
        self, user_message: str, intent: dict[str, Any]
    ) -> dict[str, Any]:
        lower = self.catalogue.normalise_natural_query(user_message)
        title_contains = self._title_contains(lower)
        release_period, release_year = self._release_period(lower)
        top_rated = bool(
            re.search(
                r"\b(?:top|highest|best)(?: \d{1,2})?[ -]?(?:rated|rating)\b|"
                r"\b(?:top|highest|best) \d{1,2} (?:movies?|films?|series|shows?)\b",
                lower,
            )
        )
        personalised = bool(
            self.model.is_personalised_recommendation_request(user_message)
            or re.search(r"\b(?:for me|suit my taste|match my taste)\b", lower)
        )
        analytics = self.model.extract_catalogue_analytics_request(user_message)
        count = self.model.extract_catalogue_count_question(user_message)
        family = (
            "analytics"
            if analytics or count
            else "title_field"
            if title_contains
            else "ranked"
            if top_rated
            else "temporal"
            if release_period or release_year
            else "recommendation"
            if personalised or intent.get("genericRecommendation")
            else "discovery"
        )
        return {
            "intentFamily": family,
            "contentType": intent.get("contentType"),
            "fieldConstraint": "title_contains" if title_contains else None,
            "titleContains": title_contains,
            "releasePeriod": release_period,
            "releaseYear": release_year,
            "topRated": top_rated,
            "personalise": personalised,
            "limit": self._requested_limit(lower),
            "contextual": bool(
                re.search(
                    r"\b(?:this|that|these|those|first|second|last result|"
                    r"previous (?:result|answer|list|recommendation)|"
                    r"the ones|them|it)\b",
                    lower,
                )
            ),
        }

    @staticmethod
    def _guards_compatible(
        current: dict[str, Any], cached: dict[str, Any]
    ) -> bool:
        if current.get("contextual") or cached.get("contextual"):
            return False
        exact_fields = (
            "intentFamily",
            "fieldConstraint",
            "releasePeriod",
            "releaseYear",
            "topRated",
            "personalise",
        )
        if any(current.get(field) != cached.get(field) for field in exact_fields):
            return False
        current_type, cached_type = current.get("contentType"), cached.get("contentType")
        return not (current_type and cached_type and current_type != cached_type)

    def _rebind_cached_plan(
        self,
        cached_plan: dict[str, Any],
        *,
        user_message: str,
        intent: dict[str, Any],
        guard: dict[str, Any],
    ) -> dict[str, Any]:
        plan = json.loads(json.dumps(cached_plan))
        filters = dict(plan.get("filters") or {})
        if intent.get("contentType"):
            plan["contentType"] = intent["contentType"]
        filters["titleContains"] = guard.get("titleContains")
        filters["releasePeriod"] = guard.get("releasePeriod")
        filters["releaseYear"] = guard.get("releaseYear")
        filters["genres"] = list(intent.get("genres") or [])
        country = intent.get("country") or {}
        filters["countries"] = list(country.get("countries") or [])
        plan["filters"] = filters
        plan["limit"] = int(guard.get("limit") or plan.get("limit") or 12)
        meaningful = str(intent.get("cleaned") or "").strip()
        if plan.get("intent") in {"catalogue_query", "similarity"}:
            plan["semanticQuery"] = meaningful or plan.get("semanticQuery")
        return plan

    def _deterministic_plan(
        self,
        user_message: str,
        intent: dict[str, Any],
        guard: dict[str, Any],
    ) -> dict[str, Any] | None:
        lower = self.catalogue.normalise_natural_query(user_message)
        content_type = intent.get("contentType")
        limit = int(guard.get("limit") or 12)
        filters: dict[str, Any] = {
            "titleContains": guard.get("titleContains"),
            "titleEquals": None,
            "genres": list(intent.get("genres") or []),
            "people": [],
            "languages": [],
            "countries": list((intent.get("country") or {}).get("countries") or []),
            "releasePeriod": guard.get("releasePeriod"),
            "releaseYear": guard.get("releaseYear"),
            "releaseYearMin": None,
            "releaseYearMax": None,
            "ratingMin": None,
            "runtimeMax": None,
        }
        if guard.get("fieldConstraint") == "title_contains":
            title_term = re.sub(
                r"[^a-z0-9]+",
                " ",
                str(guard.get("titleContains") or "").lower(),
            ).strip()
            filters["genres"] = [
                genre
                for genre in filters["genres"]
                if re.sub(
                    r"[^a-z0-9]+", " ", str(genre).lower()
                ).strip()
                != title_term
            ]
            return {
                "intent": "catalogue_query",
                "contentType": content_type,
                "filters": filters,
                "semanticQuery": None,
                "personalise": False,
                "sort": "popularity_desc",
                "limit": limit,
                "aggregation": None,
                "confidence": 1.0,
            }
        if guard.get("topRated"):
            return {
                "intent": "catalogue_query",
                "contentType": content_type,
                "filters": filters,
                "semanticQuery": None,
                "personalise": False,
                "sort": "rating_desc",
                "limit": limit if limit != 12 else 1,
                "aggregation": None,
                "confidence": 1.0,
            }
        if guard.get("releasePeriod") or guard.get("releaseYear"):
            personalised = bool(guard.get("personalise"))
            return {
                "intent": "recommendation" if personalised else "catalogue_query",
                "contentType": content_type,
                "filters": filters,
                "semanticQuery": None,
                "personalise": personalised,
                "sort": "personalised" if personalised else "release_year_desc",
                "limit": limit,
                "aggregation": None,
                "confidence": 1.0,
            }
        analytics = self.model.extract_catalogue_analytics_request(user_message)
        if analytics:
            return {
                "intent": "catalogue_analytics",
                "contentType": (analytics.get("filters") or {}).get("contentType"),
                "filters": filters,
                "semanticQuery": None,
                "personalise": False,
                "sort": "relevance",
                "limit": int(analytics.get("limit") or 30),
                "aggregation": {
                    "measure": analytics.get("measure"),
                    "groupBy": analytics.get("dimension"),
                },
                "confidence": 1.0,
            }
        if not (
            intent.get("requestLanguage")
            or intent.get("genres")
            or intent.get("country")
            or intent.get("cleaned")
        ):
            return None
        personalised = bool(
            guard.get("personalise") or intent.get("personalisedRecommendation")
        )
        meaningful = str(intent.get("cleaned") or "").strip()
        return {
            "intent": "recommendation" if personalised else "catalogue_query",
            "contentType": content_type,
            "filters": filters,
            "semanticQuery": meaningful or None,
            "personalise": personalised,
            "sort": "personalised" if personalised else "relevance",
            "limit": limit,
            "aggregation": None,
            "confidence": 0.98,
        }

    def validate_plan(self, raw_plan: dict[str, Any]) -> dict[str, Any]:
        now_year = datetime.now(timezone.utc).year
        intent = str(raw_plan.get("intent") or "unknown").strip().lower()
        if intent not in self.ALLOWED_INTENTS:
            intent = "unknown"
        content_type = self._string(raw_plan.get("contentType"))
        if content_type not in {"movie", "tv"}:
            content_type = None
        raw_filters = dict(raw_plan.get("filters") or {})
        release_period = self._string(raw_filters.get("releasePeriod"))
        if release_period not in self.ALLOWED_PERIODS:
            release_period = None
        filters = {
            "titleContains": self._string(raw_filters.get("titleContains")),
            "titleEquals": self._string(raw_filters.get("titleEquals")),
            "genres": [
                str(value).strip()
                for value in list(raw_filters.get("genres") or [])[:10]
                if str(value).strip()
            ],
            "people": [
                str(value).strip()
                for value in list(raw_filters.get("people") or [])[:10]
                if str(value).strip()
            ],
            "languages": [
                str(value).strip()
                for value in list(raw_filters.get("languages") or [])[:10]
                if str(value).strip()
            ],
            "countries": [
                str(value).strip().upper()
                for value in list(raw_filters.get("countries") or [])[:10]
                if str(value).strip()
            ],
            "releasePeriod": release_period,
            "releaseYear": self._integer(
                raw_filters.get("releaseYear"),
                minimum=1888,
                maximum=now_year + 5,
            ),
            "releaseYearMin": self._integer(
                raw_filters.get("releaseYearMin"),
                minimum=1888,
                maximum=now_year + 5,
            ),
            "releaseYearMax": self._integer(
                raw_filters.get("releaseYearMax"),
                minimum=1888,
                maximum=now_year + 5,
            ),
            "ratingMin": self._number(
                raw_filters.get("ratingMin"), minimum=0, maximum=10
            ),
            "runtimeMax": self._integer(
                raw_filters.get("runtimeMax"), minimum=1, maximum=1000
            ),
        }
        sort = str(raw_plan.get("sort") or "relevance").strip().lower()
        if sort not in self.ALLOWED_SORTS:
            sort = "relevance"
        personalise = bool(raw_plan.get("personalise"))
        if personalise:
            intent, sort = "recommendation", "personalised"
        aggregation = raw_plan.get("aggregation")
        safe_aggregation: dict[str, str] | None = None
        if isinstance(aggregation, dict):
            measure = str(aggregation.get("measure") or "")
            group_by = str(aggregation.get("groupBy") or "")
            if measure in self.ALLOWED_MEASURES and group_by in self.ALLOWED_GROUPS:
                safe_aggregation = {"measure": measure, "groupBy": group_by}
                intent = "catalogue_analytics"
        plan = {
            "intent": intent,
            "contentType": content_type,
            "filters": filters,
            "semanticQuery": self._string(raw_plan.get("semanticQuery")),
            "personalise": personalise,
            "sort": sort,
            "limit": min(max(int(raw_plan.get("limit") or 12), 1), 50),
            "aggregation": safe_aggregation,
            "confidence": self._number(
                raw_plan.get("confidence"), minimum=0, maximum=1
            )
            or 0.0,
            "plannerVersion": self.settings.assistant_planner_version,
            "toolSchemaVersion": self.settings.assistant_tool_schema_version,
        }
        signature_payload = {key: value for key, value in plan.items() if key != "confidence"}
        plan["planSignature"] = hashlib.sha256(
            json.dumps(signature_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]
        return plan

    def _merge_with_deterministic_constraints(
        self,
        plan: dict[str, Any],
        deterministic: dict[str, Any] | None,
        guard: dict[str, Any],
    ) -> dict[str, Any]:
        if deterministic is None:
            return plan
        # Exact field, temporal and aggregate constraints are authoritative.
        # The model is useful for semantic interpretation but cannot loosen
        # facts directly stated in the user's request.
        if guard.get("fieldConstraint") or guard.get("topRated") or guard.get(
            "releasePeriod"
        ) or guard.get("releaseYear") or guard.get("intentFamily") == "analytics":
            return self.validate_plan(deterministic)
        merged = dict(plan)
        if deterministic.get("contentType"):
            merged["contentType"] = deterministic["contentType"]
        if guard.get("personalise"):
            merged["intent"] = "recommendation"
            merged["personalise"] = True
            merged["sort"] = "personalised"
        elif guard.get("intentFamily") == "discovery":
            merged["intent"] = "catalogue_query"
            merged["personalise"] = False
            if merged.get("sort") == "personalised":
                merged["sort"] = "relevance"
        if not merged.get("semanticQuery") and deterministic.get("semanticQuery"):
            merged["semanticQuery"] = deterministic["semanticQuery"]
        merged["limit"] = deterministic.get("limit") or merged.get("limit")
        deterministic_filters = dict(deterministic.get("filters") or {})
        merged_filters = dict(merged.get("filters") or {})
        for field in ("genres", "countries"):
            if deterministic_filters.get(field):
                merged_filters[field] = deterministic_filters[field]
        merged["filters"] = merged_filters
        return self.validate_plan(merged)

    @staticmethod
    def _semantic_eligible(plan: dict[str, Any], guard: dict[str, Any]) -> bool:
        return bool(
            plan.get("intent")
            in {
                "catalogue_query",
                "recommendation",
                "similarity",
                "catalogue_analytics",
            }
            and bool(str(plan.get("semanticQuery") or "").strip())
            and not guard.get("contextual")
            and float(plan.get("confidence") or 0) >= 0.85
        )

    async def plan(
        self,
        user_message: str,
        *,
        catalogue_intent: dict[str, Any],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        guard = self._guard_for_query(user_message, catalogue_intent)
        cache_errors: list[str] = []
        deterministic = self._deterministic_plan(
            user_message, catalogue_intent, guard
        )
        deterministic_validated = (
            self.validate_plan(deterministic) if deterministic is not None else None
        )

        if self.settings.plan_cache_enabled:
            try:
                exact = await asyncio.to_thread(
                    self.catalogue.get_cached_plan_exact, user_message
                )
                if exact:
                    plan = self.validate_plan(dict(exact.get("planTemplate") or {}))
                    return {
                        "plan": plan,
                        "trace": {
                            "planSource": "exact_cache",
                            "planCacheHit": True,
                            "planCacheHitType": "exact",
                            "matchedQuery": exact.get("normalisedQuery"),
                            "cacheDocumentId": exact.get("cacheDocumentId"),
                            "plannerLlmInvoked": False,

                            "planValidation": "passed",
                            "elapsedMs": round(
                                (time.perf_counter() - started) * 1000, 1
                            ),
                        },
                    }
            except Exception as exc:
                cache_errors.append(f"exact: {type(exc).__name__}: {exc}")

            should_probe_semantic_cache = bool(
                self.settings.plan_cache_semantic_enabled
                and not guard.get("contextual")
                and (
                    deterministic_validated is None
                    or self._semantic_eligible(deterministic_validated, guard)
                )
            )
            if should_probe_semantic_cache:
                try:
                    candidates = await asyncio.wait_for(
                        asyncio.to_thread(
                            self.catalogue.find_cached_plans_semantic,
                            user_message,
                            limit=self.settings.plan_cache_candidate_limit,
                        ),
                        timeout=self.settings.plan_cache_semantic_probe_timeout_seconds,
                    )
                    for candidate in candidates:
                        similarity = float(candidate.get("similarity") or 0)
                        if similarity < self.settings.plan_cache_semantic_threshold:
                            continue
                        if not self._guards_compatible(
                            guard, dict(candidate.get("guard") or {})
                        ):
                            continue
                        rebound = self._rebind_cached_plan(
                            dict(candidate.get("planTemplate") or {}),
                            user_message=user_message,
                            intent=catalogue_intent,
                            guard=guard,
                        )
                        plan = self.validate_plan(rebound)
                        await asyncio.to_thread(
                            self.catalogue.mark_plan_cache_hit,
                            str(candidate.get("cacheDocumentId") or ""),
                        )
                        return {
                            "plan": plan,
                            "trace": {
                                "planSource": "semantic_cache",
                                "planCacheHit": True,
                                "planCacheHitType": "semantic",
                                "matchedQuery": candidate.get("normalisedQuery"),
                                "similarity": round(similarity, 4),
                                "cacheDocumentId": candidate.get("cacheDocumentId"),
                                "plannerLlmInvoked": False,

                                "planValidation": "passed_after_slot_rebind",
                                **dict(candidate.get("embeddingTrace") or {}),
                                "elapsedMs": round(
                                    (time.perf_counter() - started) * 1000, 1
                                ),
                            },
                        }
                except asyncio.TimeoutError:
                    cache_errors.append(
                        "semantic: probe budget exceeded; continued without blocking"
                    )
                except Exception as exc:
                    cache_errors.append(f"semantic: {type(exc).__name__}: {exc}")

        source = "deterministic"
        usage: dict[str, Any] = {}
        raw_plan: dict[str, Any] | None = deterministic
        model_error: str | None = None
        should_use_model = bool(
            self.settings.assistant_planner_model_enabled
            and self.settings.chat_provider != "disabled"
            and deterministic is None
        )
        if should_use_model:
            try:
                raw_plan = await asyncio.wait_for(
                    self.model.plan_catalogue_request(
                        user_message, usage_out=usage
                    ),
                    timeout=self.settings.assistant_planner_timeout_seconds,
                )
                source = "llm"
            except asyncio.TimeoutError:
                model_error = (
                    "planner time budget exceeded; used deterministic fallback"
                )
                raw_plan = deterministic
                source = "deterministic_fallback"
            except Exception as exc:
                model_error = f"{type(exc).__name__}: {exc}"
                raw_plan = deterministic
                source = "deterministic_fallback"
        if raw_plan is None:
            raw_plan = {
                "intent": "unknown",
                "filters": {},
                "limit": 12,
                "confidence": 0,
            }
        plan = self.validate_plan(raw_plan)
        plan = self._merge_with_deterministic_constraints(
            plan, deterministic, guard
        )
        cache_write: dict[str, Any] = {"stored": False}
        semantic_eligible = self._semantic_eligible(plan, guard)
        if (
            plan.get("intent") != "unknown"
            and float(plan.get("confidence") or 0) >= 0.8
            and self.settings.plan_cache_enabled
        ):
            try:
                cache_write = await asyncio.to_thread(
                    self.catalogue.cache_assistant_plan,
                    query=user_message,
                    plan=plan,
                    guard=guard,
                    source=source,
                    # Store the exact KV plan synchronously. Semantic
                    # vectorisation is promoted in the background after the
                    # fresh catalogue execution so it cannot delay this turn.
                    semantic_eligible=False,
                )
            except Exception as exc:
                cache_errors.append(f"write: {type(exc).__name__}: {exc}")
        llm_invoked = should_use_model
        cache_promotion = (
            {
                "query": user_message,
                "plan": plan,
                "guard": guard,
                "source": source,
            }
            if semantic_eligible
            and bool(cache_write.get("stored"))
            and self.settings.plan_cache_semantic_enabled
            else None
        )
        return {
            "plan": plan,
            "cachePromotion": cache_promotion,
            "trace": {
                "planSource": source,
                "planCacheHit": False,
                "planCacheHitType": None,
                "plannerLlmInvoked": llm_invoked,
                "plannerLlmSucceeded": source == "llm",
                "plannerLlmFailed": bool(should_use_model and source != "llm"),
                "plannerUsage": usage,
                "planValidation": "passed",
                "cacheWrite": cache_write,
                "cacheErrors": cache_errors,
                "plannerModelError": model_error,
                "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
            },
        }
