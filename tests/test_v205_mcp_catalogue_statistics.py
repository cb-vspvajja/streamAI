from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ui.app.mcp_gateway import MCPCall, MCPGateway


def make_gateway() -> MCPGateway:
    return MCPGateway(
        SimpleNamespace(
            mcp_enabled=True,
            mcp_required=True,
            mcp_url="http://localhost:8000/mcp",
            mcp_timeout_seconds=8,
            mcp_bearer_token="",
            mcp_read_only=True,
            content_bucket="streaming",
            titles_collection="titles",
            catalogue_scope="catalogue",
        )
    )


def test_business_catalogue_statistics_adapter_executes_scoped_mcp_query() -> None:
    gateway = make_gateway()
    captured: dict[str, object] = {}

    async def fake_run_query(query, named_parameters, *, scope_name):
        captured.update(
            query=query,
            named_parameters=named_parameters,
            scope_name=scope_name,
        )
        return MCPCall(
            tool="run_sql_plus_plus_query",
            arguments={"query": query},
            result=[{"count": 42}],
            duration_ms=2.5,
        )

    gateway._run_query = fake_run_query  # type: ignore[method-assign]
    result = asyncio.run(
        gateway.call_business_tool(
            "get_catalogue_statistics",
            {"content_type": "movie", "genre": "Family"},
        )
    )

    assert result["count"] == 42
    assert result["contentType"] == "movie"
    assert result["genre"] == "Family"
    assert result["queryMode"] == "agent_catalog_mcp_sqlpp_aggregate"
    assert captured["scope_name"] == "catalogue"
    assert captured["named_parameters"] == {
        "contentType": "movie",
        "genre": "Family",
    }
    assert "COUNT(1) AS count" in str(captured["query"])
    assert "ANY g IN t.genres" in str(captured["query"])
    assert result["_nativeExecution"]["businessTool"] == "get_catalogue_statistics"
    assert result["_nativeExecution"]["mcpPrimitives"][0]["tool"] == (
        "run_sql_plus_plus_query"
    )
    assert gateway.snapshot()["businessCalls"] == 1


def test_business_catalogue_statistics_adapter_preserves_unfiltered_count() -> None:
    gateway = make_gateway()

    async def fake_run_query(query, named_parameters, *, scope_name):
        assert named_parameters == {"contentType": None, "genre": None}
        assert scope_name == "catalogue"
        return MCPCall(
            tool="run_sql_plus_plus_query",
            arguments={"query": query},
            result={"rows": [{"count": 137}]},
            duration_ms=1.0,
        )

    gateway._run_query = fake_run_query  # type: ignore[method-assign]
    result = asyncio.run(gateway.call_business_tool("get_catalogue_statistics", {}))

    assert result["count"] == 137
    assert result["contentType"] is None
    assert result["genre"] is None


def test_business_catalogue_statistics_runs_through_warm_mcp_session() -> None:
    gateway = make_gateway()
    captured: dict[str, object] = {}

    class Session:
        async def call_tool(self, name, *, arguments):
            captured.update(name=name, arguments=arguments)
            return SimpleNamespace(
                isError=False,
                structuredContent={"rows": [{"count": 18}]},
                content=[],
            )

    gateway._session = Session()
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

    result = asyncio.run(
        gateway.call_business_tool(
            "get_catalogue_statistics",
            {"content_type": "tv", "genre": "Comedy"},
        )
    )

    assert result["count"] == 18
    assert captured["name"] == "run_sql_plus_plus_query"
    arguments = captured["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["bucket"] == "streaming"
    assert arguments["scope"] == "catalogue"
    assert arguments["query_parameters"] == (
        '{"contentType": "tv", "genre": "Comedy"}'
    )
    snapshot = gateway.snapshot()
    assert snapshot["businessCalls"] == 1
    assert snapshot["primitiveCalls"] == 1
