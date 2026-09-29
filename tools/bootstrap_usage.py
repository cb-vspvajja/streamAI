#!/usr/bin/env python3
"""Prepare Couchbase usage storage before any setup/model calls.

An old, still-running gateway can transfer its flushed Capella audit records
and reporting settings. No old local database is read or mounted by this app.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "usage-gateway"))
from comparison import validate_assumptions
from ledger import Ledger
from storage import CouchbaseStore, StorageUnavailable, STATE_TYPE


def fetch_existing_gateway():
    url = os.getenv("MODEL_USAGE_URL", "http://usage:8090").rstrip("/")
    try:
        with urllib.request.urlopen(url + "/metrics", timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise StorageUnavailable("Existing usage gateway is unhealthy; inspect its logs before upgrading") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (ConnectionRefusedError, socket.gaierror)):
            # A fresh install or a previously stopped meter has no live exporter.
            return None
        raise StorageUnavailable("Cannot inspect the previous usage gateway; history transfer stopped") from exc
    except TimeoutError as exc:
        raise StorageUnavailable("Previous usage gateway timed out; retry before replacing it") from exc


def flushed_snapshot(fetch, *, wait_seconds=120, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + wait_seconds
    saw_legacy = False
    while True:
        snapshot = fetch()
        if snapshot is None:
            if saw_legacy:
                raise StorageUnavailable("Previous usage gateway disappeared before its history transfer completed")
            return None
        if snapshot.get("persistence", {}).get("storage") == "couchbase":
            return snapshot
        replica = snapshot.get("replication")
        if not isinstance(replica, dict) or "pending" not in replica:
            raise StorageUnavailable("Unrecognized previous usage format; history transfer was not attempted")
        saw_legacy = True
        if replica["pending"] == 0 and not replica.get("error") and not snapshot.get("totals", {}).get("inFlight") and not snapshot.get("memoryApi", {}).get("inFlight"):
            return snapshot
        if clock() >= deadline:
            raise StorageUnavailable("Previous usage gateway still has pending records or calls. It has been left running; restore its Capella connection and rerun startup")
        sleep(2)


def adopt_or_initialize(store, snapshot):
    state = store.get_state()
    legacy = snapshot is not None and "replication" in snapshot
    if state is not None:
        if legacy and (not state.get("migration") or state.get("windowStartedAt") != snapshot.get("windowStartedAt")):
            raise StorageUnavailable("A previous meter and Couchbase usage settings both exist. Stop the previous meter after verifying its history; automatic merging is not safe")
        if not legacy:
            return "Existing Couchbase reporting window and history preserved"
    if not legacy:
        Ledger(store, recover=False)
        return "New Couchbase reporting window created; older unverified audit copies are retained separately"
    if store.namespace != "streamai":
        raise StorageUnavailable("Legacy history can only be adopted into the default streamai ledger")
    since = snapshot["windowStartedAt"]
    started = snapshot["instrumentationStartedAt"]
    if not all(isinstance(v, (int, float)) and 0 < v <= time.time() for v in (since, started)):
        raise StorageUnavailable("Previous meter returned invalid reporting boundaries")
    events = store.events(since, include_legacy=True)
    requests = [e for e in events if e.get("kind") == "model_request"]
    observed = {
        "requests": len(requests),
        "reportedInputTokens": sum((e.get("usage") or {}).get("inputTokens") or 0 for e in requests),
        "reportedOutputTokens": sum((e.get("usage") or {}).get("outputTokens") or 0 for e in requests),
    }
    expected = snapshot.get("totals", {})
    if any(value != expected.get(key) for key, value in observed.items()):
        raise StorageUnavailable("Capella audit-copy counts do not match the previous meter; startup stopped without replacing it")
    comparison = snapshot.get("comparison", {})
    chats = sum(e.get("kind") == "activity" and e.get("category") == "chat_turn" for e in events)
    if chats != comparison.get("completedChats"):
        raise StorageUnavailable("Capella chat counts do not match the previous meter; history transfer stopped")
    if state is not None:
        return "Previously verified history transfer rechecked; Couchbase settings preserved"
    now = time.time()
    state = {"type": STATE_TYPE, "schemaVersion": 3, "ledgerId": store.namespace,
             "instrumentationStartedAt": started, "windowStartedAt": since,
             "couchbasePrimarySince": now, "includeLegacyMirrors": True, "controlEvents": [],
             "historyCoverage": "Adopted flushed records from the previous meter; current-window request, token and chat counts verified. Pre-instrumentation and unreported token usage remain unavailable.",
             "migration": {"source": "verified_capella_audit_records", "adoptedAt": now,
                           "verifiedWindowTotals": observed, "verifiedCompletedChats": chats}}
    if comparison.get("assumptionsOrigin") == "operator_configured":
        state["comparisonAssumptions"] = validate_assumptions(comparison["assumptions"])
    store.initialize_state(state)
    return "Previous reporting window, assumptions and verified Capella usage history adopted"


def main():
    store = CouchbaseStore.from_env(provisioning=True)
    try:
        store.prepare_schema()
        snapshot = flushed_snapshot(fetch_existing_gateway, wait_seconds=int(os.getenv("USAGE_UPGRADE_WAIT_SECONDS", "120")))
        print(adopt_or_initialize(store, snapshot))
    finally:
        store.close()


if __name__ == "__main__":
    try:
        main()
    except (StorageUnavailable, ValueError, KeyError) as exc:
        print(f"Usage storage setup stopped: {exc}", file=sys.stderr)
        raise SystemExit(1)
