"""Capella setup and verification, executed inside the packaged tools container."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import timedelta
from urllib.error import URLError, HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Use letters, numbers, underscores and hyphens in demo bucket names")
    return "`" + value + "`"


def connection():
    from couchbase.auth import PasswordAuthenticator
    from couchbase.cluster import Cluster
    from couchbase.options import ClusterOptions
    opts = ClusterOptions(PasswordAuthenticator(os.environ["CB_PROVISION_USERNAME"], os.environ["CB_PROVISION_PASSWORD"]))
    opts.apply_profile("wan_development")
    cluster = Cluster.connect(os.environ["CB_CONN_STRING"], opts)
    cluster.wait_until_ready(timedelta(seconds=30))
    return cluster


def rows(cluster, statement: str, **parameters):
    from couchbase.options import QueryOptions
    from couchbase.n1ql import QueryScanConsistency
    return list(cluster.query(statement, QueryOptions(readonly=True, timeout=timedelta(seconds=60), scan_consistency=QueryScanConsistency.REQUEST_PLUS, named_parameters=parameters)).rows())


def catalogue_state(cluster, bucket: str) -> dict:
    return rows(cluster, f"SELECT COUNT(1) AS total, SUM(CASE WHEN ISARRAY(t.embedding) AND ARRAY_LENGTH(t.embedding) = $dims THEN 0 ELSE 1 END) AS invalid FROM {identifier(bucket)}.catalogue.titles AS t", dims=int(os.environ["EMBEDDING_DIMENSIONS"]))[0]


def contains_mapping(actual, required) -> bool:
    if isinstance(required, dict):
        return isinstance(actual, dict) and all(key in actual and contains_mapping(actual[key], value) for key, value in required.items())
    if isinstance(required, list):
        return isinstance(actual, list) and all(any(contains_mapping(item, needed) for item in actual) for needed in required)
    return actual == required


def merge_mapping(actual, required):
    if isinstance(actual, dict) and isinstance(required, dict):
        result = copy.deepcopy(actual)
        for key, value in required.items():
            result[key] = merge_mapping(result.get(key), value)
        return result
    return copy.deepcopy(required)


def ensure_search_indexes(cluster, bucket: str) -> None:
    from couchbase.management.search import SearchIndex
    from couchbase.exceptions import SearchIndexNotFoundException
    for scope, filename in [("catalogue", "streaming-catalogue-search.json"), ("recommendations", "streaming-plan-cache.json")]:
        definition = json.loads((ROOT / "config/search" / filename).read_text())
        definition["sourceName"] = bucket
        desired = SearchIndex.from_json(definition)
        manager = cluster.bucket(bucket).scope(scope).search_indexes()
        try:
            existing = manager.get_index(desired.name)
        except SearchIndexNotFoundException:
            manager.upsert_index(desired)
            print(f"Created Search index: {bucket}.{scope}.{desired.name}", flush=True)
            continue
        if existing.source_name != bucket:
            raise RuntimeError(f"Existing index {desired.name} uses another source bucket; resolve that before startup")
        if contains_mapping(existing.params, desired.params):
            print(f"Search index already configured: {scope}.{desired.name}", flush=True)
            continue
        desired.uuid = existing.uuid
        desired.source_uuid = existing.source_uuid
        desired.plan_params = existing.plan_params
        desired.params = merge_mapping(existing.params, desired.params)
        manager.upsert_index(desired)
        print(f"Updated Search mapping to 2048 dimensions: {scope}.{desired.name}", flush=True)


def read_document(collection, key: str):
    from couchbase.exceptions import DocumentNotFoundException
    try:
        return collection.get(key).content_as[dict]
    except DocumentNotFoundException:
        return None


def prepare() -> None:
    subprocess.run([sys.executable, str(ROOT / "tools/provision_content_plane.py")], check=True)
    cluster = connection()
    bucket = os.environ["CONTENT_BUCKET"]
    b = identifier(bucket)
    try:
        # Prove the memory bucket exists without writing a test record.
        cluster.bucket(os.environ["AGENTMEMORY_BUCKET"]).default_collection().exists("streamai::connection-check")
        jobs = cluster.bucket(bucket).scope("operations").collection("ingestion_jobs")
        manifest_key = "streamai::capella-bootstrap"
        manifest = read_document(jobs, manifest_key)
        state = catalogue_state(cluster, bucket)
        model = os.environ["EMBEDDING_MODEL"]
        previous = read_document(jobs, "ingestion::latest")
        if state["total"] and previous and previous.get("embeddingModel") and previous["embeddingModel"] != model:
            raise RuntimeError("Existing catalogue was embedded with another model. Use a fresh demo bucket or deliberately re-embed it; startup has retained the existing data.")
        if manifest and manifest.get("embeddingModel") != model:
            raise RuntimeError("This bucket's bootstrap record uses another embedding model. Use a fresh demo bucket or migrate its embeddings first.")
        managed = os.environ["DATA_PROCESSING_MODE"] == "capella_workflow"
        retry_load = bool(manifest and manifest.get("stage") == "loading")
        if state["total"] and state.get("invalid") and not managed and not retry_load:
            raise RuntimeError(f"Existing catalogue contains {state['invalid']} missing/incompatible vectors. It was retained. Use a fresh target or regenerate vectors before starting.")
        ensure_search_indexes(cluster, bucket)
        if not state["total"] or retry_load:
            profile_count = rows(cluster, f"SELECT RAW COUNT(1) FROM {b}.viewers.profiles")[0]
            history_count = rows(cluster, f"SELECT RAW COUNT(1) FROM {b}.viewers.watch_history WHERE viewerId IS NOT MISSING")[0]
            env = os.environ.copy()
            env["SEED_DEMO_VIEWERS"] = "true" if profile_count == 0 and history_count == 0 else "false"
            jobs.upsert(manifest_key, {"type": "streamai_capella_bootstrap", "stage": "loading", "embeddingModel": model, "dimensions": 2048, "updatedAt": time.time()})
            subprocess.run([sys.executable, str(ROOT / "tools/load_catalogue.py")], env=env, check=True)
            subprocess.run([sys.executable, str(ROOT / "tools/upgrade_v130.py")], env=env, check=True)
            jobs.upsert(manifest_key, {"type": "streamai_capella_bootstrap", "stage": "loaded", "embeddingModel": model, "dimensions": 2048, "updatedAt": time.time()})
        else:
            print(f"Keeping existing catalogue ({state['total']} titles) and viewer history.", flush=True)

        timeout = int(os.environ.get("CAPELLA_WAIT_SECONDS", "300"))
        deadline = time.monotonic() + timeout
        last_error = "waiting for vectors and Search indexes"
        print("Waiting for embeddings and Search indexing...", flush=True)
        while time.monotonic() < deadline:
            try:
                state = catalogue_state(cluster, bucket)
                catalogue_manager = cluster.bucket(bucket).scope("catalogue").search_indexes()
                plan_manager = cluster.bucket(bucket).scope("recommendations").search_indexes()
                indexed = catalogue_manager.get_indexed_documents_count("streaming-catalogue-search")
                plan_manager.get_indexed_documents_count("streaming-plan-cache")
                if state["total"] > 0 and not state.get("invalid") and indexed >= state["total"]:
                    print(f"Catalogue ready: {state['total']} titles with 2048-dimension vectors; both Search indexes available.", flush=True)
                    return
                last_error = f"titles={state['total']}, incompatible/missing vectors={state.get('invalid') or 0}, indexed={indexed}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            print(last_error, flush=True)
            time.sleep(5)
        extra = " Check the configured Capella workflow is running and writing embedding." if managed else ""
        raise RuntimeError(f"Search preparation timed out: {last_error}.{extra} Rerun startup after resolving this; data is retained.")
    finally:
        cluster.close()


def http_json(url: str):
    with urlopen(url, timeout=20) as response:
        return json.load(response)


def wait_for(url: str, predicate, description: str):
    deadline = time.monotonic() + int(os.environ.get("CAPELLA_WAIT_SECONDS", "300"))
    last = "not reachable yet"
    while time.monotonic() < deadline:
        try:
            payload = http_json(url)
            if predicate(payload):
                print(f"Ready: {description}", flush=True)
                return payload
            last = payload.get("error") or payload.get("startup_error") or payload.get("status") or "still starting"
        except (URLError, HTTPError, TimeoutError, ValueError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        print(f"Waiting for {description}: {last}", flush=True)
        time.sleep(5)
    raise RuntimeError(f"{description} did not become ready: {last}. Run bash CAPELLA-LOGS.sh")


def verify() -> None:
    wait_for("http://ui:8088/api/readiness", lambda p: bool(p.get("ready")), "StreamAI UI")
    required = ("data_kv", "query_index", "search_fts_vector", "agent_memory", "agent_catalog", "mcp_server", "agent_tracer", "model_runtime", "usage_meter")
    status = wait_for("http://ui:8088/api/status", lambda p: bool(p.get("ready")) and all(p.get("services", {}).get(key, {}).get("healthy") for key in required), "all required application services")
    for key in required:
        print(f"PASS: {key}")
    print("Checking plan-cache statistics and Search index...", flush=True)
    try:
        cache = http_json("http://ui:8088/api/planner/cache").get("data") or {}
    except HTTPError as exc:
        raise RuntimeError(
            f"Plan-cache status check (/api/planner/cache) returned HTTP {exc.code}. "
            "Run bash CAPELLA-LOGS.sh ui for the server error. "
            "Check that the plan-cache SQL++ indexes are online as well as its Search index."
        ) from exc
    if not cache.get("searchHealthy"):
        raise RuntimeError("The plan cache Search index failed its runtime check")
    print("PASS: plan cache Search index")
    for key in ("ai_functions", "data_processing"):
        item = status.get("services", {}).get(key, {})
        print(f"{key}: enabled={item.get('enabled')} healthy={item.get('healthy')}")
        if item.get("enabled") and not item.get("healthy"):
            raise RuntimeError(f"Enabled service {key} is unhealthy; check the Capella setup")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "wait-memory", "verify"])
    action = parser.parse_args().action
    if action == "prepare":
        prepare()
    elif action == "wait-memory":
        wait_for("http://memory:8080/health", lambda p: p.get("status") == "healthy", "Agent Memory")
    else:
        verify()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        for key, value in os.environ.items():
            if value and ("PASSWORD" in key or "API_KEY" in key or key.endswith("TOKEN")):
                message = message.replace(value, "[redacted]")
        raise SystemExit("Capella setup stopped: " + message)
