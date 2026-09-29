from ui.app.agent_service import GovernedAgentService


def test_session_thread_reconstructs_conversation_and_tool_activity() -> None:
    traces = [
        {
            "type": "streamai_agent_trace",
            "traceId": "trace-1",
            "viewerId": "sai",
            "sessionId": "session-1",
            "startedAt": 10.0,
            "completedAt": 10.2,
            "status": "completed",
            "userMessage": "What have I watched?",
            "assistantResponse": "You recently watched Dune.",
            "responseMode": "watch_history_grounded",
            "responseMs": 200,
            "llmInvoked": False,
            "agentCatalogId": "catalog-abc",
            "plan": {"intent": "watch_history_query", "tools": ["get_watch_history"], "deterministic": True},
            "spans": [{
                "spanId": "span-1",
                "name": "get_watch_history",
                "status": "completed",
                "startedAt": 10.05,
                "completedAt": 10.1,
                "durationMs": 50,
                "inputs": {"viewer_id": "sai"},
                "output": {"resultCount": 1, "titles": ["Dune"]},
                "components": ["Query", "Index", "Data/KV"],
                "executionSource": "agent_catalog_mcp",
                "transport": "mcp",
                "catalogId": "catalog-abc",
            }],
            "groundedTitleIds": ["movie::dune"],
            "policy": {"nativeAgentTracerLogged": True},
        },
        {
            "type": "streamai_agent_trace",
            "traceId": "trace-2",
            "viewerId": "sai",
            "sessionId": "session-1",
            "startedAt": 20.0,
            "completedAt": 20.4,
            "status": "completed",
            "userMessage": "Suggest something similar.",
            "assistantResponse": "Try Arrival.",
            "responseMode": "profile_recommendation_grounded",
            "responseMs": 400,
            "llmInvoked": True,
            "agentCatalogId": "catalog-abc",
            "plan": {"intent": "personalised_recommendation", "tools": ["rank_personalised_candidates"], "deterministic": False},
            "spans": [],
            "groundedTitleIds": ["movie::arrival"],
            "policy": {},
        },
    ]

    thread = GovernedAgentService.build_session_thread(list(reversed(traces)))

    assert thread["sessionId"] == "session-1"
    assert thread["turnCount"] == 2
    assert thread["turns"][0]["traceId"] == "trace-1"
    assert thread["turns"][1]["traceId"] == "trace-2"
    assert [event["type"] for event in thread["turns"][0]["events"]] == [
        "user_message", "agent_plan", "tool_call", "assistant_message"
    ]
    tool = next(event for event in thread["events"] if event["type"] == "tool_call")
    assert tool["inputs"] == {"viewer_id": "sai"}
    assert tool["output"]["titles"] == ["Dune"]
    assert tool["component"] == "Agent Catalog → MCP"
    assert "MCP Server" in thread["components"]
    assert "Model Runtime" in thread["components"]
    assert thread["catalogIds"] == ["catalog-abc"]


def test_trace_thread_ui_and_session_endpoint_are_present() -> None:
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    script = (root / "ui/static/app.js").read_text()
    page = (root / "ui/static/index.html").read_text()
    main = (root / "ui/app/main.py").read_text()
    catalogue = (root / "ui/app/catalogue_service.py").read_text()

    assert 'id="traceThreadDialog"' in page
    assert "View full session" in script
    assert "/api/agent/sessions/" in script
    assert "Download JSON" in page
    assert '@app.get("/api/agent/sessions/{session_id:path}")' in main
    assert "list_agent_session_traces" in catalogue


def test_session_trace_index_is_provisioned() -> None:
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert "ix_traces_viewer_session" in (root / "tools/provision_content_plane.py").read_text()
    assert "ix_traces_viewer_session" in (root / "scripts/03-setup-content-plane.sh").read_text()
