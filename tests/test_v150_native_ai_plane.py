from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from ui.app.agent_service import GovernedAgentService
from ui.app.mcp_gateway import MCPGateway


def test_mcp_normalises_text_and_structured_results() -> None:
    text_item = SimpleNamespace(text='{"results":[{"id":"movie::1"}]}')
    response = SimpleNamespace(content=[text_item], structuredContent=None)
    assert MCPGateway.normalise_result(response) == [{"id": "movie::1"}]

    structured = SimpleNamespace(
        content=[], structuredContent={"document": {"viewerId": "sai"}}
    )
    assert MCPGateway.normalise_result(structured) == {"viewerId": "sai"}


def test_agent_catalog_gateway_loads_prompt_tools_and_traces(monkeypatch) -> None:
    class Meta:
        description = "tool description"
        catalog_id = "catalog-150"

    class Item:
        def __init__(self, name: str, prompt: bool = False):
            self.meta = Meta()
            self.content = "catalogued system prompt" if prompt else None
            self.func = lambda **kwargs: {"name": name, **kwargs}

    class Span:
        def __init__(self, name: str, **tags):
            self.name, self.tags, self.logs = name, tags, []
        def log(self, content, **tags):
            self.logs.append((content, tags))
        def new(self, name: str, **tags):
            return Span(name, **tags)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False

    class Catalog:
        def find(self, kind, name=None, **_):
            return Item(name or "unknown", prompt=kind == "prompt")
        def Span(self, name, **tags):
            return Span(name, **tags)

    import agentc
    monkeypatch.setattr(agentc, "Catalog", Catalog)
    from ui.app.agent_catalog_gateway import AgentCatalogGateway

    settings = SimpleNamespace(
        agent_catalog_enabled=True,
        agent_catalog_required=True,
        agent_catalog_prompt_name="streaming_assistant_system",
        native_agent_tracing_enabled=True,
        app_version="1.5.0",
    )
    gateway = AgentCatalogGateway(settings, ["get_viewer_context"])
    assert gateway.healthy
    assert gateway.prompt_content == "catalogued system prompt"
    assert gateway.catalog_id == "catalog-150"
    root = gateway.start_trace(
        trace_id="trace::1", viewer_id="sai", session_id="session::1", user_message="hello"
    )
    async def runtime_executor(name, inputs):
        return {"name": name, **inputs, "source": "mcp"}
    execution = asyncio.run(
        gateway.execute_tool(
            name="get_viewer_context",
            inputs={"viewer_id": "sai"},
            root_span=root,
            trace_id="trace::1",
            runtime_executor=runtime_executor,
        )
    )
    assert execution.result["viewer_id"] == "sai"
    assert execution.transport == "couchbase_mcp_server"


def test_governed_agent_routes_matching_reads_to_catalog() -> None:
    class Catalogue:
        def sync_agent_catalog(self, _):
            pass
        def get_profile(self, viewer_id):
            return {"viewerId": viewer_id, "source": "direct"}

    class Gateway:
        healthy = True
        required = True
        prompt_content = "native prompt"
        catalog_id = "cat-1"
        settings = SimpleNamespace(agent_catalog_prompt_name="streaming_assistant_system")
        def tool_available(self, name):
            return name == "get_viewer_context"
        async def execute_tool(self, **kwargs):
            return SimpleNamespace(
                result={"viewerId": kwargs["inputs"]["viewer_id"], "source": "mcp"},
                duration_ms=2.0,
                catalog_id="cat-1",
            )
        def start_trace(self, **_):
            return None
        def snapshot(self):
            return {"healthy": True, "catalogId": "cat-1", "promptName": "streaming_assistant_system", "nativeTracerEnabled": True}

    async def mcp_business_tool(name, inputs):
        return {"viewerId": inputs["viewer_id"], "source": "mcp", "tool": name}
    mcp = SimpleNamespace(
        healthy=True,
        required=True,
        snapshot=lambda: {"healthy": True},
        call_business_tool=mcp_business_tool,
    )
    service = GovernedAgentService(
        Catalogue(), agent_catalog_gateway=Gateway(), mcp_gateway=mcp
    )
    trace = service.new_trace(viewer_id="sai", session_id="s1", user_message="profile")
    result = asyncio.run(
        service.try_catalog_tool(
            trace,
            name="get_viewer_context",
            inputs={"viewer_id": "sai"},
            fallback_callable=Catalogue().get_profile,
        )
    )
    assert result["used"] is True
    assert result["result"]["source"] == "mcp"


