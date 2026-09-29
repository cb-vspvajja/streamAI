"""Couchbase is the only persistent store for usage events and control state."""
from __future__ import annotations

import copy
import os
import re
from datetime import timedelta

from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Cluster
from couchbase.durability import DurabilityLevel, ServerDurability
from couchbase.exceptions import CasMismatchException, DocumentExistsException, DocumentNotFoundException
from couchbase.n1ql import QueryScanConsistency
from couchbase.options import ClusterOptions, GetOptions, InsertOptions, QueryOptions, ReplaceOptions, UpsertOptions

EVENT_TYPE = "streamai_measured_usage"
STATE_TYPE = "streamai_usage_state"


class StorageUnavailable(RuntimeError):
    """Safe public error: underlying exceptions may contain credentials/hosts."""


def identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid Couchbase ledger identifier")
    return "`" + value + "`"


class CouchbaseStore:
    def __init__(self, cluster, bucket="streaming", namespace="streamai"):
        identifier(bucket)
        identifier(namespace)
        self.cluster = cluster
        self.bucket = bucket
        self.namespace = namespace
        self.keyspace = f"{identifier(bucket)}.`telemetry`.`ai_metrics`"
        self.collection = cluster.bucket(bucket).scope("telemetry").collection("ai_metrics")
        self.state_key = f"measured-usage-state::{namespace}"
        self.durability = ServerDurability(DurabilityLevel.MAJORITY_AND_PERSIST_TO_ACTIVE)
        self.timeout = timedelta(seconds=5)

    @classmethod
    def from_env(cls, *, provisioning=False):
        uri = os.environ.get("CB_CONN_STRING", "")
        if not uri.startswith("couchbases://"):
            raise StorageUnavailable("Couchbase usage storage requires a TLS connection string")
        prefix = "CB_PROVISION_" if provisioning else "CB_"
        user = os.environ.get(prefix + "USERNAME") or os.environ.get("CB_USERNAME", "")
        password = os.environ.get(prefix + "PASSWORD") or os.environ.get("CB_PASSWORD", "")
        cluster = None
        try:
            options = ClusterOptions(PasswordAuthenticator(user, password))
            options.apply_profile("wan_development")
            cluster = Cluster.connect(uri, options)
            cluster.wait_until_ready(timedelta(seconds=30))
            return cls(cluster, os.environ.get("CONTENT_BUCKET", "streaming"),
                       os.environ.get("USAGE_LEDGER_ID", "streamai"))
        except Exception as exc:
            if cluster is not None:
                cluster.close()
            raise StorageUnavailable("Cannot connect to Couchbase usage storage") from exc

    def close(self):
        self.cluster.close()

    def prepare_schema(self):
        b = identifier(self.bucket)
        statements = [
            f"CREATE SCOPE {b}.`telemetry` IF NOT EXISTS",
            f"CREATE COLLECTION {self.keyspace} IF NOT EXISTS",
            f"CREATE INDEX IF NOT EXISTS ix_usage_ledger_time ON {self.keyspace}(`type`, timestamp, ledgerId, id)",
        ]
        try:
            for statement in statements:
                self.cluster.query(statement, QueryOptions(timeout=timedelta(seconds=120))).execute()
        except Exception as exc:
            raise StorageUnavailable("Cannot provision the Couchbase usage collection/index") from exc

    def get_state(self):
        try:
            return self.collection.get(self.state_key, GetOptions(timeout=self.timeout)).content_as[dict]
        except DocumentNotFoundException:
            return None
        except Exception as exc:
            raise StorageUnavailable("Cannot read Couchbase usage settings") from exc

    def initialize_state(self, state):
        try:
            self.collection.insert(self.state_key, state,
                InsertOptions(timeout=self.timeout, durability=self.durability))
        except DocumentExistsException:
            pass
        except Exception as exc:
            raise StorageUnavailable("Cannot initialize Couchbase usage settings") from exc
        return self.get_state()

    def update_state(self, transform):
        for _ in range(10):
            try:
                result = self.collection.get(self.state_key, GetOptions(timeout=self.timeout))
                doc = transform(copy.deepcopy(result.content_as[dict]))
                self.collection.replace(self.state_key, doc,
                    ReplaceOptions(cas=result.cas, timeout=self.timeout, durability=self.durability))
                return doc
            except CasMismatchException:
                continue
            except ValueError:
                raise
            except Exception as exc:
                raise StorageUnavailable("Cannot save Couchbase usage settings") from exc
        raise StorageUnavailable("Concurrent usage-settings update; retry the operation")

    def put_event(self, event):
        doc = copy.deepcopy(event)
        old_key = doc.pop("_documentKey", None)
        # Recovered legacy records retain their original key: no duplicate counting.
        key = old_key or f"measured-usage::{self.namespace}::{doc['id']}"
        doc.update(type=EVENT_TYPE, ledgerId=self.namespace)
        try:
            self.collection.upsert(key, doc,
                UpsertOptions(timeout=self.timeout, durability=self.durability))
        except Exception as exc:
            raise StorageUnavailable("Cannot persist Couchbase usage event") from exc

    def events(self, since=0, *, include_legacy=False):
        # Request-plus prevents a dashboard refresh from missing acknowledged writes.
        sql = f"""SELECT e AS event, META(e).id AS documentKey FROM {self.keyspace} AS e
            WHERE e.type = $eventType AND e.timestamp >= $since
              AND (e.ledgerId = $ledgerId OR ($legacy AND e.ledgerId IS MISSING))
            ORDER BY e.timestamp, e.id"""
        try:
            rows = self.cluster.query(sql, QueryOptions(
                readonly=True, timeout=timedelta(seconds=10),
                scan_consistency=QueryScanConsistency.REQUEST_PLUS,
                named_parameters={"eventType": EVENT_TYPE, "since": since,
                                  "ledgerId": self.namespace,
                                  "legacy": bool(include_legacy and self.namespace == "streamai")}))
            return [{**r["event"], "_documentKey": r["documentKey"]} for r in rows.rows()]
        except Exception as exc:
            raise StorageUnavailable("Cannot query Couchbase usage records") from exc
