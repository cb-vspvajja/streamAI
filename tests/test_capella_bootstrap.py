"""Offline tests for the packaged Capella setup; no cluster or Docker required."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("capella_setup", ROOT / "tools/capella_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"CONTENT_BUCKET": "streaming", "AGENTMEMORY_BUCKET": "agent_memory", "EMBEDDING_MODEL": "nvidia/llama-3.2-nv-embedqa-1b-v2", "EMBEDDING_DIMENSIONS": "2048", "DATA_PROCESSING_MODE": "python_loader", "CAPELLA_WAIT_SECONDS": "20"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_bucket_names_are_quoted_and_validated(self):
        self.assertEqual(setup.identifier("media-demo"), "`media-demo`")
        with self.assertRaises(ValueError):
            setup.identifier("media`; DELETE FROM x")

    def test_server_defaults_do_not_cause_repeat_index_updates(self):
        expected = {"mapping": {"field": {"dims": 2048}}}
        actual = {"mapping": {"field": {"dims": 2048, "serverDefault": True}}, "store": {}}
        self.assertTrue(setup.contains_mapping(actual, expected))

    def test_merge_retains_unrelated_mapping_properties(self):
        actual = {"mapping": {"vector": {"dims": 768}, "extraField": True}}
        wanted = {"mapping": {"vector": {"dims": 2048}}}
        merged = setup.merge_mapping(actual, wanted)
        self.assertEqual(merged["mapping"]["vector"]["dims"], 2048)
        self.assertTrue(merged["mapping"]["extraField"])
        self.assertEqual(actual["mapping"]["vector"]["dims"], 768)

    def run_prepare(self, state, profiles=0, history=0, previous=None):
        cluster = MagicMock()
        cluster.bucket.return_value.scope.return_value.search_indexes.return_value.get_indexed_documents_count.return_value = 6
        final_state = {"total": 6, "invalid": 0}
        def counts(_, statement, **kw):
            return [profiles if ".profiles" in statement else history]
        with patch.object(setup, "connection", return_value=cluster), patch.object(setup, "catalogue_state", side_effect=[state, final_state]), patch.object(setup, "read_document", side_effect=lambda _, key: previous if key == "ingestion::latest" else None), patch.object(setup, "rows", side_effect=counts), patch.object(setup, "ensure_search_indexes"), patch.object(setup.subprocess, "run") as run:
            setup.prepare()
            return run.call_args_list

    def test_empty_target_loads_and_seeds(self):
        calls = self.run_prepare({"total": 0, "invalid": 0})
        loads = [c for c in calls if str(c.args[0][-1]).endswith("load_catalogue.py")]
        self.assertEqual(len(loads), 1)
        self.assertEqual(loads[0].kwargs["env"]["SEED_DEMO_VIEWERS"], "true")

    def test_existing_profiles_are_not_reseeded(self):
        calls = self.run_prepare({"total": 0, "invalid": 0}, profiles=2)
        load = next(c for c in calls if str(c.args[0][-1]).endswith("load_catalogue.py"))
        self.assertEqual(load.kwargs["env"]["SEED_DEMO_VIEWERS"], "false")

    def test_existing_history_is_not_reseeded(self):
        calls = self.run_prepare({"total": 0, "invalid": 0}, history=5)
        load = next(c for c in calls if str(c.args[0][-1]).endswith("load_catalogue.py"))
        self.assertEqual(load.kwargs["env"]["SEED_DEMO_VIEWERS"], "false")

    def test_restart_keeps_existing_catalogue(self):
        calls = self.run_prepare({"total": 6, "invalid": 0})
        self.assertEqual(len(calls), 1)  # Only idempotent schema provisioning.

    def test_existing_incompatible_vectors_stop_without_loading(self):
        with self.assertRaisesRegex(RuntimeError, "incompatible vectors"):
            self.run_prepare({"total": 6, "invalid": 6})

    def test_another_embedding_model_requires_explicit_migration(self):
        with self.assertRaisesRegex(RuntimeError, "another model"):
            self.run_prepare({"total": 6, "invalid": 0}, previous={"embeddingModel": "another-2048-model"})


class SearchManagementTests(unittest.TestCase):
    def setUp(self):
        try:
            from couchbase.management.search import SearchIndex
            from couchbase.exceptions import SearchIndexNotFoundException
        except ImportError:
            self.skipTest("Install the tools requirements to check the real SDK index type")
        self.SearchIndex = SearchIndex
        self.NotFound = SearchIndexNotFoundException

    def indexes(self):
        return [self.SearchIndex.from_json(json.loads(p.read_text())) for p in sorted((ROOT / "config/search").glob('streaming-*.json'))]

    def test_create_uses_scoped_sdk_indexes(self):
        cluster = MagicMock()
        manager = cluster.bucket.return_value.scope.return_value.search_indexes.return_value
        manager.get_index.side_effect = self.NotFound()
        setup.ensure_search_indexes(cluster, "streaming")
        self.assertEqual(manager.upsert_index.call_count, 2)
        names = [c.args[0] for c in cluster.bucket.return_value.scope.call_args_list]
        self.assertEqual(names, ["catalogue", "recommendations"])
        for call in manager.upsert_index.call_args_list:
            self.assertEqual(call.args[0].source_name, "streaming")
            self.assertIn('"dims": 2048', json.dumps(call.args[0].params))

    def test_matching_indexes_are_not_rebuilt(self):
        cluster = MagicMock()
        manager = cluster.bucket.return_value.scope.return_value.search_indexes.return_value
        manager.get_index.side_effect = self.indexes()
        setup.ensure_search_indexes(cluster, "streaming")
        manager.upsert_index.assert_not_called()

    def test_update_preserves_uuid_and_replica_settings(self):
        cluster = MagicMock()
        manager = cluster.bucket.return_value.scope.return_value.search_indexes.return_value
        existing = self.indexes()
        for idx in existing:
            idx.uuid = "existing-uuid"
            idx.plan_params = {"numReplicas": 1, "indexPartitions": 2}
            idx.params = json.loads(json.dumps(idx.params).replace('"dims": 2048', '"dims": 768'))
        manager.get_index.side_effect = existing
        setup.ensure_search_indexes(cluster, "streaming")
        self.assertEqual(manager.upsert_index.call_count, 2)
        for call in manager.upsert_index.call_args_list:
            self.assertEqual(call.args[0].uuid, "existing-uuid")
            self.assertEqual(call.args[0].plan_params["numReplicas"], 1)

    def test_permission_failure_does_not_create_or_replace_index(self):
        cluster = MagicMock()
        manager = cluster.bucket.return_value.scope.return_value.search_indexes.return_value
        manager.get_index.side_effect = RuntimeError("permission denied")
        with self.assertRaisesRegex(RuntimeError, "permission denied"):
            setup.ensure_search_indexes(cluster, "streaming")
        manager.upsert_index.assert_not_called()


if __name__ == "__main__":
    unittest.main()