def test_native_scripts_and_catalog_sources_are_packaged() -> None:
    root = Path(__file__).resolve().parents[1]
    required = [
        "scripts/02b-start-mcp-server.sh",
        "mcp-server/Dockerfile",
        "scripts/04d-publish-agent-catalog.sh",
        "agent-catalog-publisher/Dockerfile",
        "ui/agent_catalog/tools/streamai_mcp_tools.py",
        "ui/agent_catalog/prompts/streaming_assistant_system.prompt",
    ]
    for path in required:
        assert (root / path).exists(), path


def test_environment_profiles_require_native_components(effective_profile) -> None:
    root = Path(__file__).resolve().parents[1]
    for name in (".env.local.example", ".env.server.example", ".env.capella.example", ".env.capella-full-aidp.example"):
        text = effective_profile(name)
        assert "MCP_ENABLED=true" in text
        assert "MCP_REQUIRED=true" in text
        assert "AGENT_CATALOG_ENABLED=true" in text
        assert "AGENT_CATALOG_REQUIRED=true" in text
        assert "NATIVE_AGENT_TRACING_ENABLED=true" in text



def test_mcp_argument_adapter_supports_published_schema_aliases() -> None:
    settings = SimpleNamespace(
        mcp_enabled=True,
        mcp_required=True,
        mcp_url="http://localhost:8000/mcp",
        mcp_timeout_seconds=8,
        mcp_bearer_token="",
        mcp_read_only=True,
    )
    gateway = MCPGateway(settings)
    gateway._tool_map = {
        "run_sql_plus_plus_query": {
            "inputSchema": {
                "type": "object",
                "properties": {
                    "bucket": {"type": "string"},
                    "scope": {"type": "string"},
                    "query_string": {"type": "string"},
                    "query_parameters": {"type": "string"},
                },
                "required": ["bucket", "scope", "query_string"],
            }
        }
    }
    adapted = gateway._adapt_arguments(
        "run_sql_plus_plus_query",
        {
            "bucket_name": "streaming",
            "scope_name": "viewers",
            "query": "SELECT 1",
            "named_parameters": {"viewerId": "sai"},
        },
    )
    assert adapted["bucket"] == "streaming"
    assert adapted["scope"] == "viewers"
    assert adapted["query_string"] == "SELECT 1"
    assert adapted["query_parameters"] == '{"viewerId": "sai"}'


def test_mcp_schema_proof_counts_collections() -> None:
    payload = {
        "scopes": [
            {"name": "catalogue", "collections": [{"name": "titles"}]},
            {
                "name": "viewers",
                "collections": [
                    {"name": "profiles"},
                    {"name": "watch_history"},
                ],
            },
        ]
    }
    assert MCPGateway._count_schema_collections(payload) == 3


def test_native_agent_tracer_closes_root_and_child_spans(monkeypatch) -> None:
    spans = []

    class Meta:
        description = "tool description"
        catalog_id = "catalog-150"

    class Item:
        def __init__(self, name: str, prompt: bool = False):
            self.meta = Meta()
            self.content = "native prompt" if prompt else None
            self.func = lambda **kwargs: kwargs

    class Span:
        def __init__(self, name: str, **tags):
            self.name = name
            self.tags = tags
            self.logs = []
            self.entered = 0
            self.exited = 0
            spans.append(self)
        def enter(self):
            self.entered += 1
        def exit(self):
            self.exited += 1
        def log(self, content=None, **_):
            self.logs.append(content)
        def new(self, name: str, **tags):
            return Span(name, **tags)

    class Catalog:
        def find(self, kind, name=None, **_):
            return Item(name or "unknown", prompt=kind == "prompt")
        def Span(self, name, **tags):
            return Span(name, **tags)

    import agentc
    monkeypatch.setattr(agentc, "Catalog", Catalog)
    from ui.app.agent_catalog_gateway import AgentCatalogGateway

    gateway = AgentCatalogGateway(
        SimpleNamespace(
            agent_catalog_enabled=True,
            agent_catalog_required=True,
            agent_catalog_prompt_name="streaming_assistant_system",
            native_agent_tracing_enabled=True,
            app_version="1.5.0",
        ),
        ["get_viewer_context"],
    )
    root = gateway.start_trace(
        trace_id="trace::1",
        viewer_id="sai",
        session_id="s1",
        user_message="hello",
    )

    async def executor(_name, inputs):
        return inputs

    asyncio.run(
        gateway.execute_tool(
            name="get_viewer_context",
            inputs={"viewer_id": "sai"},
            root_span=root,
            runtime_executor=executor,
        )
    )
    gateway.finish_trace(
        root,
        assistant_response="done",
        response_mode="test",
        response_ms=2.0,
        grounded_title_ids=[],
    )
    assert spans[0].entered == 1 and spans[0].exited == 1
    assert spans[1].entered == 1 and spans[1].exited == 1


