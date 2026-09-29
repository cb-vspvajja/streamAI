from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_publisher_exports_agentc_native_indexes_and_legacy_aliases() -> None:
    script = (ROOT / "agent-catalog-publisher" / "publish.sh").read_text()
    assert 'export AGENT_CATALOG_CATALOG=".agent-catalog"' in script
    assert 'export_catalog_index tools.json tool-catalog.json' in script
    assert 'export_catalog_index prompts.json prompt-catalog.json' in script
    assert 'cp "$source_file" "$OUTPUT_DIR/$native_name"' in script
    assert 'cp "$source_file" "$OUTPUT_DIR/$legacy_name"' in script
    assert script.index('agentc publish --bucket') < script.index('export_catalog_index tools.json')


def test_host_publisher_verifies_native_runtime_snapshot_files() -> None:
    script = (ROOT / "scripts" / "04d-publish-agent-catalog.sh").read_text()
    assert 'tools.json prompts.json streamai-publish.json' in script
    assert 'Agent Catalog runtime snapshot exported to:' in script


def test_ui_requires_agentc_native_indexes() -> None:
    script = (ROOT / "scripts" / "05-start-ui.sh").read_text()
    assert 'ui/.agent-catalog/tools.json' in script
    assert 'ui/.agent-catalog/prompts.json' in script
    assert 'tool index is missing (tools.json)' in script
    assert 'prompt index is missing (prompts.json)' in script


def test_publish_marker_records_native_file_contract() -> None:
    script = (ROOT / "agent-catalog-publisher" / "publish.sh").read_text()
    assert '"nativeIndexFiles": ["tools.json", "prompts.json"]' in script
    assert '"compatibilityAliases": ["tool-catalog.json", "prompt-catalog.json"]' in script
