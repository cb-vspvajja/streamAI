from pathlib import Path


def test_agent_catalog_publisher_installs_sentence_transformers_and_cpu_torch() -> None:
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "agent-catalog-publisher" / "Dockerfile").read_text()
    assert '"sentence-transformers>=5.1,<6"' in dockerfile
    assert 'https://download.pytorch.org/whl/cpu' in dockerfile
    assert 'import packaging.version, sentence_transformers, agentc, agentc_cli' in dockerfile


def test_agent_catalog_runtime_preflight_imports_sentence_transformers() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "agent-catalog-publisher" / "publish.sh").read_text()
    assert "import sentence_transformers" in script
    assert "FORCE_AGENT_CATALOG_REBUILD=true" in script


def test_local_chat_endpoint_is_selected_by_execution_mode() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "05-start-ui.sh").read_text()
    assert 'native) CHAT_RUNTIME_BASE_URL="http://host.docker.internal:${OLLAMA_NATIVE_PORT:-11435}"' in script
    assert 'docker) CHAT_RUNTIME_BASE_URL="http://ollama:11434"' in script
    assert '-e CHAT_BASE_URL="$CHAT_RUNTIME_BASE_URL"' in script


def test_agent_catalog_embedding_model_is_exposed_in_local_profile() -> None:
    root = Path(__file__).resolve().parents[1]
    env_text = (root / ".env.local.example").read_text()
    assert "AGENT_CATALOG_EMBEDDING_MODEL=all-MiniLM-L12-v2" in env_text
