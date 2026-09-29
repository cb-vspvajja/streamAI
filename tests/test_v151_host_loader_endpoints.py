from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOADER = ROOT / "tools" / "load_catalogue.py"
SCRIPT = ROOT / "scripts" / "04-load-catalogue.sh"


def load_module():
    spec = importlib.util.spec_from_file_location("streamai_v151_loader", LOADER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_local_loader_translates_container_only_couchbase_hostname(monkeypatch):
    module = load_module()
    monkeypatch.setenv("DEPLOYMENT_TARGET", "local")
    monkeypatch.delenv("CB_LOADER_CONN_STRING", raising=False)
    monkeypatch.setenv("CB_CONN_STRING", "couchbase://host.docker.internal")
    assert module.loader_connection_string() == "couchbase://localhost"


def test_local_loader_translates_container_only_ollama_hostname(monkeypatch):
    module = load_module()
    monkeypatch.setenv("DEPLOYMENT_TARGET", "local")
    monkeypatch.delenv("EMBEDDING_LOADER_BASE_URL", raising=False)
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://ollama:11434")
    assert module.loader_embedding_url() == "http://localhost:11434"


def test_explicit_host_loader_endpoints_win(monkeypatch):
    module = load_module()
    monkeypatch.setenv("DEPLOYMENT_TARGET", "local")
    monkeypatch.setenv("CB_LOADER_CONN_STRING", "couchbase://127.0.0.1")
    monkeypatch.setenv("EMBEDDING_LOADER_BASE_URL", "http://127.0.0.1:11434/v1")
    assert module.loader_connection_string() == "couchbase://127.0.0.1"
    assert module.loader_embedding_url() == "http://127.0.0.1:11434/v1"


def test_local_loader_uses_admin_credentials_by_default(monkeypatch):
    module = load_module()
    monkeypatch.setenv("DEPLOYMENT_TARGET", "local")
    monkeypatch.delenv("CB_LOADER_USERNAME", raising=False)
    monkeypatch.delenv("CB_LOADER_PASSWORD", raising=False)
    monkeypatch.setenv("CB_ADMIN_USERNAME", "Administrator")
    monkeypatch.setenv("CB_ADMIN_PASSWORD", "password")
    monkeypatch.setenv("CB_USERNAME", "streamai")
    monkeypatch.setenv("CB_PASSWORD", "streamai123")
    assert module.loader_username() == "Administrator"
    assert module.loader_password() == "password"


def test_loader_script_preserves_command_line_overrides_and_sets_host_defaults():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'EXTERNAL_CB_LOADER_CONN_STRING="${CB_LOADER_CONN_STRING-}"' in text
    assert 'EXTERNAL_EMBEDDING_BASE_URL="${EMBEDDING_BASE_URL-}"' in text
    assert 'CB_LOADER_CONN_STRING:-couchbase://localhost' in text
    assert 'EMBEDDING_LOADER_BASE_URL' in text
    assert 'CB_LOADER_USERNAME' in text
