from pathlib import Path

import yaml


def test_agent_catalog_prompt_is_one_yaml_document_with_content() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "ui" / "agent_catalog" / "prompts" / "streaming_assistant_system.prompt"
    text = path.read_text()
    document = yaml.safe_load(text)
    assert isinstance(document, dict)
    assert document["record_kind"] == "prompt"
    assert document["name"] == "streaming_assistant_system"
    assert isinstance(document["content"], str)
    assert "streaming service assistant" in document["content"]
    assert text.count("---") == 0


def test_publisher_validates_prompt_yaml_before_agentc_index() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "agent-catalog-publisher" / "publish.sh").read_text()
    assert "yaml.safe_load" in script
    assert "Agent Catalog prompt must be one YAML object" in script
    assert script.index("yaml.safe_load") < script.index('agentc index "$PROJECT_DIR/agent_catalog"')
