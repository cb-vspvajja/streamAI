# Native Couchbase Agent Catalog

The authoritative source is under `ui/agent_catalog`:

- `prompts/streaming_assistant_system.prompt`
- `tools/streamai_mcp_tools.py`

`scripts/04d-publish-agent-catalog.sh` builds a Python 3.12 publisher with
`agentc>=1.1.0,<1.2`, creates a clean Git commit, runs `agentc index`, and publishes
the catalog to the configured Couchbase bucket. The UI copies the generated
`.agent-catalog` files into its image and loads the published prompt/tool
versions at runtime with `agentc.Catalog()`.

Tool execution is owned by the StreamAI agent runtime, as intended by Agent
Catalog. The default runtime executor is the warm Couchbase MCP Server client.
Set `AGENT_CATALOG_EXECUTION_MODE=catalog_function` only for troubleshooting.
