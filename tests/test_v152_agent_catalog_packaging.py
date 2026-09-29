from pathlib import Path


def test_agent_catalog_publisher_declares_packaging_and_smoke_tests_cli() -> None:
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "agent-catalog-publisher" / "Dockerfile").read_text()
    assert '"packaging>=24,<26"' in dockerfile
    assert "import packaging.version, sentence_transformers, agentc, agentc_cli" in dockerfile
    assert "agentc --help" in dockerfile


def test_runtime_requirements_include_agentc_packaging_dependency() -> None:
    root = Path(__file__).resolve().parents[1]
    assert "packaging>=24,<26" in (root / "requirements.txt").read_text()
    assert "packaging>=24,<26" in (root / "ui" / "requirements.txt").read_text()


def test_publisher_preflight_reports_rebuild_action() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "agent-catalog-publisher" / "publish.sh").read_text()
    assert "Agent Catalog publisher dependency check failed" in script
    assert "FORCE_AGENT_CATALOG_REBUILD=true" in script
