from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from agentmemory import AgentMemoryClient, ChatMessage

from .config import Settings
from .memory_policy import needs_summary
from .state_store import ViewerStateStore


@dataclass(frozen=True)
class TurnTarget:
    session: Any
    session_id: str


@dataclass
class DemoState:
    login_id: str | None = None
    user_id: str | None = None
    user_name: str | None = None
    session_id: str | None = None
    session_number: int = 0
    user: Any = None
    session: Any = None
    chat_log: list[dict[str, Any]] = field(default_factory=list)
    long_term_profile: list[dict[str, Any]] = field(default_factory=list)
    operation_count: int = 0
    short_term_submitted: int = 0
    long_term_submitted: int = 0
    short_term_ready: int = 0
    long_term_ready: int = 0
    persistence_pending: int = 0
    last_persistence_error: str | None = None
    profile_source: str = "empty"
    profile_memory_sync: dict[str, Any] = field(default_factory=dict)
    last_catalogue_request: dict[str, Any] = field(default_factory=dict)
    created_at: float | None = None


class AgentMemoryService:
    """Fast-path orchestrator over Couchbase Agent Memory.

    The active transcript supplies immediate short-term working memory. Durable
    profile facts are written to Agent Memory and reloaded at session boundaries.
    Semantic vector search is an explicit inspector operation, never a prerequisite
    for an ordinary chat turn.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = AgentMemoryClient(base_url=settings.agent_memory_url)
        self.state_store = ViewerStateStore(settings)
        self.state = DemoState()
        self._lock = threading.RLock()

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()
        self.state_store.close()

    def state_store_health(self) -> dict[str, Any]:
        return self.state_store.health()

    def health(self) -> dict[str, Any]:
        health = self.client.health_ping()
        status_obj = getattr(health, "overall_status", None)
        status = getattr(status_obj, "value", None) or getattr(health, "status", None)
        return {
            "healthy": str(status).lower() == "healthy" if status else True,
            "status": status or str(health),
            "url": self.settings.agent_memory_url,
        }

    def register_viewer(
        self,
        *,
        name: str,
        login_id: str,
        pin: str,
    ) -> dict[str, Any]:
        """Create a persistent demo login, Agent Memory user and first session."""
        with self._lock:
            normalized = self.state_store.normalize_login_id(login_id)
            resolved_user_id = f"viewer-{normalized}"
            # Create the login registry first so duplicate IDs fail before Agent Memory.
            self.state_store.create_viewer(
                login_id=normalized,
                pin=pin,
                name=name,
                user_id=resolved_user_id,
            )
            try:
                user = self.client.create_user(user_id=resolved_user_id, name=name)
            except Exception:
                # Do not leave an unusable login behind when Agent Memory user creation fails.
                self.state_store.delete_viewer(normalized)
                raise
            self.state = DemoState(
                login_id=normalized,
                user_id=resolved_user_id,
                user_name=name,
                user=user,
                created_at=time.time(),
            )
            self._create_session(label="Viewing discovery", save=False)
            self._save_state()
            data = self.snapshot(include_memories=False)
            data["memories"] = self.cached_memories()
            data["auth"] = {"login_id": normalized, "new_viewer": True}
            return data

    def reset(self, *, name: str, user_id: str | None = None) -> dict[str, Any]:
        """Backward-compatible fresh demo creation used by older scripts."""
        suffix = uuid.uuid4().hex[:8]
        login_id = user_id or f"viewer-{suffix}"
        return self.register_viewer(name=name, login_id=login_id, pin="1234")

    def login(self, *, login_id: str, pin: str) -> dict[str, Any]:
        """Restore a viewer and reattach to the same active Agent Memory session."""
        with self._lock:
            document = self.state_store.authenticate(login_id=login_id, pin=pin)
            user = self._retrieve_user(str(document["user_id"]))
            session = None
            session_id = document.get("session_id")
            session_active = bool(document.get("session_active") and session_id)
            if session_active:
                session = self._retrieve_session(user, str(document["user_id"]), str(session_id))

            self.state = DemoState(
                login_id=str(document["login_id"]),
                user_id=str(document["user_id"]),
                user_name=str(document.get("name") or document["login_id"]),
                session_id=str(session_id) if session_active else None,
                session_number=int(document.get("session_number") or 0),
                user=user,
                session=session,
                chat_log=list(document.get("chat_log") or []),
                long_term_profile=list(document.get("long_term_profile") or []),
                operation_count=int(document.get("operation_count") or 0) + 1,
                short_term_submitted=int(document.get("short_term_submitted") or 0),
                long_term_submitted=int(document.get("long_term_submitted") or 0),
                profile_source=str(document.get("profile_source") or "state_store_rehydrated"),
                profile_memory_sync=dict(document.get("profile_memory_sync") or {}),
                last_catalogue_request=dict(document.get("last_catalogue_request") or {}),
                created_at=document.get("created_at"),
            )
            if self.state.session is None:
                self._rehydrate_long_term_profile()
                self._create_session(label="Return viewing session", save=False)
            else:
                # Refresh durable profile from Agent Memory without vector search.
                self._rehydrate_long_term_profile()
            self._save_state()
            data = self.snapshot(include_memories=False)
            data["memories"] = self.cached_memories()
            data["auth"] = {"login_id": self.state.login_id, "resumed": session_active}
            return data

    def logout(self) -> dict[str, Any]:
        """Detach the UI without ending the active Agent Memory session."""
        with self._lock:
            if self.state.login_id:
                self._save_state()
            previous = self.state.login_id
            self.state = DemoState()
            return {"logged_out": True, "login_id": previous}

    @staticmethod
    def _call_first_available(attempts: list[tuple[Any, str, tuple[Any, ...], dict[str, Any]]], label: str) -> Any:
        errors: list[str] = []
        available: set[str] = set()
        for owner, method_name, args, kwargs in attempts:
            if owner is None:
                continue
            available.update(name for name in dir(owner) if label in name.lower())
            method = getattr(owner, method_name, None)
            if not callable(method):
                continue
            try:
                result = method(*args, **kwargs)
                if result is not None:
                    return result
            except TypeError as exc:
                # Generated SDK versions sometimes differ only in positional/keyword style.
                errors.append(f"{type(owner).__name__}.{method_name}: {exc}")
                continue
        raise RuntimeError(
            f"The installed Agent Memory SDK could not retrieve the existing {label}. "
            f"Available related methods: {', '.join(sorted(available)) or 'none'}. "
            f"Attempts: {'; '.join(errors) or 'no compatible method found'}"
        )

    def _retrieve_user(self, user_id: str) -> Any:
        users_manager = getattr(self.client, "users", None)
        if callable(users_manager):
            try:
                users_manager = users_manager()
            except TypeError:
                pass
        attempts = [
            (self.client, "get_user", (), {"user_id": user_id}),
            (self.client, "get_user", (user_id,), {}),
            (self.client, "user", (user_id,), {}),
            (self.client, "retrieve_user", (), {"user_id": user_id}),
            (self.client, "retrieve_user", (user_id,), {}),
            (users_manager, "get", (user_id,), {}),
            (users_manager, "get_user", (user_id,), {}),
        ]
        try:
            return self._call_first_available(attempts, "user")
        except RuntimeError as direct_error:
            for method_name in ("list_users", "get_users"):
                method = getattr(self.client, method_name, None)
                if not callable(method):
                    continue
                for args, kwargs in (((), {"limit": 200}), ((), {})):
                    try:
                        page = method(*args, **kwargs)
                    except TypeError:
                        continue
                    candidates = getattr(page, "users", None) or getattr(page, "items", None) or page
                    if not isinstance(candidates, (list, tuple)):
                        continue
                    for candidate in candidates:
                        if str(getattr(candidate, "user_id", "")) == user_id:
                            return candidate
            raise direct_error

    def _retrieve_session(self, user: Any, user_id: str, session_id: str) -> Any:
        sessions_manager = getattr(user, "sessions", None)
        if callable(sessions_manager):
            try:
                sessions_manager = sessions_manager()
            except TypeError:
                pass
        attempts = [
            (user, "get_session", (), {"session_id": session_id}),
            (user, "get_session", (session_id,), {}),
            (user, "session", (session_id,), {}),
            (user, "retrieve_session", (), {"session_id": session_id}),
            (user, "retrieve_session", (session_id,), {}),
            (sessions_manager, "get", (session_id,), {}),
            (sessions_manager, "get_session", (session_id,), {}),
            (self.client, "get_session", (), {"user_id": user_id, "session_id": session_id}),
            (self.client, "get_session", (user_id, session_id), {}),
        ]
        try:
            return self._call_first_available(attempts, "session")
        except RuntimeError as direct_error:
            for owner in (user, self.client):
                for method_name in ("list_sessions", "get_sessions"):
                    method = getattr(owner, method_name, None)
                    if not callable(method):
                        continue
                    argument_sets = [
                        ((), {"limit": 200}),
                        ((), {}),
                        ((), {"user_id": user_id, "limit": 200}),
                    ]
                    for args, kwargs in argument_sets:
                        try:
                            page = method(*args, **kwargs)
                        except TypeError:
                            continue
                        candidates = getattr(page, "sessions", None) or getattr(page, "items", None) or page
                        if not isinstance(candidates, (list, tuple)):
                            continue
                        for candidate in candidates:
                            if str(getattr(candidate, "session_id", "")) == session_id:
                                return candidate
            raise direct_error

    def start_new_session(self, label: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._require_demo()
            # One non-semantic list call rehydrates durable profile facts from the
            # Agent Memory source of truth. There is no enrichment polling and no
            # vector search on this path.
            self._rehydrate_long_term_profile()
            self._end_active_session_safely()
            self._create_session(label=label or "Return viewing session")
            data = self.snapshot(include_memories=False)
            data["memories"] = self.cached_memories()
            return data

    def _create_session(self, label: str, *, save: bool = True) -> None:
        self.state.session_number += 1
        suffix = uuid.uuid4().hex[:6]
        session_id = f"session-{self.state.session_number}-{suffix}"
        self.state.session = self.state.user.create_session(
            session_id=session_id,
            memory_blocks_ttl=self.settings.short_term_ttl_seconds,
            annotations={
                "channel": "streamai_ui",
                "journey": "streaming_companion",
                "label": label.replace(" ", "_").lower(),
            },
        )
        self.state.session_id = session_id
        self.state.chat_log = []
        self.state.short_term_submitted = 0
        self.state.short_term_ready = 0
        self.state.operation_count += 1
        if save:
            self._save_state()

    def end_session(self) -> dict[str, Any]:
        with self._lock:
            self._require_demo()
            self._end_active_session_safely()
            self._save_state()
            data = self.snapshot(include_memories=False)
            data["memories"] = self.cached_memories()
            return data

    def _end_active_session_safely(self) -> None:
        if self.state.session is None:
            return
        try:
            self.state.session.end()
        except Exception:
            pass
        finally:
            self.state.session = None
            self.state.session_id = None
            self.state.operation_count += 1

    def remember_catalogue_request(self, request: dict[str, Any]) -> None:
        with self._lock:
            self.state.last_catalogue_request = dict(request)
            self._save_state()

    def get_last_catalogue_request(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.state.last_catalogue_request)

    def recall_previous_turn(self) -> dict[str, Any] | None:
        """Return the latest verified prior user turn without model inference.

        The active transcript is the low-latency source.  If a new session has
        no local turns yet, the method performs one bounded Agent Memory list
        read and selects the newest retained conversation block.
        """
        with self._lock:
            self._require_active_session()
            entries = list(self.state.chat_log)
            for index in range(len(entries) - 1, -1, -1):
                item = entries[index]
                if item.get("role") != "user" or not str(item.get("content") or "").strip():
                    continue
                assistant_message = None
                if index + 1 < len(entries) and entries[index + 1].get("role") == "assistant":
                    assistant_message = entries[index + 1].get("content")
                return {
                    "userMessage": str(item.get("content")),
                    "assistantMessage": assistant_message,
                    "sessionId": self.state.session_id,
                    "source": "active_transcript",
                }

            try:
                page = self.state.user.list_memories(limit=200)
                blocks = [
                    block
                    for block in self._blocks_from_result(page)
                    if str(block.get("user_content") or "").strip()
                    and block.get("annotations", {}).get("memory_type") == "conversation"
                ]
                blocks.sort(
                    key=lambda block: (
                        str(block.get("created_at") or block.get("ingested_at") or ""),
                        str(block.get("block_id") or ""),
                    ),
                    reverse=True,
                )
                self.state.operation_count += 1
                if blocks:
                    block = blocks[0]
                    return {
                        "userMessage": str(block.get("user_content")),
                        "assistantMessage": block.get("assistant_content"),
                        "sessionId": block.get("session_id"),
                        "blockId": block.get("block_id"),
                        "source": "agent_memory",
                    }
            except Exception:
                # An explicit recall question should fail closed when the durable
                # memory service is unavailable, never ask the LLM to guess.
                return None
            return None

    def prepare_turn(self, query: str) -> tuple[dict[str, list[dict[str, Any]]], TurnTarget]:
        with self._lock:
            self._require_active_session()
            target = TurnTarget(session=self.state.session, session_id=str(self.state.session_id))
            return self.retrieve_context(query), target

    def retrieve_context(self, query: str) -> dict[str, list[dict[str, Any]]]:
        """Return context without blocking on summary or embedding enrichment."""
        with self._lock:
            self._require_active_session()
            short_blocks = self._chat_log_blocks()
            long_blocks = list(self.state.long_term_profile)

            if self.settings.semantic_retrieval_in_chat:
                # Optional advanced mode. The shipped demo keeps this off because
                # semantic retrieval should not be on the critical live-chat path.
                try:
                    if self.state.short_term_ready:
                        result = self.state.session.search_memory(
                            query=query,
                            filters={
                                "relevant_k": self.settings.semantic_result_limit,
                                "annotations": {"memory_scope": "short_term"},
                            },
                        )
                        short_blocks = self._blocks_from_result(result)
                        self.state.operation_count += 1
                    if self.state.long_term_ready:
                        result = self.state.session.search_memory(
                            query=query,
                            filters={
                                "session_ids": "all",
                                "relevant_k": self.settings.semantic_result_limit,
                                "annotations": {"memory_scope": "long_term"},
                            },
                        )
                        long_blocks = self._blocks_from_result(result)
                        self.state.operation_count += 1
                except Exception:
                    # Fast-path context remains available even when deep retrieval
                    # is warming up or temporarily unavailable.
                    pass

            return {"short_term": short_blocks, "long_term": long_blocks}

    def record_turn_local(
        self,
        *,
        target: TurnTarget,
        user_message: str,
        assistant_message: str,
        facts: list[str],
    ) -> None:
        """Update immediate working/profile memory before asynchronous enrichment."""
        with self._lock:
            if self.state.session_id == target.session_id:
                self.state.chat_log.extend(
                    [
                        {"role": "user", "content": user_message},
                        {"role": "assistant", "content": assistant_message},
                    ]
                )
            for fact in facts:
                if not any(item.get("fact") == fact for item in self.state.long_term_profile):
                    self.state.long_term_profile.append(
                        self._local_fact_block(fact, target.session_id)
                    )
            if facts:
                self.state.profile_source = "local_fast_path_pending_agent_memory"
            self._save_state()

    def persist_turn(
        self,
        *,
        target: TurnTarget,
        user_message: str,
        assistant_message: str,
        facts: list[str],
    ) -> dict[str, list[str]]:
        """Submit blocks to Agent Memory with asynchronous enrichment enabled.

        The API acceptance is fast; embedding and summarisation continue in the
        Agent Memory queue and never block token generation.
        """
        with self._lock:
            self.state.persistence_pending += 1
            self.state.last_persistence_error = None
        try:
            conversation_response = target.session.add_memory(
                messages=[
                    ChatMessage(
                        user_content=user_message,
                        assistant_content=assistant_message,
                    )
                ],
                annotations={
                    "memory_scope": "short_term",
                    "memory_type": "conversation",
                    "source": "streamai_ui_streaming",
                },
                context_required=needs_summary(user_message, assistant_message, getattr(self.settings, "memory_summary_policy", "selective")),
                memory_block_ttl=self.settings.short_term_ttl_seconds,
                async_processing=self.settings.memory_async_processing,
            )
            conversation_ids = list(
                getattr(conversation_response, "block_ids", []) or []
            )

            fact_ids: list[str] = []
            if facts:
                fact_response = target.session.add_memory(
                    facts=facts,
                    annotations={
                        "memory_scope": "long_term",
                        "memory_type": "viewer_preference",
                        "source": "streamai_viewer_fact_extraction",
                        "importance": "high",
                    },
                    context_required=False,
                    memory_block_ttl=0,
                    async_processing=self.settings.memory_async_processing,
                )
                fact_ids = list(getattr(fact_response, "block_ids", []) or [])

            with self._lock:
                self.state.short_term_submitted += max(len(conversation_ids), 1)
                self.state.long_term_submitted += max(len(fact_ids), len(facts))
                self.state.operation_count += 1 + (1 if facts else 0)
                if facts:
                    self.state.profile_source = "agent_memory_submitted"
                self._save_state()
            return {
                "conversation_block_ids": conversation_ids,
                "fact_block_ids": fact_ids,
            }
        except Exception as exc:
            with self._lock:
                self.state.last_persistence_error = f"{type(exc).__name__}: {exc}"
                self._save_state()
            raise
        finally:
            with self._lock:
                self.state.persistence_pending = max(self.state.persistence_pending - 1, 0)

    def sync_operational_profile(self, facts: list[str], *, apply: bool = True,
                                 expected_login_id: str | None = None) -> dict[str, Any]:
        from .profile_memory_sync import SOURCE, reconcile
        with self._lock:
            if expected_login_id is not None and self.state.login_id != expected_login_id:
                return {"status": "deferred", "expectedFacts": len(facts),
                        "message": "Viewer changed; these preferences will synchronise on their next login."}
            self._require_demo()
            try:
                result, stored = reconcile(self.state.user, self.state.user_id, facts,
                    apply=apply, blocks_from_result=self._blocks_from_result, is_ready=self._block_is_ready)
                # Replace this mirror's local entries so removed preferences do
                # not survive merely because they were cached in an older login.
                other = [b for b in self.state.long_term_profile if (b.get("annotations") or {}).get("source") != SOURCE]
                self.state.long_term_profile = [*other, *stored]
                self.state.profile_memory_sync = {**result, "checkedAt": time.time()}
            except Exception as exc:
                self.state.profile_memory_sync = {"status": "error", "expectedFacts": len(facts),
                    "error": type(exc).__name__, "message": "Operational preferences remain saved; Agent Memory synchronisation needs a retry.",
                    "checkedAt": time.time()}
            self._save_state()
            return dict(self.state.profile_memory_sync)

    def record_external_facts(self, facts: list[str], source: str = "viewer_interaction") -> dict[str, list[str]]:
        """Persist facts produced outside chat, such as Like/Dislike actions.

        The local profile cache is updated first so the memory inspector reflects
        the action immediately; Agent Memory enrichment remains asynchronous.
        """
        cleaned = [str(fact).strip() for fact in facts if str(fact).strip()]
        if not cleaned:
            return {"fact_block_ids": []}
        with self._lock:
            self._require_active_session()
            for fact in cleaned:
                if not any(item.get("fact") == fact for item in self.state.long_term_profile):
                    self.state.long_term_profile.append(
                        self._local_fact_block(fact, str(self.state.session_id), source=source)
                    )
            self.state.profile_source = "local_fast_path_pending_agent_memory"
            self._save_state()
            target = self.state.session
        try:
            response = target.add_memory(
                facts=cleaned,
                annotations={
                    "memory_scope": "long_term",
                    "memory_type": "viewer_preference",
                    "source": source,
                    "importance": "high",
                },
                context_required=False,
                memory_block_ttl=0,
                async_processing=self.settings.memory_async_processing,
            )
            block_ids = list(getattr(response, "block_ids", []) or [])
            with self._lock:
                self.state.long_term_submitted += max(len(block_ids), len(cleaned))
                self.state.operation_count += 1
                self.state.profile_source = "agent_memory_submitted"
                self._save_state()
            return {"fact_block_ids": block_ids}
        except Exception as exc:
            with self._lock:
                self.state.last_persistence_error = f"{type(exc).__name__}: {exc}"
                self._save_state()
            raise

    def _rehydrate_long_term_profile(self) -> None:
        existing = list(self.state.long_term_profile)
        try:
            page = self.state.user.list_memories(limit=200)
            blocks = self._blocks_from_result(page)
            durable = [
                block
                for block in blocks
                if block.get("annotations", {}).get("memory_scope") == "long_term"
                and block.get("fact")
            ]
            merged: list[dict[str, Any]] = []
            for block in [*durable, *existing]:
                if not any(item.get("fact") == block.get("fact") for item in merged):
                    merged.append(block)
            self.state.long_term_profile = merged
            self.state.profile_source = "agent_memory_rehydrated" if durable else self.state.profile_source
            self.state.operation_count += 1
        except Exception:
            # Retain the fast-path profile if Agent Memory is momentarily busy.
            self.state.long_term_profile = existing

    def _chat_log_blocks(self) -> list[dict[str, Any]]:
        pairs: list[dict[str, Any]] = []
        entries = self.state.chat_log[-12:]
        for index in range(0, len(entries), 2):
            user = entries[index] if index < len(entries) else {}
            assistant = entries[index + 1] if index + 1 < len(entries) else {}
            pairs.append(
                {
                    "block_id": None,
                    "session_id": self.state.session_id,
                    "fact": None,
                    "user_content": user.get("content") if user.get("role") == "user" else None,
                    "assistant_content": assistant.get("content") if assistant.get("role") == "assistant" else None,
                    "summary": None,
                    "status": "working_memory",
                    "rel_score": None,
                    "annotations": {
                        "memory_scope": "short_term",
                        "memory_type": "conversation",
                        "source": "active_transcript_fast_path",
                    },
                    "created_at": None,
                    "ingested_at": None,
                    "expires_at": None,
                }
            )
        return pairs

    @staticmethod
    def _local_fact_block(
        fact: str, session_id: str, *, source: str = "fast_profile_cache"
    ) -> dict[str, Any]:
        return {
            "block_id": None,
            "session_id": session_id,
            "fact": fact,
            "user_content": None,
            "assistant_content": None,
            "summary": None,
            "status": "submitted",
            "rel_score": None,
            "annotations": {
                "memory_scope": "long_term",
                "memory_type": "viewer_preference",
                "source": source,
            },
            "created_at": None,
            "ingested_at": None,
            "expires_at": None,
        }

    def cached_memories(self) -> dict[str, Any]:
        with self._lock:
            short = self._chat_log_blocks()
            long = list(self.state.long_term_profile)
            all_blocks = [*short, *long]
            return {
                "all": all_blocks,
                "short_term": short,
                "long_term": long,
                "other": [],
                "pending": all_blocks,
                "counts": {
                    "total": len(all_blocks),
                    "short_term": len(short),
                    "long_term": len(long),
                    "pending": len(all_blocks),
                    "ready": 0,
                },
                "view": "fast_path_cache",
            }

    def manual_search(self, query: str, scope: str) -> dict[str, Any]:
        with self._lock:
            self._require_active_session()
            if scope == "short":
                result = self.state.session.search_memory(
                    query=query,
                    filters={
                        "relevant_k": self.settings.semantic_result_limit,
                        "annotations": {"memory_scope": "short_term"},
                    },
                )
            elif scope == "long":
                result = self.state.session.search_memory(
                    query=query,
                    filters={
                        "session_ids": "all",
                        "relevant_k": self.settings.semantic_result_limit,
                        "annotations": {"memory_scope": "long_term"},
                    },
                )
            else:
                result = self.state.session.search_memory(
                    query=query,
                    filters={
                        "session_ids": "all",
                        "relevant_k": self.settings.semantic_result_limit,
                    },
                )
            self.state.operation_count += 1
            return {"scope": scope, "query": query, "blocks": self._blocks_from_result(result)}

    def snapshot(self, *, include_memories: bool = False) -> dict[str, Any]:
        with self._lock:
            data: dict[str, Any] = {
                "ready": self.state.user is not None,
                "authenticated": self.state.login_id is not None,
                "login_id": self.state.login_id,
                "user_id": self.state.user_id,
                "user_name": self.state.user_name,
                "session_id": self.state.session_id,
                "session_number": self.state.session_number,
                "session_active": self.state.session is not None,
                "short_term_ttl_seconds": self.settings.short_term_ttl_seconds,
                "chat_log": list(self.state.chat_log),
                "operation_count": self.state.operation_count,
                "short_term_submitted": self.state.short_term_submitted,
                "long_term_submitted": self.state.long_term_submitted,
                "short_term_ready": self.state.short_term_ready,
                "long_term_ready": self.state.long_term_ready,
                "memory_async_processing": self.settings.memory_async_processing,
                "semantic_retrieval_in_chat": self.settings.semantic_retrieval_in_chat,
                "persistence_pending": self.state.persistence_pending,
                "last_persistence_error": self.state.last_persistence_error,
                "profile_source": self.state.profile_source,
                "profile_memory_sync": dict(self.state.profile_memory_sync),
                "last_catalogue_request": dict(self.state.last_catalogue_request),
                "created_at": self.state.created_at,
            }
            if include_memories and self.state.user is not None:
                data["memories"] = self.list_memories()
            return data

    def list_memories(self) -> dict[str, Any]:
        with self._lock:
            self._require_demo()
            page = self.state.user.list_memories(limit=200)
            blocks = self._blocks_from_result(page)
            short = [b for b in blocks if b.get("annotations", {}).get("memory_scope") == "short_term"]
            long = [b for b in blocks if b.get("annotations", {}).get("memory_scope") == "long_term"]
            other = [b for b in blocks if b not in short and b not in long]
            pending = [b for b in blocks if not self._block_is_ready(b)]
            ready_short = [
                b
                for b in short
                if self._block_is_ready(b) and b.get("session_id") == self.state.session_id
            ]
            ready_long = [b for b in long if self._block_is_ready(b)]
            self.state.short_term_ready = len(ready_short)
            self.state.long_term_ready = len(ready_long)
            merged_long: list[dict[str, Any]] = []
            for block in [*long, *self.state.long_term_profile]:
                if not any(item.get("fact") == block.get("fact") for item in merged_long):
                    merged_long.append(block)
            if merged_long:
                self.state.long_term_profile = merged_long
                self.state.profile_source = "agent_memory_rehydrated" if long else self.state.profile_source
            merged_all = [*short, *merged_long, *other]
            merged_pending = [b for b in merged_all if not self._block_is_ready(b)]
            return {
                "all": merged_all,
                "short_term": short,
                "long_term": merged_long,
                "other": other,
                "pending": merged_pending,
                "counts": {
                    "total": len(merged_all),
                    "short_term": len(short),
                    "long_term": len(merged_long),
                    "pending": len(merged_pending),
                    "ready": len(merged_all) - len(merged_pending),
                },
                "view": "agent_memory_plus_immediate_profile_cache",
            }

    @staticmethod
    def _block_is_ready(block: dict[str, Any]) -> bool:
        status = str(block.get("status") or "").lower()
        if any(token in status for token in ("pending", "queued", "processing", "extracting", "failed", "submitted")):
            return False
        if any(token in status for token in ("ready", "complete", "completed", "processed", "success")):
            return True
        return bool(block.get("ingested_at") or block.get("summary"))

    def _save_state(self) -> None:
        if not self.state.login_id:
            return
        self.state_store.save_state(
            self.state.login_id,
            {
                "name": self.state.user_name,
                "user_id": self.state.user_id,
                "session_id": self.state.session_id,
                "session_number": self.state.session_number,
                "session_active": self.state.session is not None,
                "chat_log": list(self.state.chat_log),
                "long_term_profile": list(self.state.long_term_profile),
                "operation_count": self.state.operation_count,
                "short_term_submitted": self.state.short_term_submitted,
                "long_term_submitted": self.state.long_term_submitted,
                "profile_source": self.state.profile_source,
                "profile_memory_sync": dict(self.state.profile_memory_sync),
                "last_catalogue_request": dict(self.state.last_catalogue_request),
                "created_at": self.state.created_at,
            },
        )

    def _require_demo(self) -> None:
        if self.state.user is None:
            raise RuntimeError("Create or sign in to a viewer first.")

    def _require_active_session(self) -> None:
        self._require_demo()
        if self.state.session is None:
            raise RuntimeError("The current session has ended. Start a new session.")

    @staticmethod
    def _blocks_from_result(result: Any) -> list[dict[str, Any]]:
        blocks = getattr(result, "memory_blocks", None)
        if blocks is None:
            blocks = getattr(result, "blocks", None)
        if blocks is None:
            blocks = []
        return [AgentMemoryService._serialize_block(block) for block in blocks]

    @staticmethod
    def _serialize_block(block: Any) -> dict[str, Any]:
        message = getattr(block, "message", None)
        score = getattr(block, "rel_score", None)
        annotations = getattr(block, "annotations", None) or {}
        return {
            "block_id": getattr(block, "block_id", None) or getattr(block, "id", None),
            "session_id": getattr(block, "session_id", None),
            "fact": getattr(block, "fact", None),
            "user_content": getattr(message, "user_content", None) if message else None,
            "assistant_content": getattr(message, "assistant_content", None) if message else None,
            "summary": getattr(block, "summary", None),
            "status": str(getattr(block, "status", "")) or None,
            "rel_score": float(score) if isinstance(score, (int, float)) else None,
            "annotations": dict(annotations) if isinstance(annotations, dict) else {},
            "created_at": AgentMemoryService._stringify_time(getattr(block, "created_at", None)),
            "ingested_at": AgentMemoryService._stringify_time(getattr(block, "ingested_at", None)),
            "expires_at": AgentMemoryService._stringify_time(getattr(block, "expires_at", None)),
        }

    @staticmethod
    def _stringify_time(value: Any) -> str | None:
        if value is None:
            return None
        isoformat = getattr(value, "isoformat", None)
        return isoformat() if callable(isoformat) else str(value)