def test_readme_documents_native_package_setup_and_conditional_services() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "README.md").read_text()
    for phrase in (
        "couchbase-mcp-server==1.0.0.post1",
        "agentc>=1.1.0,<1.2",
        "Agent Catalog",
        "Agent Tracer",
        "AI Functions",
        "Data Processing",
        "semantic cache",
    ):
        assert phrase in text


def test_environment_profiles_include_complete_agent_memory_server_configuration(effective_profile) -> None:
    root = Path(__file__).resolve().parents[1]
    required = (
        "AGENTMEMORY_CONN_STRING=",
        "AGENTMEMORY_USERNAME=",
        "AGENTMEMORY_PASSWORD=",
        "AGENTMEMORY_BUCKET=agent_memory",
        "AGENTMEMORY_EMBEDDING_MODEL=",
        "AGENTMEMORY_EMBEDDING_URL=",
        "AGENTMEMORY_LLM_MODEL=",
        "AGENTMEMORY_LLM_URL=",
    )
    for name in (
        ".env.local.example",
        ".env.server.example",
        ".env.capella.example",
        ".env.capella-full-aidp.example",
    ):
        text = effective_profile(name)
        for value in required:
            assert value in text, (name, value)


def test_full_capella_profile_enables_managed_ai_services(effective_profile) -> None:
    root = Path(__file__).resolve().parents[1]
    text = effective_profile(".env.capella-full-aidp.example")
    assert "CHAT_PROVIDER=capella_model_service" in text
    assert "EMBEDDING_PROVIDER=capella_model_service" in text
    assert "AI_FUNCTIONS_ENABLED=true" in text
    assert "DATA_PROCESSING_MODE=capella_workflow" in text
    assert "DATA_PROCESSING_WORKFLOW_ID=CHANGE_ME_WORKFLOW_ID" in text


def test_standard_capella_profile_does_not_require_managed_data_processing_workflow() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / ".env.capella.example").read_text()
    assert "DATA_PROCESSING_MODE=python_loader" in text
    assert "DATA_PROCESSING_WORKFLOW_ID=\n" in text


def test_agent_memory_launcher_sources_selected_environment_profile() -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts/02-start-agent-memory.sh").read_text()
    assert 'source "$ENV_FILE"' in text
    assert text.index('source "$ENV_FILE"') < text.index('TARGET="${DEPLOYMENT_TARGET:-local}"')


def test_mcp_query_inlines_parameters_when_server_schema_has_no_parameter_field() -> None:
    settings = SimpleNamespace(
        content_bucket="streaming",
        mcp_enabled=True,
        mcp_required=True,
        mcp_url="http://localhost:8000/mcp",
        mcp_timeout_seconds=5,
    )
    gateway = MCPGateway(settings)
    gateway._tool_map = {
        "run_sql_plus_plus_query": {
            "inputSchema": {
                "type": "object",
                "properties": {
                    "bucket_name": {"type": "string"},
                    "scope_name": {"type": "string"},
                    "query": {"type": "string"},
                },
                "required": ["bucket_name", "scope_name", "query"],
            }
        }
    }
    captured = {}

    async def fake_call_tool(name, arguments):
        captured.update(arguments)
        return SimpleNamespace(tool=name, result=[], duration_ms=1.0)

    gateway.call_tool = fake_call_tool
    asyncio.run(
        gateway._run_query(
            "SELECT * FROM `titles` WHERE title=$title AND META().id IN $ids AND active=$active",
            {"title": 'Dune "Part Two"', "ids": ["movie::1", "movie::2"], "active": True},
            scope_name="catalogue",
        )
    )
    assert "named_parameters" not in captured
    assert '$title' not in captured["query"]
    assert 'Dune \\"Part Two\\"' in captured["query"]
    assert '["movie::1","movie::2"]' in captured["query"]
    assert 'active=true' in captured["query"]


def test_mcp_business_queries_use_scope_local_collection_names() -> None:
    text = (Path(__file__).resolve().parents[1] / "ui/app/mcp_gateway.py").read_text()
    assert 'FROM `{self._identifier(self.settings.watch_history_collection)}` AS h' in text
    assert 'FROM `{self._identifier(self.settings.titles_collection)}` AS t' in text
    assert 'FROM `{bucket}`.`{viewers_scope}`' not in text
