from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import Settings
from .ai_enrichment import current_guide
from .providers import ChatProvider


class LocalModelService:
    """Fast deterministic intent/fact handling plus an optional local LLM.

    Catalogue recommendations, profile questions and history questions are
    rendered from authoritative application data. The model may help interpret
    language and create embeddings, but it is never a viewer-facing source of
    catalogue, profile, history, entitlement or availability facts.
    """

    DATABASE_BOUNDARY_REPLY = (
        "I can only answer using verified information from the current catalogue "
        "and your viewer data. I do not have database evidence for that request. "
        "Try asking about available titles, catalogue ratings, your preferences, "
        "watch history or subscription availability."
    )

    GENRES = {
        "science fiction": "Science Fiction",
        "sci fi": "Science Fiction",
        "sci-fi": "Science Fiction",
        "thriller": "Thriller",
        "crime": "Crime",
        "comedy": "Comedy",
        "drama": "Drama",
        "documentary": "Documentary",
        "animation": "Animation",
        "fantasy": "Fantasy",
        "horror": "Horror",
        "romance": "Romance",
        "mystery": "Mystery",
        "action": "Action",
        "adventure": "Adventure",
        "family": "Family",
        "children": "Family",
        "kids": "Family",
        "history": "History",
        "music": "Music",
        "war": "War",
        "western": "Western",
    }

    TEMPORARY_TERMS = ("tonight", "right now", "this evening", "today", "for now")

    # These are preference/safety concepts, not catalogue genres. They are
    # deliberately collapsed into one durable safety signal so natural phrases
    # such as "sex, romance and adult content" do not become literal searches.
    ADULT_CONTENT_TERMS = (
        "adult content",
        "adult movies",
        "adult films",
        "18 plus content",
        "18+ content",
        "sexual content",
        "sex scenes",
        "sex scene",
        "explicit sex",
        "sex",
        "sexual",
        "sexually explicit",
        "erotic content",
        "erotica",
        "erotic",
        "pornographic content",
        "pornography",
        "pornographic",
        "nudity",
        "nude scenes",
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._chat_provider = ChatProvider(settings)

    def close(self) -> None:
        return None

    async def plan_catalogue_request(
        self,
        user_message: str,
        *,
        usage_out: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Use the configured model only as a constrained language planner.

        The model sees no catalogue records and cannot execute a tool. Its JSON
        output is validated again by the application before it can reach the
        data plane.
        """
        system = (
            "You are a query planner for a governed film and television catalogue. "
            "Return exactly one JSON object and no prose. Do not name any film or "
            "television title unless the user supplied that title. Do not answer the "
            "question. Translate the language into this schema:\n"
            "{"
            '"intent":"catalogue_query|recommendation|similarity|catalogue_analytics|unknown",'
            '"contentType":"movie|tv|null",'
            '"filters":{"titleContains":"string|null","titleEquals":"string|null",'
            '"genres":["string"],"people":["string"],"languages":["string"],'
            '"countries":["string"],"releasePeriod":"LAST_YEAR|THIS_YEAR|RECENT|null",'
            '"releaseYear":"integer|null","releaseYearMin":"integer|null",'
            '"releaseYearMax":"integer|null","ratingMin":"number|null",'
            '"runtimeMax":"integer|null"},'
            '"semanticQuery":"string|null","personalise":"boolean",'
            '"sort":"relevance|rating_desc|popularity_desc|release_year_desc|personalised",'
            '"limit":"integer 1..20",'
            '"aggregation":{"measure":"count|average_rating|average_runtime",'
            '"groupBy":"genre|release_year|original_language|country|content_type"}|null,'
            '"confidence":"number 0..1"'
            "}.\n"
            "Rules: title/name contains wording must use filters.titleContains and "
            "must not infer a genre. 'Last year' and 'this year' remain symbolic "
            "releasePeriod values. 'New/latest' means RECENT. 'Will I like' or "
            "'for me' sets personalise=true. Top/highest/best rated sets "
            "sort=rating_desc. Preserve spelling-corrected semantic concepts such "
            "as sequel. Exact counts and grouped questions use catalogue_analytics."
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ]
        parts: list[str] = []
        async for token in self._chat_provider.stream(messages, usage_out):
            parts.append(token)
        raw = "".join(parts).strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
        candidate = fenced.group(1) if fenced else raw
        if not candidate.startswith("{"):
            start, end = candidate.find("{"), candidate.rfind("}")
            candidate = candidate[start : end + 1] if start >= 0 and end > start else ""
        parsed = json.loads(candidate)
        if not isinstance(parsed, dict):
            raise ValueError("Planner model did not return a JSON object")
        return parsed

    @staticmethod
    def estimate_tokens(value: Any) -> int:
        """Portable token estimate used when a runtime does not return usage.

        Ollama reports exact prompt/eval counts on completed generations. For
        deterministic paths there is no model call, so the demo uses the common
        four-characters-per-token approximation and labels it as estimated.
        """
        if value is None:
            return 0
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        text = value.strip()
        return max(1, round(len(text) / 4)) if text else 0

    def estimate_prompt_tokens(
        self,
        *,
        user_message: str,
        short_term_context: list[dict[str, Any]],
        long_term_context: list[dict[str, Any]],
        current_turn_facts: list[str] | None = None,
        catalogue_candidates: list[dict[str, Any]] | None = None,
        retrieval_trace: dict[str, Any] | None = None,
    ) -> int:
        messages = self._build_messages(
            user_message=user_message,
            short_term_context=short_term_context,
            long_term_context=long_term_context,
            current_turn_facts=current_turn_facts or [],
            catalogue_candidates=catalogue_candidates or [],
            retrieval_trace=retrieval_trace or {},
            system_prompt=None,
        )
        # Small per-message overhead approximates role and framing tokens.
        return sum(self.estimate_tokens(message.get("content", "")) + 4 for message in messages)

    def estimate_context_tokens(
        self, short_term_context: list[dict[str, Any]], long_term_context: list[dict[str, Any]]
    ) -> int:
        return self.estimate_tokens({"short": short_term_context, "long": long_term_context})

    def health(self) -> dict[str, Any]:
        result = self._chat_provider.health()
        result.update({"catalogue_grounding": "server_enforced"})
        return result

    @staticmethod
    def _normalise_genre_terms(value: str) -> str:
        lower = re.sub(r"\s+", " ", value.lower().strip())
        replacements = {
            "comedies": "comedy",
            "documentaries": "documentary",
            "fantasies": "fantasy",
            "mysteries": "mystery",
            "kid friendly": "family",
            "child friendly": "family",
            "children's": "family",
            "childrens": "family",
            "children": "family",
            "kids": "family",
            "families": "family",
            "histories": "history",
            "thrillers": "thriller",
            "romances": "romance",
            "dramas": "drama",
            "westerns": "western",
            "adventures": "adventure",
            "musicals": "music",
        }
        for source, target in replacements.items():
            lower = re.sub(rf"\b{source}\b", target, lower)
        return lower

    @classmethod
    def _genre_for(cls, value: str) -> str | None:
        lower = cls._normalise_genre_terms(value)
        for token, canonical in cls.GENRES.items():
            if lower == token or re.search(rf"\b{re.escape(token)}\b", lower):
                return canonical
        return None

    @staticmethod
    def _clean_theme(value: str) -> str:
        text = re.sub(
            r"\b(?:movies?|films?|series|shows?|television|tv|content|stories|programmes?)\b",
            " ",
            value,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\b(?:really|generally|usually|mostly|some|more)\b", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"[^A-Za-z0-9 '&-]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip(" .,-")
        return text.title()

    @staticmethod
    def _normalised_question(user_message: str) -> str:
        lower = re.sub(r"[^a-z0-9' ]+", " ", user_message.lower())
        return re.sub(r"\s+", " ", lower).strip()

    @staticmethod
    def is_recommendation_explanation_question(user_message: str) -> bool:
        lower = LocalModelService._normalised_question(user_message)
        patterns = (
            r"^(?:why|how) (?:did|do|could|would) you (?:recommend|suggest|show|pick)",
            r"^(?:why|how) (?:was|were) .+ (?:recommend|suggest|show|pick)",
            r"\bwhy (?:am i|was i) (?:shown|recommended)\b",
        )
        return any(re.search(pattern, lower) for pattern in patterns)

    @staticmethod
    def is_catalogue_result_complaint(user_message: str) -> bool:
        lower = LocalModelService._normalised_question(user_message)
        patterns = (
            r"^(?:these|those|the results|your suggestions|your recommendations) (?:do not|don't|dont|aren't|are not) (?:look|seem|feel|match|fit)",
            r"^(?:these|those|the results) (?:are|look|seem) (?:wrong|irrelevant|unrelated|incorrect)",
            r"^(?:none of these|none of those) (?:match|fit|are relevant|look right)",
            r"^(?:try again|search again|rerun it|redo the search)$",
            r"\b(?:do not|don't|dont|aren't|are not) look like .+ (?:movies|films|series|shows)\b",
        )
        return any(re.search(pattern, lower) for pattern in patterns)

    @staticmethod
    def should_capture_preference(user_message: str) -> bool:
        text = user_message.strip()
        lower = LocalModelService._normalised_question(text)
        if (
            not text
            or LocalModelService.is_recommendation_explanation_question(text)
            or LocalModelService.is_catalogue_result_complaint(text)
        ):
            return False
        explicit_preference = bool(
            re.match(
                r"^(?:"
                r"i\s+(?:(?:really|generally|usually|only)\s+)*"
                r"(?:like|love|enjoy|prefer|dislike|hate|avoid|do not like|don't like|dont like|want to avoid|am a fan of|am not a fan of)"
                r"|my\s+(?:favourite|favorite)\s+(?:director|actor)\s+is"
                r")\b",
                lower,
            )
        )
        if explicit_preference:
            return True
        if text.endswith("?") or re.match(
            r"^(?:what|why|how|when|where|which|who|do|did|does|can|could|would|will|is|are)\b",
            lower,
        ):
            return False
        return False

    @staticmethod
    def _preference_sentiment(verb: str) -> str:
        lower = verb.lower().strip()
        return (
            "dislike"
            if any(
                token in lower
                for token in (
                    "do not",
                    "don't",
                    "dont",
                    "dislike",
                    "hate",
                    "avoid",
                    "not a fan",
                    "no longer",
                )
            )
            else "like"
        )

    @classmethod
    def _preference_clauses(cls, value: str) -> list[tuple[str, str]]:
        """Return sentiment-scoped preference clauses.

        Negative scope intentionally continues across commas and conjunctions.
        For example, ``I do not like sex, romance, adult content movies`` is
        one negative clause rather than a negative ``sex`` clause followed by a
        falsely positive ``romance`` mention.
        """
        pattern = re.compile(
            r"(?:^|[.!?;]\s*|\bbut\s+|\bwhereas\s+|\bwhile\s+|\band\s+)"
            r"(?:i\s+)?(?:(?:really|generally|usually|only)\s+)*"
            r"(?P<verb>do not like|don't like|dont like|no longer like|want to avoid|"
            r"am not a fan of|dislike|hate|avoid|am a fan of|like|love|enjoy|prefer)\s+",
            flags=re.IGNORECASE,
        )
        matches = list(pattern.finditer(value))
        clauses: list[tuple[str, str]] = []
        for index, match in enumerate(matches):
            subject_end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
            subject = value[match.end():subject_end].strip(" ,.;:!?-")
            if subject:
                clauses.append((cls._preference_sentiment(match.group("verb")), subject))
        return clauses

    @classmethod
    def _contains_adult_content_term(cls, value: str) -> bool:
        lower = cls._normalised_question(value)
        return any(re.search(rf"\b{re.escape(term)}\b", lower) for term in cls.ADULT_CONTENT_TERMS)

    @classmethod
    def is_preference_only_statement(cls, user_message: str) -> bool:
        """True when the turn updates taste/safety but asks for no results."""
        if not cls.should_capture_preference(user_message):
            return False
        return not (
            cls.extract_recommendation_clause(user_message)
            or cls.is_recommendation_request(user_message)
            or cls.extract_similar_title_request(user_message)
            or cls.is_watch_history_question(user_message)
            or cls.is_profile_question(user_message)
            or cls.is_likes_dislikes_question(user_message)
        )

    def extract_preferences_fast(self, user_message: str) -> list[dict[str, Any]]:
        """Extract explicit durable preferences without another model call.

        Preference verbs establish sentiment scope. Catalogue words within that
        scope are facts to save, not browse instructions. A separate explicit
        recommendation clause is removed before extraction.
        """
        text = user_message.strip()
        if not text or not self.should_capture_preference(text):
            return []

        recommendation_clause = self.extract_recommendation_clause(text)
        if recommendation_clause:
            offset = text.lower().rfind(recommendation_clause.lower())
            if offset > 0:
                text = text[:offset].rstrip(" ,.;:!?-")
        lower = self._normalise_genre_terms(text)
        if not text or any(token in lower for token in self.TEMPORARY_TERMS):
            return []

        preferences: list[dict[str, Any]] = []
        clauses = self._preference_clauses(text)
        exclusive_content_preference = bool(
            re.search(
                r"\bi\s+(?:(?:really|generally|usually)\s+)*only\s+(?:like|love|enjoy|prefer)\b",
                lower,
            )
        )

        for clause_sentiment, subject in clauses:
            subject_lower = self._normalise_genre_terms(subject)
            matched_tokens: set[str] = set()

            # Every genre in the clause inherits the clause sentiment. This is
            # what preserves "do not like" across comma-separated lists.
            for token, canonical in self.GENRES.items():
                if not re.search(rf"\b{re.escape(token)}\b", subject_lower):
                    continue
                matched_tokens.add(token)
                exclusive = bool(exclusive_content_preference and clause_sentiment == "like")
                verb = (
                    "dislikes"
                    if clause_sentiment == "dislike"
                    else "exclusively prefers" if exclusive else "prefers"
                )
                item = {
                    "kind": "genre",
                    "value": canonical,
                    "sentiment": clause_sentiment,
                    "fact": f"Viewer {verb} {canonical} content.",
                    "source": "explicit_viewer_statement",
                }
                if exclusive:
                    item["exclusive"] = True
                preferences.append(item)

            if self._contains_adult_content_term(subject):
                if clause_sentiment == "dislike":
                    preferences.append(
                        {
                            "kind": "safety",
                            "value": "adult_content",
                            "sentiment": "avoid",
                            "fact": "Viewer prefers to avoid sexual and adult content.",
                            "source": "explicit_viewer_statement",
                        }
                    )
                else:
                    # A later positive statement explicitly removes the safety
                    # exclusion instead of silently creating a catalogue genre.
                    preferences.append(
                        {
                            "kind": "safety",
                            "value": "adult_content",
                            "sentiment": "remove",
                            "fact": "Viewer no longer asks to exclude sexual and adult content.",
                            "source": "explicit_viewer_statement",
                        }
                    )

            # Preserve useful free-form themes left after known genre and safety
            # concepts have been removed. Parse list items independently so a
            # whole comma-separated sentence never becomes one malformed theme.
            # Use the normalised subject so aliases such as children/kids →
            # Family do not also leak through as a second free-form theme.
            residual = subject_lower
            for token in sorted(matched_tokens, key=len, reverse=True):
                residual = re.sub(rf"\b{re.escape(token)}s?\b", " ", residual, flags=re.IGNORECASE)
            for term in sorted(self.ADULT_CONTENT_TERMS, key=len, reverse=True):
                residual = re.sub(rf"\b{re.escape(term)}\b", " ", residual, flags=re.IGNORECASE)
            for segment in re.split(r"\s*(?:,|/|&|\band\b)\s*", residual, flags=re.IGNORECASE):
                theme = self._clean_theme(segment)
                if not theme or len(theme.split()) > 6 or self._genre_for(theme):
                    continue
                exclusive = bool(exclusive_content_preference and clause_sentiment == "like")
                verb = (
                    "dislikes"
                    if clause_sentiment == "dislike"
                    else "exclusively prefers" if exclusive else "prefers"
                )
                item = {
                    "kind": "theme",
                    "value": theme,
                    "sentiment": clause_sentiment,
                    "fact": f"Viewer {verb} {theme.lower()}-themed content.",
                    "source": "explicit_viewer_statement",
                }
                if exclusive:
                    item["exclusive"] = True
                preferences.append(item)

        negative = any(sentiment == "dislike" for sentiment, _ in clauses)
        overall_sentiment = "dislike" if negative else "like"

        person_match = re.search(
            r"\b(?:i\s+(?:really\s+)?(?:like|love|enjoy|prefer|dislike|hate)|"
            r"my\s+(?:favourite|favorite)\s+(?:director|actor)\s+is)\s+"
            r"([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){1,3})",
            text,
        )
        if person_match:
            person = person_match.group(1).strip()
            preferences.append(
                {
                    "kind": "person",
                    "value": person,
                    "sentiment": overall_sentiment,
                    "fact": f"Viewer {'dislikes' if overall_sentiment == 'dislike' else 'likes'} titles involving {person}.",
                    "source": "explicit_viewer_statement",
                }
            )

        language_match = re.search(
            r"\b(?:i\s+prefer|i\s+like|i\s+usually\s+watch)\s+"
            r"(English|French|Spanish|German|Italian|Korean|Japanese|Hindi|Tamil|Telugu|Malayalam|Bengali)"
            r"(?:-language)?\s+(?:films|movies|series|shows|content)",
            text,
            re.IGNORECASE,
        )
        if language_match:
            language = language_match.group(1).title()
            preferences.append(
                {
                    "kind": "language",
                    "value": language,
                    "sentiment": "like",
                    "fact": f"Viewer prefers {language}-language content.",
                    "source": "explicit_viewer_statement",
                }
            )

        if re.search(r"\b(?:avoid|do not like|don't like|dont like|do not show|don't show|no)\b[^.?!]{0,60}\bgraphic violence\b", lower):
            preferences.append(
                {
                    "kind": "safety",
                    "value": "graphic_violence",
                    "sentiment": "avoid",
                    "fact": "Viewer prefers to avoid graphic violence.",
                    "source": "explicit_viewer_statement",
                }
            )

        runtime = re.search(r"\b(?:under|less than|max(?:imum)? of)\s*(\d{2,3})\s*(?:minutes|mins)\b", lower)
        if runtime:
            minutes = int(runtime.group(1))
            preferences.append(
                {
                    "kind": "runtime",
                    "value": minutes,
                    "sentiment": "like",
                    "fact": f"Viewer generally prefers titles under {minutes} minutes.",
                    "source": "explicit_viewer_statement",
                }
            )

        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for pref in preferences:
            key = (str(pref["kind"]), str(pref["value"]).lower(), str(pref["sentiment"]))
            if key not in seen:
                seen.add(key)
                deduped.append(pref)
        return deduped[:10]

    def extract_facts_fast(self, user_message: str) -> list[str]:
        return [str(item["fact"]) for item in self.extract_preferences_fast(user_message)]

    @staticmethod
    def is_watch_history_question(user_message: str) -> bool:
        lower = user_message.strip().lower()
        patterns = (
            r"\bwhat (?:films|movies|series|shows|titles) have i (?:watched|seen)\b",
            r"\bwhat have i (?:watched|seen)\b",
            r"\bshow (?:me )?my watch history\b",
            r"\bmy watch history\b",
            r"\bwhich (?:films|movies|series|shows|titles) have i (?:watched|seen)\b",
            r"\bdo you know what i have (?:watched|seen)\b",
            r"\bwhat did i (?:watch|see)\b",
        )
        return any(re.search(pattern, lower) for pattern in patterns)

    @staticmethod
    def is_watch_history_count_question(user_message: str) -> bool:
        lower = LocalModelService._normalised_question(user_message)
        patterns = (
            r"how many (?:movies|films|series|shows|titles) have i (?:watched|seen)",
            r"how many (?:movies|films|series|shows|titles) did i (?:watch|see)",
            r"what is my (?:movie|film|series|show|title) watch count",
            r"count (?:the )?(?:movies|films|series|shows|titles) i have (?:watched|seen)",
        )
        return any(re.fullmatch(pattern, lower) for pattern in patterns)

    @classmethod
    def extract_catalogue_count_question(cls, user_message: str) -> dict[str, str | None] | None:
        """Extract catalogue inventory-count questions before browse routing.

        Examples include ``How many movies are in the catalogue?`` and
        ``How many comedy movies are there?``. The result contains only
        structured filters and is safe to execute as an aggregate query.
        """
        lower = cls._normalised_question(user_message)
        patterns = (
            r"how many (?P<subject>.+?) (?:are there|are available|are|exist)(?: in| on)? (?:the )?(?:catalogue|catalog|service)?",
            r"how many (?P<subject>.+?) do (?:you|we) have(?: (?:in|on) (?:the )?(?:catalogue|catalog|service))?",
            r"(?:what is|what's) the (?:total |number |count )?of (?P<subject>.+?)(?: in| on) (?:the )?(?:catalogue|catalog|service)",
            r"count (?:the )?(?P<subject>.+?)(?: in| on) (?:the )?(?:catalogue|catalog|service)",
        )
        subject = None
        for pattern in patterns:
            match = re.fullmatch(pattern, lower)
            if match:
                subject = str(match.group("subject") or "").strip()
                break
        if not subject:
            return None
        content_type = cls.history_content_type(subject)
        genre = cls._genre_for(subject)
        # A generic noun such as "movies" is not a genre filter.
        cleaned = re.sub(r"\b(?:available|total|all|number of|count of)\b", " ", subject)
        cleaned = re.sub(r"\b(?:movies?|films?|series|shows?|titles?|programmes?|content)\b", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned and genre is None:
            # Do not silently turn arbitrary adjectives into fuzzy searches.
            return None
        return {
            "contentType": content_type,
            "genre": genre,
            "subject": subject,
        }

    @classmethod
    def extract_catalogue_analytics_request(
        cls, user_message: str
    ) -> dict[str, Any] | None:
        """Parse bounded catalogue aggregation questions into an allowlisted plan."""
        lower = cls._normalised_question(user_message)
        dimension_aliases = {
            "genre": "genre",
            "genres": "genre",
            "year": "release_year",
            "release year": "release_year",
            "release years": "release_year",
            "language": "original_language",
            "languages": "original_language",
            "original language": "original_language",
            "country": "country",
            "countries": "country",
            "content type": "content_type",
            "type": "content_type",
            "movie or series": "content_type",
        }
        measure = None
        if re.search(r"\b(?:average|avg|mean) (?:viewer )?rating\b", lower):
            measure = "average_rating"
        elif re.search(r"\b(?:average|avg|mean) runtime\b", lower):
            measure = "average_runtime"
        elif re.search(r"\b(?:how many|count|number|breakdown|distribution|most common)\b", lower):
            measure = "count"
        if not measure:
            return None

        dimension = None
        group_match = re.search(
            r"\b(?:by|per|for each|for every|in each|in every|"
            r"grouped by|broken down by)\s+(?:of\s+the\s+|the\s+)?"
            r"(?P<dimension>release years?|original languages?|content type|"
            r"movie or series|genres?|years?|languages?|countries?|type)\b",
            lower,
        )
        if group_match:
            dimension = dimension_aliases.get(group_match.group("dimension"))
        elif re.search(
            r"\b(?:each|every)\s+(?:of\s+the\s+|the\s+)?"
            r"(?P<dimension>release years?|original languages?|content type|"
            r"genres?|years?|languages?|countries?|type)\b",
            lower,
        ):
            token = re.search(
                r"\b(?:each|every)\s+(?:of\s+the\s+|the\s+)?"
                r"(?P<dimension>release years?|original languages?|content type|"
                r"genres?|years?|languages?|countries?|type)\b",
                lower,
            )
            dimension = (
                dimension_aliases.get(token.group("dimension")) if token else None
            )
        elif re.search(
            r"\b(?P<dimension>genre|year|language|country|content type) wise\b",
            lower,
        ):
            token = re.search(
                r"\b(?P<dimension>genre|year|language|country|content type) wise\b",
                lower,
            )
            dimension = (
                dimension_aliases.get(token.group("dimension")) if token else None
            )
        elif re.search(
            r"\b(?:genre|language|country|year|content type) "
            r"(?:breakdown|distribution|counts?)\b",
            lower,
        ):
            token = re.search(
                r"\b(?P<dimension>genre|language|country|year|content type) "
                r"(?:breakdown|distribution|counts?)\b",
                lower,
            )
            dimension = dimension_aliases.get(token.group("dimension")) if token else None
        if not dimension:
            return None

        content_type = cls.history_content_type(lower)
        year_match = re.search(r"\b(?:released in|from|in the year)\s+((?:19|20)\d{2})\b", lower)
        genre = cls._genre_for(lower)
        return {
            "measure": measure,
            "dimension": dimension,
            "filters": {
                "contentType": content_type,
                "genre": genre,
                "releaseYear": int(year_match.group(1)) if year_match else None,
            },
            "limit": 30,
            "order": "desc",
        }

    @staticmethod
    def is_region_question(user_message: str) -> bool:
        lower = LocalModelService._normalised_question(user_message)
        patterns = (
            r"what region do i live in",
            r"which region do i live in",
            r"what is my region",
            r"what's my region",
            r"which region is on my profile",
            r"what region is my account set to",
        )
        return any(re.fullmatch(pattern, lower) for pattern in patterns)

    @staticmethod
    def _normalise_region_code(value: str) -> str | None:
        cleaned = re.sub(r"[^a-zA-Z ]+", " ", value).strip().lower()
        cleaned = re.sub(r"\s+", " ", cleaned)
        aliases = {
            "us": "US", "usa": "US", "united states": "US", "united states of america": "US",
            "uk": "GB", "gb": "GB", "great britain": "GB", "united kingdom": "GB",
            "ireland": "IE", "ie": "IE", "canada": "CA", "ca": "CA",
            "australia": "AU", "au": "AU", "new zealand": "NZ", "nz": "NZ",
            "germany": "DE", "de": "DE", "france": "FR", "fr": "FR",
            "spain": "ES", "es": "ES", "italy": "IT", "it": "IT",
            "denmark": "DK", "dk": "DK", "netherlands": "NL", "nl": "NL",
        }
        if cleaned in aliases:
            return aliases[cleaned]
        compact = cleaned.replace(" ", "")
        if re.fullmatch(r"[a-z]{2}", compact):
            return compact.upper()
        return None

    @classmethod
    def extract_region_statement(cls, user_message: str) -> str | None:
        text = re.sub(r"\s+", " ", user_message.strip()).strip(" .!?")
        patterns = (
            r"^i (?:live|reside|am based) in (?:the )?(?P<region>.+?)(?: region)?$",
            r"^my region is (?:the )?(?P<region>.+?)$",
            r"^set my region to (?:the )?(?P<region>.+?)$",
            r"^i am in (?:the )?(?P<region>.+?)(?: region)?$",
        )
        for pattern in patterns:
            match = re.match(pattern, text, flags=re.IGNORECASE)
            if match:
                return cls._normalise_region_code(match.group("region"))
        return None

    @staticmethod
    def is_contextual_similarity_request(user_message: str) -> bool:
        lower = LocalModelService._normalised_question(user_message)
        patterns = (
            r"(?:suggest|recommend|show me|find me) similar (?:movies|films|series|shows|titles)",
            r"(?:suggest|recommend|show me|find me) something similar",
            r"more like (?:this|that|it|these|those|them)",
            r"similar (?:movies|films|series|shows|titles)",
        )
        return any(re.fullmatch(pattern, lower) for pattern in patterns)

    @classmethod
    def extract_contextual_request(cls, user_message: str) -> dict[str, Any] | None:
        """Resolve follow-ups that must stay inside an existing evidence set.

        This is intentionally deterministic.  References such as ``these`` and
        ``this list`` are control language, not Search query terms.  The router
        therefore returns a structured operation and lets the data plane apply
        it to exact title IDs from the immediately preceding grounded result.
        """
        lower = cls._normalised_question(user_message)

        if any(
            re.fullmatch(pattern, lower)
            for pattern in (
                r"what (?:question )?did i (?:just )?ask(?: you)?(?: before| previously| last)?",
                r"what was my (?:previous|last) question",
                r"what did i say (?:before|previously|last)",
                r"repeat my (?:previous|last) question",
            )
        ):
            return {"kind": "previous_question"}

        if any(
            re.fullmatch(pattern, lower)
            for pattern in (
                r"what (?:answer|response) did you (?:just )?give(?: me)?",
                r"what did you (?:just )?(?:say|answer|reply)",
                r"repeat your (?:previous|last) (?:answer|response)",
                r"what was your (?:previous|last) (?:answer|response)",
            )
        ):
            return {"kind": "previous_answer"}

        if any(
            re.fullmatch(pattern, lower)
            for pattern in (
                r"(?:repeat|show|list) (?:me )?(?:the )?(?:previous|last) "
                r"(?:results|recommendations|suggestions)(?: again)?",
                r"what did you (?:recommend|suggest)(?: to me)?(?: before|previously|last)?",
                r"(?:show|list) (?:me )?(?:those|these) "
                r"(?:results|recommendations|suggestions) again",
            )
        ):
            return {"kind": "repeat_previous_results"}

        if any(
            re.fullmatch(pattern, lower)
            for pattern in (
                r"(?:what|which) (?:movies|films|series|shows|titles) did (?:you|we|i) "
                r"(?:add|save|put) (?:to|on) my (?:watchlist|watch list|list)(?: recently)?",
                r"what did (?:you|we|i) (?:add|save|put) (?:to|on) my "
                r"(?:watchlist|watch list|list)(?: recently)?",
                r"(?:show|list) (?:me )?(?:the )?(?:recent|recently) "
                r"(?:movies|films|series|shows|titles) (?:added|saved) "
                r"(?:to|on) my (?:watchlist|watch list|list)",
            )
        ):
            return {"kind": "recent_my_list_additions"}

        if any(
            re.fullmatch(pattern, lower)
            for pattern in (
                r"(?:show|find|recommend|suggest)(?: me)? (?:some )?"
                r"(?:movies|films|series|shows|titles) (?:that are )?similar to "
                r"(?:the )?(?:ones|titles|movies|films|shows|series) "
                r"(?:in|on) my (?:watchlist|watch list|list)",
                r"(?:show|find|recommend|suggest)(?: me)? (?:something|more) "
                r"(?:like|similar to) (?:what is|what's|the titles) "
                r"(?:in|on) my (?:watchlist|watch list|list)",
                r"(?:movies|films|series|shows|titles) similar to "
                r"(?:my|the ones in my) (?:watchlist|watch list|list)",
            )
        ):
            return {"kind": "similar_to_my_list"}

        if any(
            re.fullmatch(pattern, lower)
            for pattern in (
                r"(?:what|which) (?:movies|films|series|shows|titles) (?:are|do i have) "
                r"(?:in|on) my (?:watchlist|watch list|list)",
                r"(?:show|list) (?:me )?(?:what is|what's|the titles|the movies|the films)? ?"
                r"(?:in|on) my (?:watchlist|watch list|list)",
                r"(?:show|open) my (?:watchlist|watch list|list)",
            )
        ):
            return {"kind": "current_my_list"}

        # Follow-up filters are an intersection with the prior result IDs. They
        # are never allowed to broaden into a new catalogue search.
        filter_patterns: tuple[tuple[str, tuple[str, ...]], ...] = (
            (
                "playable",
                (
                    r"(?:show|which|what)(?: me)? (?:of )?(?:the )?"
                    r"(?:ones|titles|movies|films|shows|series|these|those) "
                    r"(?:i can|can i) (?:watch|play)(?: from (?:this|that|the) list)?",
                    r"(?:show|which|what)(?: me)? (?:of )?(?:the )?"
                    r"(?:ones|titles|movies|films|shows|series|these|those) "
                    r"(?:are )?included in my (?:current )?plan(?: from (?:this|that|the) list)?",
                    r"(?:show|filter) (?:this|that|the|these|those) list "
                    r"(?:to|for) (?:titles )?(?:i can watch|included in my plan)",
                ),
            ),
            (
                "available",
                (
                    r"(?:show|which|what)(?: me)? (?:of )?(?:the )?"
                    r"(?:ones|titles|movies|films|shows|series|these|those) "
                    r"(?:are )?available (?:to|for) me(?: from (?:this|that|the) list)?",
                ),
            ),
            (
                "in_my_list",
                (
                    r"(?:which|what|show)(?: me)? (?:of )?(?:these|those|the ones|the titles) "
                    r"(?:are|is) (?:already )?(?:in|on) my (?:watchlist|watch list|list)",
                ),
            ),
            (
                "missing_from_my_list",
                (
                    r"(?:which|what|show)(?: me)? (?:of )?(?:these|those|the ones|the titles) "
                    r"(?:are|is) (?:missing|not|not already) (?:from|in|on) my "
                    r"(?:watchlist|watch list|list)",
                ),
            ),
            (
                "watched",
                (
                    r"(?:which|what|show)(?: me)? (?:of )?(?:these|those|the ones|the titles) "
                    r"(?:have i|i have) (?:watched|seen)",
                ),
            ),
            (
                "unwatched",
                (
                    r"(?:which|what|show)(?: me)? (?:of )?(?:these|those|the ones|the titles) "
                    r"(?:have i not|i have not|i haven't) (?:watched|seen)",
                    r"(?:show|which)(?: me)? (?:of )?(?:these|those|the ones|the titles) "
                    r"(?:are )?unseen",
                    r"(?:show|which)(?: me)? (?:the )?unseen "
                    r"(?:ones|titles|movies|films|shows|series)",
                ),
            ),
            (
                "liked",
                (
                    r"(?:which|what|show)(?: me)? (?:of )?(?:these|those|the ones|the titles) "
                    r"(?:have i|i have) liked",
                ),
            ),
            (
                "top_rated",
                (
                    r"(?:which|what) (?:one|title|movie|film|show|series) "
                    r"(?:is|has) (?:the )?(?:highest|best|top) rating",
                    r"(?:which|what) (?:one|title|movie|film|show|series) "
                    r"is (?:the )?(?:highest|best|top)[ -]?rated",
                    r"(?:which|what) (?:is|are) (?:the )?(?:highest|best|top)[ -]?rated "
                    r"(?:one|ones|title|titles)(?: (?:in|from) (?:this|that|the) list)?",
                ),
            ),
            (
                "movies_only",
                (
                    r"(?:show|filter)(?: me)? (?:only )?(?:the )?movies "
                    r"(?:in|from) (?:this|that|the) list",
                    r"(?:which|what) (?:of )?(?:these|those|the ones) are movies",
                ),
            ),
            (
                "series_only",
                (
                    r"(?:show|filter)(?: me)? (?:only )?(?:the )?(?:series|shows) "
                    r"(?:in|from) (?:this|that|the) list",
                    r"(?:which|what) (?:of )?(?:these|those|the ones) are (?:series|shows)",
                ),
            ),
        )
        for filter_kind, patterns in filter_patterns:
            if any(re.fullmatch(pattern, lower) for pattern in patterns):
                return {"kind": "filter_previous_results", "filter": filter_kind}

        if any(
            re.fullmatch(pattern, lower)
            for pattern in (
                r"(?:show|find|recommend|suggest)(?: me)? (?:more|something) "
                r"(?:like|similar to) (?:these|those|them|the previous results)",
                r"(?:more|something) (?:like|similar to) (?:these|those|them)",
            )
        ):
            return {"kind": "similar_to_previous_results"}
        return None

    @staticmethod
    def extract_top_rated_catalogue_question(user_message: str) -> dict[str, str | None] | None:
        lower = LocalModelService._normalised_question(user_message)
        patterns = (
            r"(?:(?:which|what) is|what's) (?:the )?(?:top|highest|best)[ -]?rated (?P<kind>movies?|films?|series|shows?|titles?)(?: in (?:the )?(?:catalogue|catalog|service))?",
            r"(?:which|what) (?P<kind>movies?|films?|series|shows?|titles?) (?:has (?:the )?(?:top|highest|best) rating|is (?:the )?(?:top|highest|best)[ -]?rated)(?: in (?:the )?(?:catalogue|catalog|service))?",
            r"(?:show|tell|give) me (?:the )?(?:top|highest|best)[ -]?rated (?P<kind>movies?|films?|series|shows?|titles?)(?: in (?:the )?(?:catalogue|catalog|service))?",
        )
        for pattern in patterns:
            match = re.fullmatch(pattern, lower)
            if not match:
                continue
            kind = match.group("kind")
            content_type = (
                "movie"
                if kind in {"movie", "movies", "film", "films"}
                else "tv"
                if kind in {"series", "show", "shows"}
                else None
            )
            return {"contentType": content_type, "kind": kind}
        return None

    @classmethod
    def render_database_boundary_reply(cls, user_message: str) -> str:
        """Return a deterministic response when no current-turn DB evidence exists.

        This is intentionally independent of the model. A request that the
        grounding router cannot satisfy must not fall through to model
        pretraining, even if the model appears confident.
        """
        lower = cls._normalised_question(user_message)
        if any(
            term in lower
            for term in (
                "rotten tomatoes",
                "metacritic",
                "imdb",
                "box office",
                "critic score",
                "review score",
            )
        ):
            return (
                "I do not have a verified external review score for that request "
                "in the current catalogue, so I cannot provide one."
            )
        return cls.DATABASE_BOUNDARY_REPLY

    @staticmethod
    def extract_title_affinity_question(user_message: str) -> dict[str, str] | None:
        text = re.sub(r"\s+", " ", user_message.strip()).strip(" .!?")
        patterns = (
            r"^(?:will|would|might) i (?:like|enjoy) (?P<title>.+)$",
            r"^(?:do you think )?i(?:'d| would) (?:like|enjoy) (?P<title>.+)$",
            r"^is (?P<title>.+?) something i (?:like|would like|might like|enjoy|would enjoy)$",
            r"^does (?P<title>.+?) (?:suit|match) my (?:taste|preferences|profile)$",
        )
        for pattern in patterns:
            match = re.match(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            title = LocalModelService._clean_title_reference(match.group("title"))
            if title:
                return {"title": title, "contextual": str(title).casefold() in {"this", "that", "it", "this title", "that title"}}
        return None

    @staticmethod
    def is_likes_dislikes_question(user_message: str) -> bool:
        lower = LocalModelService._normalised_question(user_message)
        patterns = (
            r"what (?:are|is) my likes? and dislikes?",
            r"what do i like and dislike",
            r"show (?:me )?my likes? and dislikes?",
            r"what (?:films?|movies?|series|shows|titles) do i (?:like|dislike)",
            r"which (?:films?|movies?|series|shows|titles) have i (?:liked|disliked)",
        )
        return any(re.search(pattern, lower) for pattern in patterns)

    @classmethod
    def extract_profile_preference_question(cls, user_message: str) -> dict[str, str] | None:
        """Extract a question about the viewer's stored preference.

        These questions mention catalogue concepts such as ``Horror`` and may
        also contain nouns such as ``films``. They must be resolved against the
        viewer profile before catalogue-browse intent is considered.
        """
        text = re.sub(r"\s+", " ", user_message.strip()).strip(" .!?")
        patterns = (
            r"^(?:do|did) i (?P<verb>like|love|enjoy|prefer|dislike|hate|avoid) (?P<subject>.+)$",
            r"^(?:am|was) i (?:a )?(?P<negation>not )?(?:a )?fan of (?P<subject>.+)$",
            r"^(?:have i|did i) (?:said|say|told you|tell you) (?:that )?i (?P<verb>like|love|enjoy|prefer|dislike|hate|avoid) (?P<subject>.+)$",
        )
        for pattern in patterns:
            match = re.match(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            groups = match.groupdict()
            verb = str(groups.get("verb") or ("dislike" if groups.get("negation") else "like")).lower()
            raw_subject = str(groups.get("subject") or "").strip()
            raw_subject = re.sub(
                r"^(?:the )?(?:genre|theme|category) (?:of )?",
                "",
                raw_subject,
                flags=re.IGNORECASE,
            )
            genre = cls._genre_for(raw_subject)
            subject = genre or cls._clean_theme(raw_subject)
            if not subject:
                return None
            polarity = "negative" if verb in {"dislike", "hate", "avoid"} else "positive"
            return {
                "subject": subject,
                "kind": "genre" if genre else "theme",
                "polarity": polarity,
                "verb": verb,
            }
        return None

    @staticmethod
    def is_profile_question(user_message: str) -> bool:
        if LocalModelService.extract_profile_preference_question(user_message):
            return True
        lower = LocalModelService._normalised_question(user_message)
        patterns = (
            r"what do you (?:know|remember) about my (?:taste|preferences)",
            r"what (?:do|did) you remember about me",
            r"what do i like",
            r"what do i dislike",
            r"what are my likes",
            r"what are my dislikes",
            r"what are my (?:viewing |movie |film )?preferences",
            r"what (?:genres?|generes?|types?) (?:of )?(?:movies?|films?|shows?|series)? do i like",
            r"what (?:type|kind) of (?:movies?|films?|shows?|series) (?:do|will|would) i like",
            r"what (?:type|kind) of (?:movies?|films?|shows?|series) (?:suits?|matches?) my taste",
            r"what do i like (?:watching|to watch)",
            r"which (?:genres?|themes?|directors?|actors?) do i (?:like|prefer)",
            r"show (?:me )?my (?:viewer )?profile",
        )
        return any(re.search(pattern, lower) for pattern in patterns)

    @staticmethod
    def is_personalised_recommendation_request(user_message: str) -> bool:
        lower = LocalModelService._normalised_question(user_message)
        patterns = (
            r"what (?:(?:new|latest|recent|newly released) )?"
            r"(?:movies?|films?|series|shows|titles) (?:will|would) i like",
            r"what should i watch(?: next)?(?: based on my (?:taste|preferences|profile))?",
            r"what can i watch next",
            r"(?:recommend|suggest|find|show me) .+ based on my (?:taste|preferences|profile)",
            r"(?:recommend|suggest) (?:me )?(?:some )?(?:movies?|films?|series|shows) i'll like",
            r"what am i likely to enjoy",
            r"(?:find|pick|choose|suggest|recommend)(?: me)? (?:some )?(?:next watch|something to watch next)",
            r"(?:suggest|recommend|show me|find me) (?:some )?(?:good|great|best|popular)? ?(?:movies?|films?|series|shows)",
            r"(?:anything|something) good (?:to watch|on)",
            r"(?:pick|choose) (?:a|my) next (?:movie|film|series|show)",
        )
        # Specific catalogue concepts after "show me movies" must not be
        # hijacked by the generic personalised-recommendation route.
        return any(re.fullmatch(pattern, lower) for pattern in patterns)

    @staticmethod
    def is_recommendation_request(user_message: str) -> bool:
        lower = user_message.lower()
        if (
            LocalModelService.is_profile_question(user_message)
            or LocalModelService.is_likes_dislikes_question(user_message)
            or LocalModelService.is_watch_history_question(user_message)
            or LocalModelService.is_watch_history_count_question(user_message)
            or LocalModelService.extract_catalogue_count_question(user_message)
            or LocalModelService.is_region_question(user_message)
            or LocalModelService.extract_top_rated_catalogue_question(user_message)
            or LocalModelService.extract_title_affinity_question(user_message)
            or LocalModelService.is_recommendation_explanation_question(user_message)
            or LocalModelService.is_catalogue_result_complaint(user_message)
        ):
            return False
        if LocalModelService.is_personalised_recommendation_request(user_message):
            return True
        explicit_request_terms = (
            "recommend", "suggest", "show me", "find me", "give me",
            "what should i watch", "what can i watch", "watch tonight",
            "something like", "similar to", "help me choose",
        )
        if any(term in lower for term in explicit_request_terms):
            return True
        declarative = LocalModelService.should_capture_preference(user_message)
        if declarative and not LocalModelService.extract_recommendation_clause(user_message):
            return False
        content_terms = ("movies", "films", "series", "shows", "documentary")
        return any(term in lower for term in content_terms)

    @staticmethod
    def _clean_title_reference(value: str) -> str:
        title = value.strip().strip('"“”\'‘’')
        title = re.sub(
            r"^(?:the\s+)?(?:movie|film|tv\s+series|television\s+series|series|show)\s+(?:called\s+)?",
            "",
            title,
            flags=re.IGNORECASE,
        )
        return title.strip().strip('"“”\'‘’')

    @staticmethod
    def extract_recommendation_clause(user_message: str) -> str | None:
        """Return the explicit discovery clause from a compound preference turn.

        Example: ``I dislike horror; suggest a comedy`` becomes
        ``suggest a comedy``. This prevents the saved negative preference from
        being misread as a positive search constraint.
        """
        text = re.sub(r"\s+", " ", user_message.strip())
        match = re.search(
            r"(?:^|[,.!?;:]\s*|\bbut\s+|\band\s+)(?P<clause>"
            r"(?:recommend|suggest|show me|find me|give me|pick|choose|help me choose|"
            r"what should i watch|what can i watch|watch tonight|find something to watch)\b.*)$",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        clause = match.group("clause").strip(" ,.;:!?-")
        return clause or None

    @staticmethod
    def extract_similar_title_request(user_message: str) -> str | None:
        text = user_message.strip().strip(".!?")
        patterns = (
            r"^(?:show|find|recommend|suggest)(?: me)? (?:movies?|films?|series|shows|titles)? ?(?:like|similar to) (?P<title>.+)$",
            r"^(?:movies?|films?|series|shows|titles) (?:like|similar to) (?P<title>.+)$",
            r"^something (?:like|similar to) (?P<title>.+)$",
            r"^more (?:like|similar to) (?P<title>.+)$",
        )
        for pattern in patterns:
            match = re.match(pattern, text, flags=re.IGNORECASE)
            if match:
                title = LocalModelService._clean_title_reference(match.group("title"))
                if title and not re.search(r"\b(?:movies?|films?|series|shows)\b$", title, flags=re.IGNORECASE):
                    return title
        return None

    @staticmethod
    def extract_direct_title_candidate(user_message: str) -> str | None:
        """Extract a possibly literal title from an ambiguous search command.

        The caller must still resolve it against Couchbase with the confidence
        gate. A failed resolution falls back to catalogue discovery.
        """
        text = re.sub(r"\s+", " ", user_message.strip()).strip(" .!?")
        match = re.match(
            r"^(?:show me|find me|search for|look up) (?P<title>.+)$",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        title = LocalModelService._clean_title_reference(match.group("title"))
        if not title or re.search(r"\b(?:like|similar to)\b", title, flags=re.IGNORECASE):
            return None
        if re.fullmatch(
            r"(?:a|an|some|any|good|great|best|new|popular|trending)? ?(?:movies?|films?|series|shows?|titles?)",
            title,
            flags=re.IGNORECASE,
        ):
            return None
        if re.search(r"\b(?:movies|films|series|shows|titles)$", title, flags=re.IGNORECASE):
            return None
        return title

    @staticmethod
    def extract_catalogue_title_question(user_message: str) -> dict[str, str] | None:
        """Extract explicit title-level catalogue questions without guessing.

        Broad discovery language is intentionally excluded; those requests go
        through recommendation retrieval instead.
        """
        text = re.sub(r"\s+", " ", user_message.strip()).strip()
        if not text or LocalModelService.is_recommendation_request(text):
            return None
        patterns: tuple[tuple[str, str], ...] = (
            # Entitlement wording must be checked before the broad ``is <title>``
            # availability pattern, otherwise phrases such as "included in my
            # plan" become part of the title sent to Couchbase.
            ("availability", r"^(?:is|are) (?P<title>.+?) included (?:in|with|on) (?:my|the|this|our) (?:plan|subscription|package)\??$"),
            ("availability", r"^(?:is|are) (?P<title>.+?) (?:covered|available) (?:under|with|through) (?:my|the|this|our) (?:plan|subscription|package)\??$"),
            ("availability", r"^(?:do i|does this viewer) have (?:access to|entitlement for) (?P<title>.+?)\??$"),
            ("availability", r"^(?:can i|may i) watch (?P<title>.+?) (?:with|using|under|on) (?:my|the|this|our) (?:plan|subscription|package)\??$"),
            ("availability", r"^(?:do you have|have you got|is) (?P<title>.+?)(?: available| in (?:the )?(?:catalogue|catalog)| on (?:here|this service|the service))?\??$"),
            ("availability", r"^(?:can i|may i) watch (?P<title>.+?)(?: on (?:here|this service|the service))?\??$"),
            ("availability", r"^(?:is|do you have) (?P<title>.+?) on (?:here|this service|the service)\??$"),
            ("availability", r"^does (?:the|your|this) (?:catalogue|catalog|service) have (?P<title>.+?)\??$"),
            ("overview", r"^(?:what is|what's) (?P<title>.+?) about\??$"),
            ("overview", r"^(?:tell me|give me information) about (?P<title>.+?)\??$"),
            ("cast", r"^(?:who (?:is|stars?) in|cast of) (?P<title>.+?)\??$"),
            ("director", r"^(?:who directed|director of|who created) (?P<title>.+?)\??$"),
            ("runtime", r"^(?:how long is|runtime of|what is the runtime of|what's the runtime of|how many minutes is) (?P<title>.+?)\??$"),
            ("release", r"^(?:when was|what year was) (?P<title>.+?) released\??$"),
            ("release", r"^when did (?P<title>.+?) (?:come out|release)\??$"),
            ("genres", r"^(?:what genre is|genres? of) (?P<title>.+?)\??$"),
            ("rating", r"^(?:what is the rating of|what rating is|how is) (?P<title>.+?)(?: rated)?\??$"),
            ("details", r"^(?:find|look up|search for) (?P<title>.+?)\??$"),
        )
        for question_type, pattern in patterns:
            match = re.match(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            title = LocalModelService._clean_title_reference(match.group("title"))
            title = re.sub(r"\s+(?:please|for me)$", "", title, flags=re.IGNORECASE).strip()
            if not title or re.fullmatch(r"(?:a|some|good|great|best|new)? ?(?:movies?|films?|series|shows)", title, flags=re.IGNORECASE):
                return None
            return {"type": question_type, "title": title}
        return None

    @staticmethod
    def history_content_type(user_message: str) -> str | None:
        lower = user_message.lower()
        if re.search(r"\b(?:series|shows|tv)\b", lower):
            return "tv"
        if re.search(r"\b(?:film|films|movie|movies)\b", lower):
            return "movie"
        return None

    @staticmethod
    def extract_watched_title_claim(user_message: str) -> str | None:
        text = user_message.strip()
        if not text or LocalModelService.is_watch_history_question(text):
            return None
        patterns = (
            r"^i\s+(?:have\s+|have\s+already\s+|just\s+|already\s+)?(?:watched|seen)\s+(?P<title>.+?)[.!?]*$",
            r"^i['’]ve\s+(?:already\s+)?(?:watched|seen)\s+(?P<title>.+?)[.!?]*$",
            r"^i\s+(?:finished|completed)\s+(?:watching\s+)?(?P<title>.+?)[.!?]*$",
            r"^i\s+saw\s+(?P<title>.+?)[.!?]*$",
        )
        for pattern in patterns:
            match = re.match(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            title = match.group("title").strip().strip('"“”\'‘’')
            title = re.sub(r"^the\s+(?:film|movie|series|show)\s+", "", title, flags=re.IGNORECASE)
            title = re.sub(r"\s+already$", "", title, flags=re.IGNORECASE).strip()
            if len(title) >= 2:
                return title
        return None

    @staticmethod
    def render_region_reply(entitlement: dict[str, Any]) -> str:
        region = str(entitlement.get("regionCode") or "unknown").upper()
        return f"Your viewer account is currently set to the {region} region. This value comes from your subscription profile."

    @staticmethod
    def render_region_update(entitlement: dict[str, Any]) -> str:
        region = str(entitlement.get("regionCode") or "unknown").upper()
        return f"Got it — I updated your viewer region to {region}. Future catalogue availability and entitlement decisions will use that region."

    @staticmethod
    def render_watch_history_count(items: list[dict[str, Any]], content_type: str | None) -> str:
        count = len(items)
        singular = "movie" if content_type == "movie" else "series" if content_type == "tv" else "title"
        plural = "movies" if content_type == "movie" else "series" if content_type == "tv" else "titles"
        if count == 0:
            return f"You do not have any watched {plural} recorded yet."
        noun = singular if count == 1 else plural
        return f"You have {count} watched {noun} recorded in your viewing history."

    @staticmethod
    def render_catalogue_count(result: dict[str, Any]) -> str:
        count = int(result.get("count") or 0)
        content_type = result.get("contentType")
        genre = str(result.get("genre") or "").strip()
        singular = "movie" if content_type == "movie" else "series" if content_type == "tv" else "title"
        plural = "movies" if content_type == "movie" else "series" if content_type == "tv" else "titles"
        noun = singular if count == 1 else plural
        qualifier = f" {genre}" if genre else ""
        if count == 0:
            return f"There are no{qualifier} {plural} in the current catalogue."
        return f"There {'is' if count == 1 else 'are'} {count}{qualifier} {noun} in the current catalogue."

    @staticmethod
    def render_catalogue_analytics(result: dict[str, Any]) -> str:
        rows = list(result.get("rows") or [])
        measure = str(result.get("measure") or "count")
        dimension = str(result.get("dimension") or "group")
        if not rows:
            return "The catalogue analytics query returned no matching groups."
        dimension_labels = {
            "genre": "genre",
            "release_year": "release year",
            "original_language": "original language",
            "country": "country",
            "content_type": "content type",
        }
        measure_labels = {
            "count": "title count",
            "average_rating": "average catalogue rating",
            "average_runtime": "average runtime",
        }
        lines: list[str] = []
        for row in rows[:30]:
            label = str(row.get("label") or "Unknown")
            value = row.get("value")
            if measure == "count":
                display = f"{int(value or 0):,}"
            elif measure == "average_rating":
                display = f"{float(value or 0):.2f}/10"
            else:
                display = f"{float(value or 0):.1f} minutes"
            lines.append(f"• {label}: {display}")
        return (
            f"Catalogue {measure_labels.get(measure, measure)} by "
            f"{dimension_labels.get(dimension, dimension)}:\n\n"
            + "\n".join(lines)
        )

    @staticmethod
    def render_top_rated_title(title: dict[str, Any] | None, content_type: str | None) -> str:
        noun = "movie" if content_type == "movie" else "series" if content_type == "tv" else "title"
        if not title:
            return f"I could not find a rated {noun} in the current catalogue."
        name = str(title.get("title") or "Untitled")
        year = title.get("releaseYear")
        label = f"{name} ({year})" if year else name
        rating = float(title.get("voteAverage") or 0)
        votes = int(title.get("voteCount") or 0)
        vote_note = f" from {votes:,} viewer votes" if votes else ""
        return f"The highest-rated {noun} in the current catalogue is {label} at {rating:.1f}/10{vote_note}."

    @staticmethod
    def render_catalogue_plan_results(
        plan: dict[str, Any],
        results: list[dict[str, Any]],
        trace: dict[str, Any] | None = None,
    ) -> str:
        """Render only the records returned by a validated catalogue plan."""
        content_type = plan.get("contentType")
        noun = "movies" if content_type == "movie" else "series" if content_type == "tv" else "titles"
        filters = dict(plan.get("filters") or {})
        resolved = dict((trace or {}).get("resolvedFilters") or filters)
        if not results:
            constraints: list[str] = []
            if filters.get("titleContains"):
                constraints.append(f"with “{filters['titleContains']}” in the title")
            if resolved.get("releaseYear") is not None:
                constraints.append(f"released in {int(resolved['releaseYear'])}")
            elif resolved.get("releaseYearMin") is not None:
                constraints.append(
                    f"released from {int(resolved['releaseYearMin'])}"
                    + (
                        f" to {int(resolved['releaseYearMax'])}"
                        if resolved.get("releaseYearMax") is not None
                        else ""
                    )
                )
            if filters.get("genres"):
                constraints.append(
                    "in " + ", ".join(str(value) for value in filters["genres"])
                )
            suffix = " " + " ".join(constraints) if constraints else ""
            return f"I found no {noun}{suffix} in the current catalogue."

        heading = f"Here are the matching {noun} from the current catalogue:"
        if plan.get("sort") == "rating_desc":
            heading = f"Here are the {len(results)} highest-rated {noun} in the current catalogue:"
        elif filters.get("titleContains"):
            heading = (
                f"Here are {noun} with “{filters['titleContains']}” in the title:"
            )
        elif resolved.get("releaseYear") is not None:
            heading = f"Here are {noun} released in {int(resolved['releaseYear'])}:"
        elif plan.get("semanticQuery"):
            heading = (
                f"Here are the strongest catalogue matches for "
                f"“{plan['semanticQuery']}”:"
            )

        lines: list[str] = []
        for item in results:
            title = str(item.get("title") or "Untitled")
            year = item.get("releaseYear")
            label = f"{title} ({year})" if year else title
            genres = ", ".join(str(value) for value in item.get("genres", [])[:3])
            rating = item.get("voteAverage")
            detail: list[str] = []
            if genres:
                detail.append(genres)
            if rating is not None and plan.get("sort") == "rating_desc":
                detail.append(f"{float(rating):.1f}/10")
                votes = int(item.get("voteCount") or 0)
                if votes:
                    detail.append(f"{votes:,} votes")
            lines.append(
                f"• {label}" + (f" — {' · '.join(detail)}" if detail else "")
            )
        return heading + "\n\n" + "\n".join(lines)

    @staticmethod
    def render_title_affinity(title: dict[str, Any], affinity: dict[str, Any]) -> str:
        name = str(title.get("title") or "This title")
        verdict = str(affinity.get("verdict") or "unknown")
        reasons = [str(value) for value in affinity.get("reasons") or [] if value]
        reason_text = "; ".join(reasons[:4])
        if verdict == "liked":
            return f"Yes — {name} is already recorded in your liked titles."
        if verdict == "disliked":
            return f"No — {name} is recorded in your disliked titles."
        if verdict == "likely":
            return f"Based on your verified viewer profile, you are likely to enjoy {name}" + (f": {reason_text}." if reason_text else ".")
        if verdict == "unlikely":
            return f"Based on your verified viewer profile, {name} is unlikely to suit you" + (f": {reason_text}." if reason_text else ".")
        if verdict == "mixed":
            return f"Your profile has mixed signals for {name}" + (f": {reason_text}." if reason_text else ".")
        return f"I do not have enough verified profile evidence to say whether you will like {name}."

    @staticmethod
    def render_watch_history_reply(items: list[dict[str, Any]]) -> str:
        if not items:
            return "You don’t have any watched titles recorded yet. Tell me what you have watched, or use the Play/Complete controls on a title card."
        lines: list[str] = []
        for item in items[:15]:
            title = str(item.get("title") or "Untitled")
            year = item.get("releaseYear")
            progress = float(item.get("progressPct") or 0)
            status = "completed" if progress >= 85 else f"{round(progress)}% watched"
            label = f"{title} ({year})" if year else title
            lines.append(f"• {label} — {status}")
        return "Here is your recorded watch history:\n\n" + "\n".join(lines)

    @staticmethod
    def render_watched_confirmation(title: dict[str, Any]) -> str:
        name = str(title.get("title") or "that title")
        year = title.get("releaseYear")
        label = f"{name} ({year})" if year else name
        return f"Got it — I’ve added {label} to your completed watch history."

    @staticmethod
    def _viewer_fact_wording(fact: str) -> str:
        text = fact.strip().rstrip(".")
        replacements = (
            (r"^Viewer generally prefers\b", "you generally prefer"),
            (r"^Viewer prefers to\b", "you prefer to"),
            (r"^Viewer prefers\b", "you prefer"),
            (r"^Viewer dislikes\b", "you dislike"),
            (r"^Viewer likes\b", "you like"),
            (r"^Viewer avoids\b", "you avoid"),
        )
        for pattern, replacement in replacements:
            updated = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
            if updated != text:
                return updated
        return re.sub(r"^Viewer\s+", "you ", text, flags=re.IGNORECASE)

    @staticmethod
    def sanitise_consumer_language(value: str) -> str:
        """Keep the white-labelled viewer experience free of implementation names."""
        text = str(value or "")
        replacements = (
            (r"Grounded\s+Couchbase\s+catalogue\s+results", "Verified catalogue results"),
            (r"Couchbase\s+catalogue", "catalogue"),
            (r"Couchbase\s+catalog", "catalogue"),
            (r"recorded\s+in\s+Couchbase", "recorded in your viewing history"),
            (r"Couchbase\s+entitlement\s+document", "subscription profile"),
            (r"Couchbase\s+Agent\s+Catalog", "governed tool catalogue"),
            (r"Couchbase\s+MCP\s+Server", "data access service"),
            (r"\bCouchbase\b", "the platform"),
        )
        for pattern, replacement in replacements:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        return text

    @staticmethod
    def render_preference_confirmation(preferences: list[dict[str, Any]]) -> str:
        if not preferences:
            return "I heard that as a current-session preference, so I have not added it to your durable profile."
        if len(preferences) == 1:
            fact = LocalModelService._viewer_fact_wording(str(preferences[0].get("fact") or ""))
            return f"Got it — I’ll remember that {fact}. I’ll use it in future recommendations."
        lines = []
        for item in preferences:
            fact = LocalModelService._viewer_fact_wording(str(item.get("fact") or ""))
            lines.append(f"• {fact}")
        return "Got it. I’ll remember these preferences:\n\n" + "\n".join(lines)

    @staticmethod
    def render_likes_dislikes_reply(
        profile: dict[str, Any], liked_titles: list[dict[str, Any]], disliked_titles: list[dict[str, Any]]
    ) -> str:
        liked: list[str] = []
        disliked: list[str] = []
        liked.extend(str(item.get("title")) for item in liked_titles if item.get("title"))
        liked.extend(str(value) for value in profile.get("preferredGenres", []) if value)
        liked.extend(f"{value}-themed content" for value in profile.get("preferredThemes", []) if value)
        liked.extend(f"titles involving {value}" for value in profile.get("preferredPeople", []) if value)
        disliked.extend(str(item.get("title")) for item in disliked_titles if item.get("title"))
        disliked.extend(f"{value} content" for value in profile.get("dislikedGenres", []) if value)
        disliked.extend(f"{value}-themed content" for value in profile.get("dislikedThemes", []) if value)
        disliked.extend(f"titles involving {value}" for value in profile.get("dislikedPeople", []) if value)
        if profile.get("avoidGraphicViolence"):
            disliked.append("graphic violence")
        if profile.get("avoidAdultContent"):
            disliked.append("sexual and adult content")

        def unique(values: list[str]) -> list[str]:
            seen: set[str] = set()
            result: list[str] = []
            for value in values:
                key = value.casefold()
                if value and key not in seen:
                    seen.add(key)
                    result.append(value)
            return result

        liked = unique(liked)
        disliked = unique(disliked)
        lines = ["Here is your authoritative likes and dislikes profile:", "", "Likes:"]
        if liked:
            lines.extend(f"• {value}" for value in liked)
        else:
            lines.append("• None recorded yet")
        lines.extend(["", "Dislikes:"])
        if disliked:
            lines.extend(f"• {value}" for value in disliked)
        else:
            lines.append("• None recorded yet")
        return "\n".join(lines)

    @staticmethod
    def render_profile_preference_answer(
        question: dict[str, str], profile: dict[str, Any]
    ) -> str:
        subject = str(question.get("subject") or "that").strip()
        kind = str(question.get("kind") or "theme")
        requested_polarity = str(question.get("polarity") or "positive")
        if kind == "genre":
            positive_values = profile.get("preferredGenres", [])
            negative_values = profile.get("dislikedGenres", [])
            positive_label = "preferred genre"
            negative_label = "genre to avoid"
        else:
            positive_values = profile.get("preferredThemes", [])
            negative_values = profile.get("dislikedThemes", [])
            positive_label = "preferred theme"
            negative_label = "theme to avoid"

        def contains(values: Any) -> bool:
            return any(str(value).strip().casefold() == subject.casefold() for value in (values or []))

        positive = contains(positive_values)
        negative = contains(negative_values)
        if positive and negative:
            return (
                f"Your profile currently has conflicting signals for {subject}: it appears as both a "
                f"{positive_label} and a {negative_label}. You may want to correct it in My Profile."
            )
        if requested_polarity == "negative":
            if negative:
                return f"Yes — {subject} is recorded in your profile as a {negative_label}."
            if positive:
                return f"No — {subject} is recorded in your profile as a {positive_label}, not something you avoid."
            return f"I don’t have a stored like or dislike for {subject} in your viewer profile yet."
        if positive:
            return f"Yes — {subject} is recorded in your profile as a {positive_label}."
        if negative:
            return f"No — {subject} is recorded in your profile as a {negative_label}."
        return f"I don’t have a stored like or dislike for {subject} in your viewer profile yet."

    @staticmethod
    def render_profile_reply(profile: dict[str, Any], long_term_context: list[dict[str, Any]]) -> str:
        sections: list[str] = []
        if str(profile.get("exclusivePreferenceMode") or "").lower() == "content":
            exclusive_values = [
                str(value)
                for value in [
                    *profile.get("preferredGenres", []),
                    *profile.get("preferredThemes", []),
                ]
                if value
            ]
            if exclusive_values:
                sections.append(
                    f"• Exclusive preference: only {', '.join(exclusive_values)} content"
                )
        pairs = (
            ("Preferred genres", profile.get("preferredGenres", [])),
            ("Themes", profile.get("preferredThemes", [])),
            ("People", profile.get("preferredPeople", [])),
            ("Languages", profile.get("preferredLanguages", [])),
            ("Genres to avoid", profile.get("dislikedGenres", [])),
            ("Themes to avoid", profile.get("dislikedThemes", [])),
        )
        for label, values in pairs:
            values = [str(value) for value in values if value]
            if values:
                sections.append(f"• {label}: {', '.join(values)}")
        if profile.get("maxRuntimeMinutes"):
            sections.append(f"• Runtime: generally under {profile['maxRuntimeMinutes']} minutes")
        safety: list[str] = []
        if profile.get("avoidGraphicViolence"):
            safety.append("graphic violence")
        if profile.get("avoidAdultContent"):
            safety.append("sexual and adult content")
        if safety:
            sections.append(f"• Safety: avoid {', '.join(safety)}")
        if not sections:
            facts = [str(item.get("fact")) for item in long_term_context if item.get("fact")]
            if facts:
                sections.extend(f"• {fact}" for fact in facts[:10])
        if not sections:
            return "I don’t have any durable viewing preferences recorded for you yet. Tell me what you like or dislike and I can remember it."
        return "Here is your current viewer profile:\n\n" + "\n".join(sections)

    @staticmethod
    def render_catalogue_recommendations(
        user_message: str, candidates: list[dict[str, Any]], trace: dict[str, Any]
    ) -> str:
        if not candidates:
            genres = trace.get("structuredGenres") or []
            content_type = trace.get("structuredContentType")
            inventory_count = int(trace.get("catalogueInventoryCount") or 0)
            exclusive_policy = dict(trace.get("exclusivePreferencePolicy") or {})
            if exclusive_policy and trace.get("exclusivePreferenceConflict"):
                requested = ", ".join(str(value) for value in genres) or "that content"
                allowed = str(exclusive_policy.get("label") or "the selected")
                noun = (
                    "movies"
                    if content_type == "movie"
                    else "series" if content_type == "tv" else "titles"
                )
                return (
                    f"Your viewer profile is set to only {allowed} content, so I did not return "
                    f"{requested} {noun}. Change or remove that exclusive preference before "
                    "searching, liking or playing content outside it."
                )
            if genres:
                label = ", ".join(str(value) for value in genres)
                noun = "movies" if content_type == "movie" else "series" if content_type == "tv" else "titles"
                if inventory_count > 0:
                    return (
                        f"There are {inventory_count} {label} {noun} in the catalogue, but none currently pass "
                        "this viewer’s region, subscription and preference filters. I won’t present an unavailable "
                        "title as a recommendation."
                    )
                return (
                    f"I understood this as a request for {label} {noun}, but the current catalogue contains no "
                    f"matching {noun}. I won’t invent catalogue details."
                )
            expanded = [str(value) for value in trace.get("expandedTerms") or [] if value]
            tried = f" I also checked related catalogue terms: {', '.join(expanded)}." if expanded else ""
            return (
                "I couldn’t find a matching title available in the current catalogue. "
                "I won’t suggest titles outside the catalogue." + tried
            )
        lines: list[str] = []
        for item in candidates[:5]:
            title = str(item.get("title") or "Untitled")
            year = item.get("releaseYear")
            label = f"{title} ({year})" if year else title
            genres = ", ".join(str(x) for x in item.get("genres", [])[:3])
            reason = str((item.get("matchExplanation") or {}).get("summary") or "catalogue relevance")
            detail = f"{genres}. {reason}." if genres else f"{reason}."
            lines.append(f"• {label} — {detail}")
        overrides = trace.get("temporaryPreferenceOverrides") or []
        override_note = ""
        if overrides:
            labels = ", ".join(str(value).split(":", 1)[-1].strip().title() for value in overrides)
            override_note = (
                f"You normally avoid {labels}, but this request temporarily overrides that preference for this turn. "
                "Your durable profile was not changed.\n\n"
            )
        return (
            override_note
            + "Here are the strongest available matches from the current catalogue:\n\n"
            + "\n".join(lines)
        )

    @staticmethod
    def render_title_information(
        request: dict[str, str],
        match: dict[str, Any] | None,
        alternatives: list[dict[str, Any]] | None = None,
    ) -> str:
        title_query = request.get("title") or "that title"
        if not match:
            return (
                f"I couldn’t find “{title_query}” in the current catalogue. "
                "I won’t claim it is available or invent catalogue details."
            )
        title = str(match.get("title") or title_query)
        year = match.get("releaseYear")
        label = f"{title} ({year})" if year else title
        kind = request.get("type") or "details"
        if kind == "availability":
            genres = ", ".join(str(value) for value in match.get("genres", [])[:3])
            suffix = f" It is listed as {genres}." if genres else ""
            entitlement = dict(match.get("entitlement") or {})
            if entitlement:
                if entitlement.get("allowed") and entitlement.get("includedInPlan"):
                    return f"Yes — {label} is in the catalogue and included in your current plan.{suffix}"
                if entitlement.get("allowed"):
                    return (
                        f"{label} is in the catalogue, but it requires a separate "
                        f"{entitlement.get('offerType') or 'purchase'} for this viewer.{suffix}"
                    )
                return (
                    f"I found {label} in the catalogue, but it is not available to this "
                    f"viewer: {entitlement.get('reason') or 'the entitlement policy denied access'}."
                )
            return f"Yes — {label} is available in the current catalogue.{suffix}"
        guide = current_guide(match)
        if guide and kind in {"overview", "details"}:
            return (f"{label}: {guide['summary']}\n\n"
                    f"AI mood classification: {guide['classification']}\n"
                    "From the saved AI viewing guide in Couchbase. Reused without regenerating this guide.")
        if kind == "overview":
            overview = str(match.get("overview") or "No synopsis is stored for this title.")
            return f"{label}: {overview}"
        if kind == "cast":
            cast = [str(value) for value in match.get("castNames", []) if value]
            return f"The catalogue lists {', '.join(cast[:10])} in {label}." if cast else f"No cast list is stored for {label}."
        if kind == "director":
            people = [str(value) for value in match.get("directorNames", []) if value]
            return f"The catalogue lists {', '.join(people)} as the director or creator of {label}." if people else f"No director or creator is stored for {label}."
        if kind == "runtime":
            runtime = match.get("runtimeMinutes") or match.get("episodeRuntimeMinutes")
            return f"{label} has a catalogue runtime of {runtime} minutes." if runtime else f"No runtime is stored for {label}."
        if kind == "release":
            return f"The catalogue release year for {title} is {year}." if year else f"No release year is stored for {title}."
        if kind == "genres":
            genres = [str(value) for value in match.get("genres", []) if value]
            return f"{label} is categorised as {', '.join(genres)}." if genres else f"No genres are stored for {label}."
        if kind == "rating":
            rating = match.get("voteAverage")
            return f"{label} has a catalogue rating of {float(rating):.1f}/10." if rating is not None else f"No rating is stored for {label}."
        overview = str(match.get("overview") or "No synopsis is stored.")
        genres = ", ".join(str(value) for value in match.get("genres", [])[:3])
        runtime = match.get("runtimeMinutes") or match.get("episodeRuntimeMinutes")
        facts = [value for value in (genres, f"{runtime} minutes" if runtime else "") if value]
        return f"{label}{' — ' + ' · '.join(facts) if facts else ''}. {overview}"

    @staticmethod
    def render_similar_recommendations(
        seed: dict[str, Any] | None,
        candidates: list[dict[str, Any]],
    ) -> str:
        if not seed:
            return "I couldn’t resolve the reference title in the current catalogue, so I did not generate similarity recommendations."
        seed_title = str(seed.get("title") or "that title")
        if not candidates:
            return f"I found {seed_title} in the catalogue, but there are no unseen catalogue titles similar enough to recommend right now."
        lines = []
        for item in candidates[:5]:
            title = str(item.get("title") or "Untitled")
            year = item.get("releaseYear")
            genres = ", ".join(str(value) for value in item.get("genres", [])[:3])
            lines.append(f"• {title}{f' ({year})' if year else ''} — {genres or 'catalogue similarity'}")
        return f"Because you asked for titles like {seed_title}, these are the strongest available catalogue matches:\n\n" + "\n".join(lines)

    @staticmethod
    def _render_title_list(
        candidates: list[dict[str, Any]], *, empty: str, heading: str
    ) -> str:
        if not candidates:
            return empty
        lines: list[str] = []
        for item in candidates[:10]:
            title = str(item.get("title") or "Untitled")
            year = item.get("releaseYear")
            genres = ", ".join(str(value) for value in item.get("genres", [])[:3] if value)
            suffix = f" — {genres}" if genres else ""
            lines.append(f"• {title}{f' ({year})' if year else ''}{suffix}")
        return heading + "\n\n" + "\n".join(lines)

    @classmethod
    def render_my_list(cls, candidates: list[dict[str, Any]]) -> str:
        return cls._render_title_list(
            candidates,
            empty="My List is currently empty.",
            heading="These exact catalogue titles are currently in My List:",
        )

    @classmethod
    def render_recent_my_list_additions(
        cls, candidates: list[dict[str, Any]]
    ) -> str:
        return cls._render_title_list(
            candidates,
            empty="I found no completed My List additions for this viewer.",
            heading="These are the most recent completed additions to My List:",
        )

    @classmethod
    def render_multi_seed_similarity(
        cls,
        seed_titles: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        *,
        source_label: str,
    ) -> str:
        seed_names = [str(item.get("title")) for item in seed_titles if item.get("title")]
        if not seed_names:
            return f"There are no grounded titles in {source_label} to use as similarity seeds."
        if not candidates:
            return (
                f"I used {len(seed_names)} exact title"
                f"{'s' if len(seed_names) != 1 else ''} from {source_label}, but no unseen "
                "catalogue titles passed similarity, preference and entitlement checks."
            )
        heading = (
            f"I combined {len(seed_names)} exact catalogue titles from {source_label} "
            "into one similarity search. These are the strongest unseen matches:"
        )
        return cls._render_title_list(candidates, empty="", heading=heading)

    @classmethod
    def render_contextual_filter(
        cls, filter_kind: str, candidates: list[dict[str, Any]]
    ) -> str:
        labels = {
            "playable": "included in your current plan and playable now",
            "available": "available to this viewer",
            "in_my_list": "already in My List",
            "missing_from_my_list": "not currently in My List",
            "watched": "already watched",
            "unwatched": "not yet watched",
            "liked": "already liked",
            "top_rated": "the highest-rated title in the previous results",
            "movies_only": "movies in the previous results",
            "series_only": "series in the previous results",
        }
        label = labels.get(filter_kind, "matching the requested filter")
        return cls._render_title_list(
            candidates,
            empty=f"None of the previous grounded results are {label}. I did not broaden the search.",
            heading=f"From the previous grounded results, these are {label}:",
        )

    @staticmethod
    def render_previous_question(recall: dict[str, Any] | None) -> str:
        if not recall or not recall.get("userMessage"):
            return (
                "I do not have a verified earlier question in the active conversation "
                "or retained conversation memory."
            )
        source = (
            "the active conversation"
            if recall.get("source") == "active_transcript"
            else "retained conversation memory"
        )
        return f'Your previous question in {source} was: “{recall["userMessage"]}”'

    @staticmethod
    def render_previous_answer(recall: dict[str, Any] | None) -> str:
        if not recall or not recall.get("assistantMessage"):
            return (
                "I do not have a verified earlier answer in the active conversation "
                "or retained conversation memory."
            )
        return "My previous verified answer was:\n\n" + str(recall["assistantMessage"])

    @classmethod
    def render_previous_results(cls, candidates: list[dict[str, Any]]) -> str:
        return cls._render_title_list(
            candidates,
            empty=(
                "There is no previous grounded result list to repeat. "
                "I did not run a new search."
            ),
            heading="These were the previous grounded catalogue results:",
        )

    @staticmethod
    def render_catalogue_retry(
        original_query: str,
        candidates: list[dict[str, Any]],
        trace: dict[str, Any],
    ) -> str:
        if not candidates:
            return (
                "You’re right—the previous results did not satisfy the request. I reran the original search "
                f"with strict structured constraints for “{original_query}”, but the current catalogue has no valid matches."
            )
        base = LocalModelService.render_catalogue_recommendations(original_query, candidates, trace)
        return (
            "You’re right—the previous results were not relevant. I reran the original request "
            f"“{original_query}” using exact genre and content-type validation.\n\n" + base
        )

    @staticmethod
    def render_recommendation_explanation(profile: dict[str, Any]) -> str:
        disliked_genres = [str(value) for value in profile.get("dislikedGenres", []) if value]
        disliked_themes = [str(value) for value in profile.get("dislikedThemes", []) if value]
        exclusions = [*disliked_genres, *disliked_themes]
        if exclusions:
            avoided = ", ".join(exclusions)
            return (
                f"You are right to question that recommendation. Your profile says to avoid {avoided}, "
                "so a title matching those negative preferences should have been excluded. The earlier "
                "recommendation path failed to apply the negative-profile filter. I have not changed your profile."
            )
        return (
            "You are right to question that recommendation. It should only have been shown when it matched "
            "your stored profile and passed all exclusion rules. I have not changed your preferences."
        )

    @staticmethod
    def render_profile_recommendations(
        candidates: list[dict[str, Any]], profile: dict[str, Any], trace: dict[str, Any]
    ) -> str:
        if not candidates:
            return (
                "I couldn’t find an unseen title in the current catalogue that satisfies your stored preferences "
                "and exclusions. Try broadening your profile or loading a larger TMDB catalogue."
            )
        lines: list[str] = []
        for item in candidates[:4]:
            title = str(item.get("title") or "Untitled")
            year = item.get("releaseYear")
            label = f"{title} ({year})" if year else title
            genres = ", ".join(str(x) for x in item.get("genres", [])[:3])
            reason = str((item.get("matchExplanation") or {}).get("summary") or genres or "profile relevance")
            lines.append(f"• {label} — {genres or reason}. {reason}.")
        positives = [
            *[str(x) for x in profile.get("preferredGenres", [])],
            *[str(x) for x in profile.get("preferredThemes", [])],
            *[str(x) for x in profile.get("preferredPeople", [])],
        ]
        negatives = [
            *[str(x) for x in profile.get("dislikedGenres", [])],
            *[str(x) for x in profile.get("dislikedThemes", [])],
        ]
        intro = "Based on your stored viewer profile, these unseen catalogue titles are likely to suit you"
        if positives:
            intro += f" ({', '.join(positives[:6])})"
        elif trace.get("coldStartFallback"):
            intro = "You do not have enough positive preference signals yet, so I used popular unseen titles from the current catalogue"
        suffix = f" I excluded {', '.join(negatives)}." if negatives else ""
        evidence_note = ""
        if trace.get("profileMatchLimited"):
            evidence_note = (
                f" I found {len(candidates)} strong catalogue match"
                f"{'es' if len(candidates) != 1 else ''} and did not pad the list with unrelated popular titles."
            )
        return (
            intro
            + ":\n\n"
            + "\n".join(lines)
            + f"\n\nThe cards below are the exact catalogue records used.{suffix}{evidence_note}"
        )

    def grounded_memory_reply(
        self,
        *,
        user_message: str,
        short_term_context: list[dict[str, Any]],
        long_term_context: list[dict[str, Any]],
        current_turn_facts: list[str],
        profile: dict[str, Any] | None = None,
    ) -> str | None:
        if self.is_profile_question(user_message):
            return self.render_profile_reply(profile or {}, long_term_context)
        if current_turn_facts and not user_message.strip().endswith("?"):
            preferences = [
                {"fact": fact} for fact in current_turn_facts
            ]
            return self.render_preference_confirmation(preferences)
        return None

    async def stream_answer(
        self,
        *,
        user_message: str,
        short_term_context: list[dict[str, Any]],
        long_term_context: list[dict[str, Any]],
        current_turn_facts: list[str] | None = None,
        catalogue_candidates: list[dict[str, Any]] | None = None,
        retrieval_trace: dict[str, Any] | None = None,
        usage_out: dict[str, Any] | None = None,
        system_prompt: str | None = None,
    ) -> AsyncIterator[str]:
        # Defence in depth: direct callers cannot use model pretraining when no
        # current-turn catalogue evidence was supplied. The primary viewer route
        # also blocks this before reaching stream_answer.
        if not catalogue_candidates:
            yield self.render_database_boundary_reply(user_message)
            return
        messages = self._build_messages(
            user_message=user_message,
            short_term_context=short_term_context,
            long_term_context=long_term_context,
            current_turn_facts=current_turn_facts or [],
            catalogue_candidates=catalogue_candidates or [],
            retrieval_trace=retrieval_trace or {},
            system_prompt=system_prompt,
        )
        async for token in self._chat_provider.stream(messages, usage_out):
            yield token

    def _build_messages(
        self,
        *,
        user_message: str,
        short_term_context: list[dict[str, Any]],
        long_term_context: list[dict[str, Any]],
        current_turn_facts: list[str],
        catalogue_candidates: list[dict[str, Any]],
        retrieval_trace: dict[str, Any],
        system_prompt: str | None = None,
    ) -> list[dict[str, str]]:
        system = system_prompt or (
            "You are StreamAI, a concise conversational TV and film assistant. "
            "Do not mention Couchbase, MCP, Agent Catalog, database documents, or other implementation details "
            "in viewer-facing replies unless the user explicitly asks a technical architecture question. "
            "Do not name or recommend any film or television title unless it appears in CATALOGUE CANDIDATES. "
            "If no catalogue candidates are supplied, do not invent titles. "
            "Treat only user statements and VERIFIED VIEWER FACTS as viewer preferences. "
            "Never infer interest from an earlier assistant suggestion or a displayed title."
        )
        facts = [str(item.get("fact")) for item in long_term_context if item.get("fact")]
        facts.extend(current_turn_facts)
        if facts:
            system += "\n\nVERIFIED VIEWER FACTS:\n" + "\n".join(f"- {fact}" for fact in facts)
        if catalogue_candidates:
            compact = [
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "type": item.get("contentType"),
                    "year": item.get("releaseYear"),
                    "genres": item.get("genres", []),
                }
                for item in catalogue_candidates[:8]
            ]
            system += "\n\nCATALOGUE CANDIDATES:\n" + json.dumps(compact, ensure_ascii=False)
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for block in short_term_context[-6:]:
            if block.get("user_content"):
                messages.append({"role": "user", "content": str(block["user_content"])})
            if block.get("assistant_content"):
                messages.append({"role": "assistant", "content": str(block["assistant_content"])})
        messages.append({"role": "user", "content": user_message})
        return messages
