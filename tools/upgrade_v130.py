#!/usr/bin/env python3
from __future__ import annotations

import os
import time
from datetime import timedelta
from typing import Any

from couchbase.auth import PasswordAuthenticator
from couchbase.cluster import Cluster
from couchbase.options import ClusterOptions, QueryOptions
from couchbase.n1ql import QueryScanConsistency
from couchbase.exceptions import DocumentNotFoundException

from load_catalogue import demo_availability, Config


def main() -> None:
    config = Config()
    conn, username, password = config.cb_conn_string, config.cb_username, config.cb_password
    bucket_name = os.getenv("CONTENT_BUCKET", "streaming")
    region = os.getenv("DEFAULT_REGION_CODE", "GB").upper()
    tier = os.getenv("DEFAULT_SUBSCRIPTION_TIER", "standard").lower()
    rating = os.getenv("DEFAULT_PARENTAL_RATING", "18")

    options = ClusterOptions(PasswordAuthenticator(username, password))
    if config.wan_profile:
        options.apply_profile("wan_development")
    cluster = Cluster.connect(conn, options)
    cluster.wait_until_ready(timedelta(seconds=30))
    bucket = cluster.bucket(bucket_name)
    titles = bucket.scope("catalogue").collection("titles")
    entitlements = bucket.scope("operations").collection("entitlements")

    title_keyspace = f"`{bucket_name}`.`catalogue`.`titles`"
    profile_keyspace = f"`{bucket_name}`.`viewers`.`profiles`"

    rows = list(
        cluster.query(
            f"SELECT META(t).id AS docId, t.id, t.tmdbId, t.availability "
            f"FROM {title_keyspace} AS t",
            QueryOptions(readonly=True, scan_consistency=QueryScanConsistency.REQUEST_PLUS),
        ).rows()
    )
    updated = 0
    for row in rows:
        if row.get("availability"):
            continue
        doc_id = str(row.get("docId") or row.get("id"))
        document = titles.get(doc_id).content_as[dict]
        document["availability"] = demo_availability(
            row.get("tmdbId") or row.get("id") or doc_id,
            region,
        )
        document["governedAgentSchemaVersion"] = 1
        titles.upsert(doc_id, document)
        updated += 1

    profile_rows = list(
        cluster.query(
            f"SELECT RAW p.viewerId FROM {profile_keyspace} AS p WHERE p.viewerId IS NOT MISSING",
            QueryOptions(readonly=True, scan_consistency=QueryScanConsistency.REQUEST_PLUS),
        ).rows()
    )
    seeded = 0
    now = time.time()
    for viewer_id in sorted({str(value) for value in profile_rows if value}):
        key = f"entitlement::{viewer_id}"
        try:
            entitlements.get(key)
            continue
        except DocumentNotFoundException:
            pass
        entitlement: dict[str, Any] = {
            "type": "streamai_viewer_entitlement",
            "viewerId": viewer_id,
            "householdId": f"household::{viewer_id}",
            "regionCode": region,
            "subscriptionTier": tier,
            "maxParentalRating": rating,
            "deviceType": "web-demo",
            "roamingAllowed": True,
            "createdAt": now,
            "updatedAt": now,
            "seededBy": "v1.3.0_upgrade",
        }
        entitlements.upsert(key, entitlement)
        seeded += 1

    print(
        f"v1.3.0 governed-agent upgrade complete: "
        f"{updated} catalogue titles enriched, {seeded} viewer entitlements seeded."
    )
    close = getattr(cluster, "close", None)
    if callable(close):
        close()


if __name__ == "__main__":
    main()
