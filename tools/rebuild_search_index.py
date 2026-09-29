#!/usr/bin/env python3
from __future__ import annotations

import time
from datetime import timedelta

from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Cluster
from couchbase.exceptions import DocumentNotFoundException
from couchbase.options import ClusterOptions, QueryOptions

from load_catalogue import Config, create_search_index

SCHEMA_VERSION = 3


def main() -> None:
    config = Config()
    auth = PasswordAuthenticator(config.cb_username, config.cb_password)
    cluster = Cluster.connect(config.cb_conn_string, ClusterOptions(auth))
    cluster.wait_until_ready(timedelta(seconds=30))
    bucket = cluster.bucket(config.bucket)
    jobs = bucket.scope("operations").collection("ingestion_jobs")
    try:
        marker = jobs.get("search_index::schema").content_as[dict]
        if int(marker.get("version") or 0) >= SCHEMA_VERSION:
            print(f"Search index schema already at v{SCHEMA_VERSION}.")
            return
    except DocumentNotFoundException:
        pass

    keyspace = f"`{config.bucket}`.`catalogue`.`titles`"
    result = cluster.query(
        f"SELECT RAW ARRAY_LENGTH(t.embedding) FROM {keyspace} AS t "
        "WHERE t.embedding IS NOT MISSING LIMIT 1",
        QueryOptions(readonly=True),
    )
    dimension = int(next(iter(result.rows()), 0))
    if not dimension:
        raise RuntimeError("No catalogue embedding found; load the catalogue first.")
    create_search_index(config, dimension)
    jobs.upsert(
        "search_index::schema",
        {"type": "search_index_schema", "version": SCHEMA_VERSION, "updatedAt": time.time()},
    )
    print(f"Search index schema upgraded to v{SCHEMA_VERSION}.")


if __name__ == "__main__":
    main()
