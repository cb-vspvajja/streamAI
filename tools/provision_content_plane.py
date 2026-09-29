#!/usr/bin/env python3
"""Provision scopes, collections and SQL++ indexes in an existing bucket.

Works with self-managed Server and Capella. Bucket creation and database-user
administration stay in the platform control plane and are deliberately not done
by the application.
"""
import os, sys
from pathlib import Path
from datetime import timedelta
ROOT=Path(__file__).resolve().parents[1]
ENV_FILE=Path(os.getenv('ENV_FILE', str(ROOT/'.env')))
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if line and not line.lstrip().startswith('#') and '=' in line:
            k,v=line.split('=',1); os.environ.setdefault(k,v)
sys.path.insert(0,str(ROOT/'ui'))
# Prefer setup credentials; callers export the resolved environment before invocation.
for suffix in ('USERNAME', 'PASSWORD'):
    if os.getenv('CB_PROVISION_' + suffix):
        os.environ['CB_' + suffix] = os.environ['CB_PROVISION_' + suffix]
from app.config import settings
from app.connection import create_cluster
from couchbase.management.collections import CollectionSpec
from couchbase.options import QueryOptions
from couchbase.exceptions import ScopeAlreadyExistsException, CollectionAlreadyExistsException

SCHEMA={
 'catalogue':['titles','people','genres'],
 'viewers':['profiles','watch_history','interactions','app_state'],
 'recommendations':['generated','traces','plan_cache'],
 'operations':['ingestion_jobs','entitlements','agent_catalog','action_receipts'],
 'telemetry':['ai_metrics','showcase_state','experiments','evaluations','profile_snapshots'],
}
cluster=create_cluster(settings)
B=f'`{settings.content_bucket}`'
if settings.deployment_target == 'capella':
    # Use SQL++ DDL instead of self-managed cluster-management endpoints.
    for scope, collections in SCHEMA.items():
        cluster.query(f'CREATE SCOPE {B}.`{scope}` IF NOT EXISTS', QueryOptions(timeout=timedelta(seconds=120))).execute()
        for collection in collections:
            cluster.query(f'CREATE COLLECTION {B}.`{scope}`.`{collection}` IF NOT EXISTS', QueryOptions(timeout=timedelta(seconds=120))).execute()
else:
    cm=cluster.bucket(settings.content_bucket).collections()
    for scope, collections in SCHEMA.items():
        try: cm.create_scope(scope)
        except ScopeAlreadyExistsException: pass
        for collection in collections:
            try: cm.create_collection(CollectionSpec(collection, scope))
            except CollectionAlreadyExistsException: pass
statements=[
 f'CREATE INDEX IF NOT EXISTS ix_usage_ledger_time ON {B}.`telemetry`.`ai_metrics`(`type`, timestamp, ledgerId, id)',
 f'CREATE INDEX IF NOT EXISTS ix_plan_cache_version_expiry ON {B}.`recommendations`.`plan_cache`(plannerVersion, toolSchemaVersion, expiresAt, semanticEligible)',
 f'CREATE INDEX IF NOT EXISTS ix_plan_cache_stats ON {B}.`recommendations`.`plan_cache`(`type`, semanticEligible, hitCount)',
 f'CREATE INDEX IF NOT EXISTS ix_agent_catalog_type_name ON {B}.`operations`.`agent_catalog`(type, name, version)',
 f'CREATE INDEX IF NOT EXISTS ix_ai_metrics_viewer_time ON {B}.`telemetry`.`ai_metrics`(viewerId, updatedAt DESC)',
 f'CREATE PRIMARY INDEX IF NOT EXISTS ON {B}.`catalogue`.`titles`',
 f'CREATE INDEX IF NOT EXISTS ix_titles_popularity ON {B}.`catalogue`.`titles`(popularity DESC, voteAverage DESC)',
 f'CREATE INDEX IF NOT EXISTS ix_titles_type_year ON {B}.`catalogue`.`titles`(contentType, releaseYear DESC)',
 f'CREATE INDEX IF NOT EXISTS ix_titles_type_genre ON {B}.`catalogue`.`titles`(contentType, DISTINCT ARRAY LOWER(g) FOR g IN genres END)',
 f'CREATE INDEX IF NOT EXISTS ix_titles_analytics ON {B}.`catalogue`.`titles`(contentType, releaseYear, originalLanguage, voteAverage, runtimeMinutes, episodeRuntimeMinutes)',
 f'CREATE PRIMARY INDEX IF NOT EXISTS ON {B}.`viewers`.`profiles`',
 f'CREATE INDEX IF NOT EXISTS ix_watch_viewer_time ON {B}.`viewers`.`watch_history`(viewerId, lastWatchedAt DESC, progressPct, titleId)',
 f'CREATE INDEX IF NOT EXISTS ix_interactions_viewer_time ON {B}.`viewers`.`interactions`(viewerId, timestamp DESC, action, titleId)',
 f'CREATE INDEX IF NOT EXISTS ix_traces_viewer_time ON {B}.`recommendations`.`traces`(viewerId, timestamp DESC, type)',
 f'CREATE INDEX IF NOT EXISTS ix_traces_viewer_session ON {B}.`recommendations`.`traces`(viewerId, sessionId, startedAt ASC, type)',
 f'CREATE INDEX IF NOT EXISTS ix_entitlements_viewer ON {B}.`operations`.`entitlements`(viewerId, regionCode, subscriptionTier)',
 f'CREATE INDEX IF NOT EXISTS ix_action_receipts_viewer_time ON {B}.`operations`.`action_receipts`(viewerId, timestamp DESC, action, titleId)',
 f'CREATE PRIMARY INDEX IF NOT EXISTS ON {B}.`telemetry`.`ai_metrics`',
 f'CREATE PRIMARY INDEX IF NOT EXISTS ON {B}.`telemetry`.`showcase_state`',
 f'CREATE INDEX IF NOT EXISTS ix_experiments_viewer_time ON {B}.`telemetry`.`experiments`(viewerId, timestamp DESC, type)',
 f'CREATE INDEX IF NOT EXISTS ix_evaluations_viewer_time ON {B}.`telemetry`.`evaluations`(viewerId, timestamp DESC, type)',
 f'CREATE INDEX IF NOT EXISTS ix_profile_snapshots_viewer_time ON {B}.`telemetry`.`profile_snapshots`(viewerId, timestamp DESC, recommendationVersion)',
]
for statement in statements:
    cluster.query(statement, QueryOptions(timeout=timedelta(seconds=120))).execute()
print('Content-plane scopes, collections and SQL++ indexes are ready.')
