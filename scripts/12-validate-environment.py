#!/usr/bin/env python3
"""Validate a StreamAI deployment profile before application startup."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = Path(os.getenv("ENV_FILE", ROOT / ".env"))


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env_file(ENV_FILE)
sys.path.insert(0, str(ROOT / "ui"))

from app.config import settings  # noqa: E402
from app.connection import create_cluster  # noqa: E402

errors: list[str] = []
warnings: list[str] = []


def is_placeholder(value: str) -> bool:
    upper = str(value or "").upper()
    return any(marker in upper for marker in ("YOUR_", "YOUR-", "CHANGE_ME", "<", ">"))


if settings.deployment_target not in {"local", "server", "capella"}:
    errors.append("DEPLOYMENT_TARGET must be local, server or capella")
if settings.deployment_target == "capella" and not settings.cb_conn_string.startswith("couchbases://"):
    errors.append("Capella requires CB_CONN_STRING=couchbases://...")
if settings.deployment_target != "local":
    for name in ("CB_CONN_STRING", "CB_USERNAME", "CB_PASSWORD"):
        if is_placeholder(os.getenv(name, "")):
            errors.append(f"Replace placeholder value for {name}")
if settings.search_transport not in {"auto", "rest", "sdk"}:
    errors.append("SEARCH_TRANSPORT must be auto, rest or sdk")
if settings.search_transport == "rest" and settings.deployment_target == "capella":
    errors.append("Capella requires SEARCH_TRANSPORT=sdk or auto")
if settings.chat_provider != "disabled" and not settings.chat_model:
    errors.append("CHAT_MODEL is required unless CHAT_PROVIDER=disabled")
if settings.embedding_provider != "disabled" and not settings.embedding_model:
    errors.append("EMBEDDING_MODEL is required unless EMBEDDING_PROVIDER=disabled")
if settings.embedding_dimensions < 1:
    errors.append("EMBEDDING_DIMENSIONS must be positive")
if settings.model_service_cache_mode not in {"standard", "semantic", "none"}:
    errors.append("MODEL_SERVICE_CACHE_MODE must be standard, semantic or none")

if settings.mcp_enabled:
    if not settings.mcp_url.startswith(("http://", "https://")):
        errors.append("MCP_URL must be an HTTP Streamable HTTP endpoint")
    if not settings.mcp_url.rstrip("/").endswith("/mcp"):
        errors.append("MCP_URL must end in /mcp")
    if settings.mcp_mode not in {"native_streamable_http", "streamable_http"}:
        errors.append("MCP_MODE must select Streamable HTTP for this release")
    if not settings.mcp_read_only:
        warnings.append("MCP_READ_ONLY=false: consumer-facing database tools can mutate data")
    mcp_user = os.getenv("MCP_CB_USERNAME", "").strip()
    app_user = os.getenv("CB_USERNAME", "").strip()
    if mcp_user and app_user and mcp_user == app_user:
        warnings.append(
            "MCP_CB_USERNAME matches CB_USERNAME; use a dedicated read-only MCP identity "
            "with the required bucket-scoped read, query, Search and metadata privileges"
        )
if settings.mcp_required and not settings.mcp_enabled:
    errors.append("MCP_REQUIRED=true requires MCP_ENABLED=true")

if settings.agent_catalog_enabled:
    if settings.agent_catalog_mode != "native":
        errors.append("AGENT_CATALOG_MODE must be native")
    if settings.agent_catalog_execution_mode not in {"mcp", "catalog_function"}:
        errors.append("AGENT_CATALOG_EXECUTION_MODE must be mcp or catalog_function")
    if not settings.agent_catalog_prompt_name:
        errors.append("AGENT_CATALOG_PROMPT_NAME is required")
    prompt = ROOT / "ui" / "agent_catalog" / "prompts" / "streaming_assistant_system.prompt"
    tools = ROOT / "ui" / "agent_catalog" / "tools" / "streamai_mcp_tools.py"
    if not prompt.exists():
        errors.append("Agent Catalog prompt source is missing")
    if not tools.exists():
        errors.append("Agent Catalog tool source is missing")
    catalog_conn = os.getenv("AGENT_CATALOG_CONN_STRING", settings.cb_conn_string)
    if settings.deployment_target == "capella" and not catalog_conn.startswith("couchbases://"):
        errors.append("Capella Agent Catalog requires AGENT_CATALOG_CONN_STRING=couchbases://...")
if settings.agent_catalog_required and not settings.agent_catalog_enabled:
    errors.append("AGENT_CATALOG_REQUIRED=true requires AGENT_CATALOG_ENABLED=true")
if settings.agent_catalog_execution_mode == "mcp" and not settings.mcp_enabled:
    errors.append("AGENT_CATALOG_EXECUTION_MODE=mcp requires MCP_ENABLED=true")

if settings.ai_functions_enabled and settings.deployment_target != "capella":
    warnings.append("AI Functions are normally demonstrated against Capella AI Services")
if os.getenv("START_AGENT_MEMORY", "true").lower() in {"1", "true", "yes", "on"}:
    for name in (
        "AGENTMEMORY_CONN_STRING",
        "AGENTMEMORY_USERNAME",
        "AGENTMEMORY_PASSWORD",
        "AGENTMEMORY_BUCKET",
        "AGENTMEMORY_EMBEDDING_MODEL",
        "AGENTMEMORY_EMBEDDING_URL",
        "AGENTMEMORY_LLM_MODEL",
        "AGENTMEMORY_LLM_URL",
    ):
        if not os.getenv(name, "").strip():
            errors.append(f"{name} is required when START_AGENT_MEMORY=true")
    memory_conn = os.getenv("AGENTMEMORY_CONN_STRING", "")
    if settings.deployment_target == "capella" and memory_conn and not memory_conn.startswith("couchbases://"):
        errors.append("Capella Agent Memory requires AGENTMEMORY_CONN_STRING=couchbases://...")

if settings.data_processing_mode not in {"python_loader", "capella_workflow"}:
    errors.append("DATA_PROCESSING_MODE must be python_loader or capella_workflow")
if settings.data_processing_mode == "capella_workflow":
    if settings.deployment_target != "capella":
        errors.append("DATA_PROCESSING_MODE=capella_workflow requires DEPLOYMENT_TARGET=capella")
    if not settings.data_processing_workflow_id or is_placeholder(settings.data_processing_workflow_id):
        errors.append("Replace DATA_PROCESSING_WORKFLOW_ID for capella_workflow mode")

if settings.deployment_target == "capella":
    required_names = ["CB_CONN_STRING", "CB_USERNAME", "CB_PASSWORD"]
    if settings.chat_provider != "disabled":
        required_names += ["CHAT_BASE_URL", "CHAT_MODEL"]
    if settings.embedding_provider != "disabled":
        required_names += ["EMBEDDING_BASE_URL", "EMBEDDING_MODEL"]
    if settings.chat_provider == "capella_model_service":
        required_names.append("CHAT_API_KEY")
    if settings.embedding_provider == "capella_model_service":
        required_names.append("EMBEDDING_API_KEY")
    if settings.mcp_enabled:
        required_names += ["MCP_CB_CONNECTION_STRING", "MCP_CB_USERNAME", "MCP_CB_PASSWORD"]
    if settings.agent_catalog_enabled:
        required_names += ["AGENT_CATALOG_CONN_STRING", "AGENT_CATALOG_USERNAME", "AGENT_CATALOG_PASSWORD"]
    if os.getenv("START_AGENT_MEMORY", "true").lower() in {"1", "true", "yes", "on"}:
        required_names += [
            "AGENTMEMORY_CONN_STRING", "AGENTMEMORY_USERNAME", "AGENTMEMORY_PASSWORD",
            "AGENTMEMORY_LLM_URL", "AGENTMEMORY_LLM_MODEL",
            "AGENTMEMORY_EMBEDDING_URL", "AGENTMEMORY_EMBEDDING_MODEL",
        ]
    for name in required_names:
        value = os.getenv(name, "")
        if not value or is_placeholder(value):
            errors.append(f"Replace required Capella placeholder for {name}")
    for name in ("CB_CONN_STRING", "CB_LOADER_CONN_STRING", "MCP_CB_CONNECTION_STRING",
                 "AGENT_CATALOG_CONN_STRING", "AGENTMEMORY_CONN_STRING"):
        value = os.getenv(name, "")
        if value and not value.startswith("couchbases://"):
            errors.append(f"Capella requires TLS for {name}")

required_files = [
    ROOT / "mcp-server" / "Dockerfile",
    ROOT / "agent-catalog-publisher" / "Dockerfile",
    ROOT / "agent-catalog-publisher" / "publish.sh",
    ROOT / "scripts" / "02b-start-mcp-server.sh",
    ROOT / "scripts" / "04d-publish-agent-catalog.sh",
]
for path in required_files:
    if not path.exists():
        errors.append(f"Required native integration asset is missing: {path.relative_to(ROOT)}")

try:
    cluster = create_cluster(settings)
    bucket = cluster.bucket(settings.content_bucket)
    bucket.default_collection()
    print(f"OK Couchbase: {settings.deployment_target} {settings.cb_conn_string}")
except Exception as exc:
    errors.append(f"Couchbase connection failed: {type(exc).__name__}: {exc}")

if warnings:
    print("Configuration warnings:")
    for warning in warnings:
        print(f" - {warning}")

if errors:
    print("Configuration validation failed:", file=sys.stderr)
    for error in errors:
        print(f" - {error}", file=sys.stderr)
    raise SystemExit(1)

print(f"OK chat provider: {settings.chat_provider}/{settings.chat_model}")
print(
    f"OK embedding provider: {settings.embedding_provider}/{settings.embedding_model} "
    f"({settings.embedding_dimensions} dimensions)"
)
print(
    f"OK MCP: {settings.mcp_mode} {settings.mcp_url} "
    f"required={settings.mcp_required} read_only={settings.mcp_read_only}"
)
print(
    f"OK Agent Catalog: prompt={settings.agent_catalog_prompt_name} "
    f"bucket={settings.agent_catalog_bucket} execution={settings.agent_catalog_execution_mode} "
    f"required={settings.agent_catalog_required}"
)
print(
    f"OK Capella AI extensions: AI Functions={settings.ai_functions_enabled} "
    f"Data Processing={settings.data_processing_mode}"
)
