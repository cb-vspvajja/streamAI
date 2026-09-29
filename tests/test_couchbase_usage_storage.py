from __future__ import annotations
import copy
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch
import urllib.error

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "usage-gateway"), str(ROOT / "tests")]
from storage import CouchbaseStore, StorageUnavailable
from couchbase.exceptions import CasMismatchException, DocumentNotFoundException
from couchbase.n1ql import QueryScanConsistency
from usage_store_fake import FakeStore
from comparison import DEFAULTS
from ledger import Ledger

spec = importlib.util.spec_from_file_location("bootstrap_usage", ROOT / "tools/bootstrap_usage.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def sdk_store():
    cluster = MagicMock()
    store = CouchbaseStore(cluster)
    return store, store.collection


def test_event_writes_use_durability_and_no_local_store():
    store, collection = sdk_store()
    store.put_event({"id": "event-1", "timestamp": 1})
    key, doc, options = collection.upsert.call_args.args
    assert key == "measured-usage::streamai::event-1"
    assert doc["ledgerId"] == "streamai"
    assert doc["type"] == "streamai_measured_usage"
    assert options["durability"] == store.durability
    assert options["timeout"] == store.timeout


def test_recovery_retains_legacy_key_without_duplicate_record():
    store, collection = sdk_store()
    store.put_event({"id": "event-1", "_documentKey": "measured-usage::event-1"})
    key, doc, _ = collection.upsert.call_args.args
    assert key == "measured-usage::event-1"
    assert "_documentKey" not in doc
    assert doc["ledgerId"] == "streamai"


def test_queries_wait_for_indexes_and_limit_namespace():
    store, _ = sdk_store()
    store.cluster.query.return_value.rows.return_value = [{"event": {"id": "a"}, "documentKey": "k"}]
    assert store.events(10, include_legacy=True) == [{"id": "a", "_documentKey": "k"}]
    sql, options = store.cluster.query.call_args.args
    assert options["scan_consistency"] == QueryScanConsistency.REQUEST_PLUS
    assert options["named_parameters"]["since"] == 10
    assert options["named_parameters"]["legacy"] is True
    store.namespace = "isolated-test"
    store.events(include_legacy=True)
    assert store.cluster.query.call_args.args[1]["named_parameters"]["legacy"] is False


def test_concurrent_state_update_retries_cas_with_latest_document():
    store, collection = sdk_store()
    collection.get.side_effect = [
        SimpleNamespace(cas=1,content_as={dict:{"controlEvents":[]}}),
        SimpleNamespace(cas=2,content_as={dict:{"controlEvents":["another-writer"]}}),
    ]
    collection.replace.side_effect = [CasMismatchException(), None]
    def change(doc):
        doc["controlEvents"].append("this-writer")
        return doc
    assert store.update_state(change)["controlEvents"] == ["another-writer", "this-writer"]
    assert collection.replace.call_args.args[2]["cas"] == 2


def test_store_errors_hide_sensitive_details_and_never_fallback():
    store, collection = sdk_store()
    collection.upsert.side_effect = RuntimeError("private credentials")
    with pytest.raises(StorageUnavailable, match="Cannot persist") as exc:
        store.put_event({"id":"a"})
    assert "private" not in str(exc.value)
    collection.get.side_effect = DocumentNotFoundException()
    assert store.get_state() is None
    store.cluster.query.side_effect = RuntimeError("secret")
    with pytest.raises(StorageUnavailable, match="Cannot query"):
        store.events()


def legacy_fixture():
    store=FakeStore()
    now=time.time()
    store.records={
        "r1":{"id":"r1","kind":"model_request","timestamp":now,"operation":"chat/completions",
              "source":"agent_memory","status":"succeeded","usage":{"inputTokens":10,"outputTokens":2}},
        "a1":{"id":"a1","kind":"activity","timestamp":now,"category":"chat_turn"}
    }
    snapshot={"replication":{"pending":0,"error":None},"instrumentationStartedAt":now-100,
              "windowStartedAt":now-10,"totals":{"requests":1,"reportedInputTokens":10,"reportedOutputTokens":2},
              "comparison":{"completedChats":1,"assumptionsOrigin":"operator_configured","assumptions":{**DEFAULTS,"llmRequestsPerChat":2}}}
    return store,snapshot


def test_verified_history_transfer_preserves_window_totals_assumptions_and_is_repeatable():
    store,snapshot=legacy_fixture()
    bootstrap.adopt_or_initialize(store,snapshot)
    result=Ledger(store).summary()
    assert result["totals"]["reportedInputTokens"] == 10
    assert result["comparison"]["assumptions"]["llmRequestsPerChat"] == 2
    assert result["windowStartedAt"] == snapshot["windowStartedAt"]
    assert store.state["includeLegacyMirrors"] is True
    state=copy.deepcopy(store.state)
    bootstrap.adopt_or_initialize(store,snapshot)
    assert state == store.state


@pytest.mark.parametrize("field",["requests","reportedInputTokens","reportedOutputTokens","chats"])
def test_migration_aborts_on_missing_history(field):
    store,snapshot=legacy_fixture()
    if field == "chats":
        snapshot["comparison"]["completedChats"] += 1
    else:
        snapshot["totals"][field] += 1
    with pytest.raises(StorageUnavailable, match="counts do not match"):
        bootstrap.adopt_or_initialize(store,snapshot)
    assert store.state is None


def test_new_install_does_not_adopt_unverified_history_or_reset_on_restart():
    store=FakeStore()
    bootstrap.adopt_or_initialize(store,None)
    assert store.state["includeLegacyMirrors"] is False
    state=copy.deepcopy(store.state)
    bootstrap.adopt_or_initialize(store,None)
    assert store.state == state


def test_transfer_waits_for_pending_writes_and_aborts_if_they_never_flush():
    _,snapshot=legacy_fixture()
    pending={**snapshot,"replication":{"pending":1,"error":None}}
    ticks=iter([0,1,2])
    calls=iter([pending,snapshot])
    assert bootstrap.flushed_snapshot(lambda:next(calls),clock=lambda:next(ticks),sleep=lambda _:None) == snapshot
    with pytest.raises(StorageUnavailable,match="left running"):
        bootstrap.flushed_snapshot(lambda:pending,wait_seconds=0)


def test_transfer_detects_an_exporter_that_disappears_midway():
    _,snapshot=legacy_fixture()
    calls=iter([{**snapshot,"replication":{"pending":1}},None])
    with pytest.raises(StorageUnavailable,match="disappeared"):
        bootstrap.flushed_snapshot(lambda:next(calls),sleep=lambda _:None)


def test_unreachable_exporter_and_timeout_are_not_treated_the_same():
    with patch.object(bootstrap.urllib.request,"urlopen",side_effect=urllib.error.URLError(ConnectionRefusedError())):
        assert bootstrap.fetch_existing_gateway() is None
    with patch.object(bootstrap.urllib.request,"urlopen",side_effect=TimeoutError()):
        with pytest.raises(StorageUnavailable,match="timed out"):
            bootstrap.fetch_existing_gateway()
    with patch.object(bootstrap.urllib.request,"urlopen",side_effect=urllib.error.URLError(TimeoutError())):
        with pytest.raises(StorageUnavailable,match="Cannot inspect"):
            bootstrap.fetch_existing_gateway()


def test_failed_settings_write_preserves_the_window_and_audit_atomically():
    store=FakeStore()
    ledger=Ledger(store)
    before=copy.deepcopy(store.state)
    store.fail_writes=True
    with pytest.raises(StorageUnavailable):
        ledger.new_window()
    with pytest.raises(StorageUnavailable):
        ledger.set_comparison_assumptions(DEFAULTS)
    assert store.state == before


def test_runtime_composition_has_no_local_ledger_volume():
    compose=json.loads((ROOT/"compose.capella.yaml").read_text())
    assert "volumes" not in compose["services"]["usage"]
    assert "usage-ledger" not in compose["volumes"]
    dockerfile=(ROOT/"usage-gateway/Dockerfile").read_text()
    assert "VOLUME" not in dockerfile
    for path in (ROOT/"usage-gateway").glob("*.py"):
        assert "sqlite" not in path.read_text().lower()
