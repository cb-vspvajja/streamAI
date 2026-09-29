from __future__ import annotations

import os
from dataclasses import dataclass


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_ui_experience_config(
    configured_mode: str,
    *,
    switch_enabled: bool,
    showcase_enabled: bool,
) -> dict[str, object]:
    """Return a bounded public UI mode configuration.

    The browser switch controls presentation only. If Showcase Studio is
    disabled for a customer deployment, the public configuration cannot expose
    or select the showcase experience even if an invalid environment value or
    stale browser preference requests it.
    """
    normalised = str(configured_mode or "showcase").strip().lower()
    if normalised not in {"showcase", "customer"}:
        normalised = "showcase"
    if not showcase_enabled:
        normalised = "customer"
    return {
        "defaultExperience": normalised,
        "switchEnabled": bool(switch_enabled and showcase_enabled),
        "showcaseEnabled": bool(showcase_enabled),
        "availableExperiences": (
            ["showcase", "customer"] if showcase_enabled else ["customer"]
        ),
        "customerHiddenSurfaces": [
            "ai_value",
            "agent_inspector",
            "showcase_studio",
            "data_plane_status",
            "memory_inspector",
            "trace_replay",
            "search_diagnostics",
        ],
    }


@dataclass(frozen=True)
class Settings:
    app_title: str = os.getenv("APP_TITLE", "Couchbase StreamAI")
    app_version: str = os.getenv("APP_VERSION", "1.1.0")

    # Couchbase Agent Memory
    agent_memory_url: str = os.getenv(
        "AGENT_MEMORY_URL", "http://agentmemory-server:8080"
    ).rstrip("/")
    short_term_ttl_seconds: int = int(os.getenv("SHORT_TERM_TTL_SECONDS", "3600"))
    semantic_result_limit: int = int(os.getenv("MEMORY_SEMANTIC_RESULT_LIMIT", "8"))
    memory_summary_policy: str = os.getenv("MEMORY_SUMMARY_POLICY", "selective").lower()
    memory_async_processing: bool = _as_bool(os.getenv("MEMORY_ASYNC_PROCESSING"), True)
    auto_capture_facts: bool = _as_bool(os.getenv("AUTO_CAPTURE_FACTS"), True)
    semantic_retrieval_in_chat: bool = _as_bool(os.getenv("SEMANTIC_RETRIEVAL_IN_CHAT"), False)

    deployment_target: str = os.getenv("DEPLOYMENT_TARGET", "local").lower()
    cb_wan_profile: bool = _as_bool(os.getenv("CB_WAN_PROFILE"), os.getenv("DEPLOYMENT_TARGET", "local").lower() == "capella")
    cb_connect_timeout_seconds: float = float(os.getenv("CB_CONNECT_TIMEOUT_SECONDS", "30"))
    search_transport: str = os.getenv("SEARCH_TRANSPORT", "auto").lower()

    # Pluggable chat and embedding providers. Ollama variables remain aliases.
    # avoid the model; Ollama is used for genuinely open conversational turns.
    chat_provider: str = os.getenv("CHAT_PROVIDER", "ollama").lower()
    chat_base_url: str = os.getenv("CHAT_BASE_URL", os.getenv("OLLAMA_CHAT_URL", "http://host.docker.internal:11435")).rstrip("/")
    chat_model: str = os.getenv("CHAT_MODEL", os.getenv("OLLAMA_LLM_MODEL", "llama3.2:1b"))
    chat_api_key: str = os.getenv("CHAT_API_KEY", "")
    chat_headers_json: str = os.getenv("CHAT_HEADERS_JSON", "")
    chat_temperature: float = float(os.getenv("CHAT_TEMPERATURE", "0.1"))
    model_service_cache_mode: str = os.getenv("MODEL_SERVICE_CACHE_MODE", "semantic").lower()
    model_service_routing_strategy: str = os.getenv("MODEL_SERVICE_ROUTING_STRATEGY", "least-latency")
    ollama_chat_url: str = chat_base_url
    embedding_provider: str = os.getenv("EMBEDDING_PROVIDER", "openai_compatible").lower()
    embedding_base_url: str = os.getenv("EMBEDDING_BASE_URL", os.getenv("OLLAMA_EMBED_URL", "http://ollama:11434")).rstrip("/")
    embedding_api_key: str = os.getenv("EMBEDDING_API_KEY", "")
    embedding_headers_json: str = os.getenv("EMBEDDING_HEADERS_JSON", "")
    embedding_send_input_type: bool = _as_bool(os.getenv("EMBEDDING_SEND_INPUT_TYPE"), os.getenv("EMBEDDING_PROVIDER", "").lower() == "capella_model_service")
    embedding_dimensions: int = int(os.getenv("EMBEDDING_DIMENSIONS", "768"))
    ollama_embed_url: str = embedding_base_url
    llm_model: str = chat_model
    embedding_model: str = os.getenv("EMBEDDING_MODEL", os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"))
    model_timeout_seconds: float = float(os.getenv("MODEL_TIMEOUT_SECONDS", "120"))
    model_num_predict: int = int(os.getenv("MODEL_NUM_PREDICT", "140"))
    embedding_timeout_seconds: float = float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "3.0"))
    embedding_query_cache_seconds: int = int(os.getenv("EMBEDDING_QUERY_CACHE_SECONDS", "86400"))
    search_query_timeout_seconds: float = float(os.getenv("SEARCH_QUERY_TIMEOUT_SECONDS", "2.0"))
    search_interactive_timeout_seconds: float = float(
        os.getenv("SEARCH_INTERACTIVE_TIMEOUT_SECONDS", "1.5")
    )
    search_circuit_breaker_seconds: float = float(
        os.getenv("SEARCH_CIRCUIT_BREAKER_SECONDS", "20")
    )
    chat_recommendation_mode: str = os.getenv("CHAT_RECOMMENDATION_MODE", "fts").lower()

    # Couchbase operational content plane.
    cb_conn_string: str = os.getenv(
        "CB_CONN_STRING", "couchbase://host.docker.internal"
    )
    cb_username: str = os.getenv("CB_USERNAME", "agentMemory")
    cb_password: str = os.getenv("CB_PASSWORD", "password")
    content_bucket: str = os.getenv("CONTENT_BUCKET", "streaming")
    catalogue_scope: str = os.getenv("CATALOGUE_SCOPE", "catalogue")
    titles_collection: str = os.getenv("TITLES_COLLECTION", "titles")
    viewers_scope: str = os.getenv("VIEWERS_SCOPE", "viewers")
    profiles_collection: str = os.getenv("PROFILES_COLLECTION", "profiles")
    watch_history_collection: str = os.getenv("WATCH_HISTORY_COLLECTION", "watch_history")
    interactions_collection: str = os.getenv("INTERACTIONS_COLLECTION", "interactions")
    app_state_collection: str = os.getenv("APP_STATE_COLLECTION", "app_state")
    recommendations_scope: str = os.getenv("RECOMMENDATIONS_SCOPE", "recommendations")
    generated_collection: str = os.getenv("GENERATED_COLLECTION", "generated")
    traces_collection: str = os.getenv("TRACES_COLLECTION", "traces")
    plan_cache_collection: str = os.getenv("PLAN_CACHE_COLLECTION", "plan_cache")
    telemetry_scope: str = os.getenv("TELEMETRY_SCOPE", "telemetry")
    ai_metrics_collection: str = os.getenv("AI_METRICS_COLLECTION", "ai_metrics")
    showcase_state_collection: str = os.getenv("SHOWCASE_STATE_COLLECTION", "showcase_state")
    experiments_collection: str = os.getenv("EXPERIMENTS_COLLECTION", "experiments")
    evaluations_collection: str = os.getenv("EVALUATIONS_COLLECTION", "evaluations")
    profile_snapshots_collection: str = os.getenv("PROFILE_SNAPSHOTS_COLLECTION", "profile_snapshots")
    operations_scope: str = os.getenv("OPERATIONS_SCOPE", "operations")
    entitlements_collection: str = os.getenv("ENTITLEMENTS_COLLECTION", "entitlements")
    agent_catalog_collection: str = os.getenv("AGENT_CATALOG_COLLECTION", "agent_catalog")
    action_receipts_collection: str = os.getenv("ACTION_RECEIPTS_COLLECTION", "action_receipts")

    # Governed streaming-agent defaults. These are seeded per viewer and remain
    # editable in Couchbase for the entitlement and policy demo.
    default_region_code: str = os.getenv("DEFAULT_REGION_CODE", "GB").upper()
    default_subscription_tier: str = os.getenv("DEFAULT_SUBSCRIPTION_TIER", "standard").lower()
    default_parental_rating: str = os.getenv("DEFAULT_PARENTAL_RATING", "18")
    agent_trace_limit: int = int(os.getenv("AGENT_TRACE_LIMIT", "40"))
    enable_governed_actions: bool = _as_bool(os.getenv("ENABLE_GOVERNED_ACTIONS"), True)
    enable_entitlement_filtering: bool = _as_bool(os.getenv("ENABLE_ENTITLEMENT_FILTERING"), True)
    # Native Couchbase Agent Catalog, Agent Tracer and MCP Server.
    # Required mode is the default for v1.5 because the presenter story should
    # prove that tools and prompts came from Agent Catalog and that database
    # reads crossed the MCP transport. Set *_REQUIRED=false only for a degraded
    # fallback mode during troubleshooting.
    agent_catalog_enabled: bool = _as_bool(os.getenv("AGENT_CATALOG_ENABLED"), True)
    agent_catalog_required: bool = _as_bool(os.getenv("AGENT_CATALOG_REQUIRED"), True)
    agent_catalog_mode: str = os.getenv("AGENT_CATALOG_MODE", "native").lower()
    agent_catalog_prompt_name: str = os.getenv(
        "AGENT_CATALOG_PROMPT_NAME", "streaming_assistant_system"
    )
    agent_catalog_bucket: str = os.getenv("AGENT_CATALOG_BUCKET", os.getenv("CONTENT_BUCKET", "streaming"))
    agent_catalog_snapshot: str = os.getenv("AGENT_CATALOG_SNAPSHOT", "")
    agent_catalog_activity_dir: str = os.getenv(
        "AGENT_CATALOG_ACTIVITY", os.getenv("AGENT_CATALOG_ACTIVITY_PATH", ".agent-activity")
    )
    agent_catalog_catalog_dir: str = os.getenv(
        "AGENT_CATALOG_CATALOG", os.getenv("AGENT_CATALOG_CATALOG_PATH", ".agent-catalog")
    )
    agent_catalog_execution_mode: str = os.getenv("AGENT_CATALOG_EXECUTION_MODE", "mcp").lower()
    native_agent_tracing_enabled: bool = _as_bool(os.getenv("NATIVE_AGENT_TRACING_ENABLED"), True)

    mcp_enabled: bool = _as_bool(os.getenv("MCP_ENABLED"), True)
    mcp_required: bool = _as_bool(os.getenv("MCP_REQUIRED"), True)
    mcp_mode: str = os.getenv("MCP_MODE", "native_streamable_http").lower()
    mcp_url: str = os.getenv("MCP_URL", "http://couchbase-mcp-server:8000/mcp")
    mcp_timeout_seconds: float = float(os.getenv("MCP_TIMEOUT_SECONDS", "8"))
    mcp_tool_cache_seconds: int = int(os.getenv("MCP_TOOL_CACHE_SECONDS", "60"))
    mcp_read_only: bool = _as_bool(os.getenv("MCP_READ_ONLY"), True)
    mcp_bearer_token: str = os.getenv("MCP_BEARER_TOKEN", "")

    # Capella AI Services integrations. They are explicit because AI Functions
    # and managed Data Processing workflows must already be enabled in Capella.
    ai_functions_enabled: bool = _as_bool(os.getenv("AI_FUNCTIONS_ENABLED"), False)
    ai_functions_temperature: float = float(os.getenv("AI_FUNCTIONS_TEMPERATURE", "0.1"))
    ai_functions_max_words: int = int(os.getenv("AI_FUNCTIONS_MAX_WORDS", "120"))
    ai_functions_max_tokens: int = int(os.getenv("AI_FUNCTIONS_MAX_TOKENS", "220"))
    data_processing_mode: str = os.getenv("DATA_PROCESSING_MODE", "python_loader").lower()
    data_processing_workflow_id: str = os.getenv("DATA_PROCESSING_WORKFLOW_ID", "")

    # Search Service REST endpoint and index.
    search_url: str = os.getenv(
        "CB_SEARCH_URL", "http://host.docker.internal:8094"
    ).rstrip("/")
    catalogue_search_index: str = os.getenv(
        "CATALOGUE_SEARCH_INDEX", "streaming-catalogue-search"
    )
    plan_cache_search_index: str = os.getenv(
        "PLAN_CACHE_SEARCH_INDEX", "streaming-plan-cache"
    )
    search_result_limit: int = int(os.getenv("SEARCH_RESULT_LIMIT", "20"))

    # Governed assistant planner and reusable plan cache. The cached payload is
    # an abstract, validated plan; viewer state and catalogue results are always
    # rebound and read fresh for every turn.
    assistant_planner_enabled: bool = _as_bool(os.getenv("ASSISTANT_PLANNER_ENABLED"), True)
    assistant_planner_model_enabled: bool = _as_bool(
        os.getenv("ASSISTANT_PLANNER_MODEL_ENABLED"), True
    )
    assistant_planner_version: str = os.getenv("ASSISTANT_PLANNER_VERSION", "3.1")
    assistant_tool_schema_version: str = os.getenv(
        "ASSISTANT_TOOL_SCHEMA_VERSION", "2.1"
    )
    assistant_planner_timeout_seconds: float = float(
        os.getenv("ASSISTANT_PLANNER_TIMEOUT_SECONDS", "2.0")
    )
    plan_cache_enabled: bool = _as_bool(os.getenv("PLAN_CACHE_ENABLED"), True)
    plan_cache_ttl_seconds: int = int(
        os.getenv("PLAN_CACHE_TTL_SECONDS", str(30 * 24 * 60 * 60))
    )
    plan_cache_semantic_enabled: bool = _as_bool(
        os.getenv("PLAN_CACHE_SEMANTIC_ENABLED"), True
    )
    plan_cache_semantic_threshold: float = float(
        os.getenv("PLAN_CACHE_SEMANTIC_THRESHOLD", "0.92")
    )
    plan_cache_semantic_probe_timeout_seconds: float = float(
        os.getenv("PLAN_CACHE_SEMANTIC_PROBE_TIMEOUT_SECONDS", "0.75")
    )
    plan_cache_candidate_limit: int = int(
        os.getenv("PLAN_CACHE_CANDIDATE_LIMIT", "5")
    )

    # Performance and cache behaviour.
    service_startup_timeout_seconds: float = float(os.getenv("SERVICE_STARTUP_TIMEOUT_SECONDS", "60"))
    home_cache_seconds: int = int(os.getenv("HOME_CACHE_SECONDS", "300"))
    row_cache_seconds: int = int(os.getenv("ROW_CACHE_SECONDS", "180"))
    trending_cache_seconds: int = int(os.getenv("TRENDING_CACHE_SECONDS", "600"))
    readiness_poll_seconds: float = float(os.getenv("READINESS_POLL_SECONDS", "1.0"))

    # Artwork. Smaller defaults materially reduce initial page bandwidth.
    tmdb_image_base: str = os.getenv(
        "TMDB_IMAGE_BASE", "https://image.tmdb.org/t/p/w342"
    ).rstrip("/")
    tmdb_backdrop_base: str = os.getenv(
        "TMDB_BACKDROP_BASE", "https://image.tmdb.org/t/p/w1280"
    ).rstrip("/")

    # UI behaviour.
    default_search_mode: str = os.getenv("DEFAULT_SEARCH_MODE", "hybrid")
    explain_recommendations: bool = _as_bool(os.getenv("EXPLAIN_RECOMMENDATIONS"), True)
    ui_experience_mode: str = os.getenv("UI_EXPERIENCE_MODE", "showcase").lower()
    ui_experience_switch_enabled: bool = _as_bool(
        os.getenv("UI_EXPERIENCE_SWITCH_ENABLED"), True
    )
    showcase_enabled: bool = _as_bool(os.getenv("SHOWCASE_ENABLED"), True)
    presenter_mode_enabled: bool = _as_bool(os.getenv("PRESENTER_MODE_ENABLED"), True)
    resilience_lab_enabled: bool = _as_bool(os.getenv("RESILIENCE_LAB_ENABLED"), True)
    evaluation_dashboard_enabled: bool = _as_bool(os.getenv("EVALUATION_DASHBOARD_ENABLED"), True)
    capella_console_base_url: str = os.getenv("CAPELLA_CONSOLE_BASE_URL", "https://cloud.couchbase.com").rstrip("/")
    capella_project_id: str = os.getenv("CAPELLA_PROJECT_ID", "")
    capella_cluster_id: str = os.getenv("CAPELLA_CLUSTER_ID", "")
    capella_agent_tracer_url: str = os.getenv("CAPELLA_AGENT_TRACER_URL", "")
    capella_tools_hub_url: str = os.getenv("CAPELLA_TOOLS_HUB_URL", "")
    capella_prompts_hub_url: str = os.getenv("CAPELLA_PROMPTS_HUB_URL", "")
    capella_model_service_url: str = os.getenv("CAPELLA_MODEL_SERVICE_URL", "")
    showcase_reference_currency: str = os.getenv("SHOWCASE_REFERENCE_CURRENCY", "GBP")

    # Agent Memory value telemetry. Local Ollama has no per-token API fee; these
    # configurable rates provide an illustrative hosted-model equivalent.
    metrics_reference_model: str = os.getenv(
        "METRICS_REFERENCE_MODEL", "Comparable hosted LLM"
    )
    metrics_input_usd_per_million: float = float(
        os.getenv("METRICS_INPUT_USD_PER_MILLION", "2.50")
    )
    metrics_output_usd_per_million: float = float(
        os.getenv("METRICS_OUTPUT_USD_PER_MILLION", "10.00")
    )
    metrics_baseline_completion_tokens: int = int(
        os.getenv("METRICS_BASELINE_COMPLETION_TOKENS", "120")
    )
    metrics_recent_event_limit: int = int(
        os.getenv("METRICS_RECENT_EVENT_LIMIT", "60")
    )


settings = Settings()
