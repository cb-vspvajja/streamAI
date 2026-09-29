"""Test-only store double. Runtime has no in-memory or local database fallback."""
import copy
import threading
from storage import StorageUnavailable


class FakeStore:
    namespace = "streamai"
    keyspace = "`streaming`.`telemetry`.`ai_metrics`"
    def __init__(self):
        self.state = None
        self.records = {}
        self.lock = threading.Lock()
        self.fail_reads = False
        self.fail_writes = False
    def get_state(self):
        if self.fail_reads:
            raise StorageUnavailable("Cannot read Couchbase usage settings")
        with self.lock:
            return copy.deepcopy(self.state)
    def initialize_state(self, state):
        with self.lock:
            if self.state is None:
                self.state = copy.deepcopy(state)
        return self.get_state()
    def update_state(self, transform):
        if self.fail_writes:
            raise StorageUnavailable("Cannot save Couchbase usage settings")
        with self.lock:
            self.state = transform(copy.deepcopy(self.state))
            return copy.deepcopy(self.state)
    def put_event(self, event):
        if self.fail_writes:
            raise StorageUnavailable("Cannot persist Couchbase usage event")
        with self.lock:
            self.records[event["id"]] = copy.deepcopy(event)
    def events(self, since=0, *, include_legacy=False):
        if self.fail_reads:
            raise StorageUnavailable("Cannot query Couchbase usage records")
        with self.lock:
            return [copy.deepcopy(e) for e in self.records.values() if e["timestamp"] >= since]
    def close(self):
        pass
