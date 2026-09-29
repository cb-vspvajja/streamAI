from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from datetime import timedelta
from typing import Any

from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Cluster
from couchbase.exceptions import DocumentExistsException, DocumentNotFoundException
from couchbase.options import ClusterOptions

from .config import Settings


_LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")


class ViewerStateStore:
    """Couchbase KV store for demo authentication and resumable UI state.

    Agent Memory remains authoritative for users, sessions and memory blocks.
    This application document restores the low-latency active transcript and
    viewer login after the UI container is restarted.
    """

    PBKDF2_ITERATIONS = 240_000
    DOCUMENT_VERSION = 2

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        auth = PasswordAuthenticator(settings.cb_username, settings.cb_password)
        self.cluster = Cluster.connect(settings.cb_conn_string, ClusterOptions(auth))
        self.cluster.wait_until_ready(timedelta(seconds=20))
        self.bucket = self.cluster.bucket(settings.content_bucket)
        self.collection = self.bucket.scope(settings.viewers_scope).collection(
            settings.app_state_collection
        )

    def close(self) -> None:
        close = getattr(self.cluster, "close", None)
        if callable(close):
            close()

    def health(self) -> dict[str, Any]:
        try:
            result = self.cluster.ping()
            return {
                "healthy": True,
                "status": "connected",
                "bucket": self.settings.content_bucket,
                "collection": f"{self.settings.viewers_scope}.{self.settings.app_state_collection}",
                "details": str(result),
            }
        except Exception as exc:
            return {
                "healthy": False,
                "status": f"{type(exc).__name__}: {exc}",
                "bucket": self.settings.content_bucket,
                "collection": f"{self.settings.viewers_scope}.{self.settings.app_state_collection}",
            }

    @classmethod
    def normalize_login_id(cls, login_id: str) -> str:
        value = login_id.strip().lower()
        if not _LOGIN_RE.fullmatch(value):
            raise ValueError(
                "Login ID must be 3-64 characters using letters, numbers, '.', '_' or '-'."
            )
        return value

    @staticmethod
    def _doc_id(login_id: str) -> str:
        return f"viewer_state::{login_id}"

    @classmethod
    def _hash_pin(cls, pin: str, salt: bytes) -> str:
        digest = hashlib.pbkdf2_hmac(
            "sha256", pin.encode("utf-8"), salt, cls.PBKDF2_ITERATIONS
        )
        return base64.b64encode(digest).decode("ascii")

    def create_viewer(
        self,
        *,
        login_id: str,
        pin: str,
        name: str,
        user_id: str,
    ) -> dict[str, Any]:
        normalized = self.normalize_login_id(login_id)
        salt = secrets.token_bytes(16)
        now = time.time()
        doc = {
            "type": "streamai_viewer_state",
            "version": self.DOCUMENT_VERSION,
            "login_id": normalized,
            "name": name.strip() or normalized,
            "user_id": user_id,
            "pin_salt": base64.b64encode(salt).decode("ascii"),
            "pin_hash": self._hash_pin(pin, salt),
            "session_id": None,
            "session_number": 0,
            "session_active": False,
            "chat_log": [],
            "long_term_profile": [],
            "operation_count": 0,
            "short_term_submitted": 0,
            "long_term_submitted": 0,
            "profile_source": "empty",
            "last_catalogue_request": {},
            "created_at": now,
            "updated_at": now,
        }
        try:
            self.collection.insert(self._doc_id(normalized), doc)
        except DocumentExistsException as exc:
            raise ValueError(f"Login ID '{normalized}' already exists.") from exc
        return doc

    def authenticate(self, *, login_id: str, pin: str) -> dict[str, Any]:
        normalized = self.normalize_login_id(login_id)
        try:
            doc = self.collection.get(self._doc_id(normalized)).content_as[dict]
        except DocumentNotFoundException as exc:
            raise ValueError("Unknown login ID or PIN.") from exc
        salt = base64.b64decode(str(doc["pin_salt"]))
        expected = str(doc["pin_hash"])
        actual = self._hash_pin(pin, salt)
        if not hmac.compare_digest(expected, actual):
            raise ValueError("Unknown login ID or PIN.")
        return doc

    def save_state(self, login_id: str, updates: dict[str, Any]) -> None:
        normalized = self.normalize_login_id(login_id)
        key = self._doc_id(normalized)
        try:
            current = self.collection.get(key).content_as[dict]
        except DocumentNotFoundException as exc:
            raise ValueError(f"Viewer state for '{normalized}' no longer exists.") from exc
        protected = {
            "type",
            "version",
            "login_id",
            "pin_salt",
            "pin_hash",
            "created_at",
        }
        for field, value in updates.items():
            if field not in protected:
                current[field] = value
        current["updated_at"] = time.time()
        self.collection.upsert(key, current)

    def delete_viewer(self, login_id: str) -> None:
        normalized = self.normalize_login_id(login_id)
        try:
            self.collection.remove(self._doc_id(normalized))
        except DocumentNotFoundException:
            pass
