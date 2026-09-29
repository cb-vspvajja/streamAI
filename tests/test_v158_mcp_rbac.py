from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_local_profile_uses_dedicated_mcp_identity() -> None:
    text = (ROOT / ".env.local.example").read_text()
    assert "MCP_CB_USERNAME=streamai_mcp" in text
    assert "MCP_CB_PASSWORD=streamaiMcp123" in text
    assert "MCP_PREFLIGHT_SCHEMA_PERMISSION=true" in text


def test_local_provisioner_grants_read_only_metadata_role_to_mcp_only() -> None:
    text = (ROOT / "scripts/03-setup-content-plane.sh").read_text()
    assert 'MCP_USER="${MCP_CB_USERNAME:-streamai_mcp}"' in text
    assert 'mcp_roles="ro_admin,data_reader[$BUCKET],query_select[$BUCKET],fts_searcher[$BUCKET]"' in text
    assert 'name=Couchbase StreamAI read-only MCP runtime' in text
    assert 'app_roles="data_reader[$BUCKET],data_writer[$BUCKET]' in text


def test_mcp_startup_has_schema_permission_preflight() -> None:
    text = (ROOT / "scripts/02b-start-mcp-server.sh").read_text()
    assert "MCP_PREFLIGHT_SCHEMA_PERMISSION" in text
    assert "/pools/default/buckets/${CONTENT_BUCKET:-streaming}/scopes" in text
    assert "Grant the read-only admin role (ro_admin)" in text
    assert "MCP RBAC preflight passed" in text
