from __future__ import annotations

import json
import sys
import types
from pathlib import Path

if "couchbase" not in sys.modules:
    couchbase = types.ModuleType("couchbase")
    auth = types.ModuleType("couchbase.auth")
    cluster = types.ModuleType("couchbase.cluster")
    exceptions = types.ModuleType("couchbase.exceptions")
    n1ql = types.ModuleType("couchbase.n1ql")
    options = types.ModuleType("couchbase.options")
    auth.PasswordAuthenticator = object
    cluster.Cluster = object
    exceptions.DocumentNotFoundException = type("DocumentNotFoundException", (Exception,), {})
    n1ql.QueryScanConsistency = types.SimpleNamespace(REQUEST_PLUS="request_plus")
    options.ClusterOptions = object
    options.QueryOptions = object
    sys.modules.update({
        "couchbase": couchbase,
        "couchbase.auth": auth,
        "couchbase.cluster": cluster,
        "couchbase.exceptions": exceptions,
        "couchbase.n1ql": n1ql,
        "couchbase.options": options,
    })

from ui.app.catalogue_service import CatalogueService
from ui.app.config import Settings
from ui.app.llm_service import LocalModelService
import ui.app.catalogue_service as catalogue_module


def bare_catalogue() -> CatalogueService:
    service = object.__new__(CatalogueService)
    service.settings = Settings()
    return service


def test_catalogue_count_intents_precede_browse_routing() -> None:
    assert LocalModelService.extract_catalogue_count_question(
        "How many movies are in the catalogue?"
    ) == {"contentType": "movie", "genre": None, "subject": "movies"}
    assert LocalModelService.extract_catalogue_count_question(
        "How many comedy movies are there in the catalogue?"
    ) == {"contentType": "movie", "genre": "Comedy", "subject": "comedy movies"}
    assert LocalModelService.extract_catalogue_count_question(
        "How many series do you have?"
    ) == {"contentType": "tv", "genre": None, "subject": "series"}
    assert LocalModelService.extract_catalogue_count_question(
        "How many movies have I watched?"
    ) is None
    assert LocalModelService.is_recommendation_request(
        "How many comedy movies are there in the catalogue?"
    ) is False


def test_catalogue_count_reply_is_exact_and_neutral() -> None:
    assert LocalModelService.render_catalogue_count(
        {"count": 120, "contentType": "movie", "genre": None}
    ) == "There are 120 movies in the current catalogue."
    assert LocalModelService.render_catalogue_count(
        {"count": 8, "contentType": "movie", "genre": "Comedy"}
    ) == "There are 8 Comedy movies in the current catalogue."
    assert LocalModelService.render_catalogue_count(
        {"count": 0, "contentType": "tv", "genre": "Horror"}
    ) == "There are no Horror series in the current catalogue."


def test_count_titles_uses_sqlpp_aggregate_and_structured_filters(monkeypatch) -> None:
    service = bare_catalogue()
    captured = {}

    class Result:
        def rows(self):
            return iter([7])

    class Cluster:
        def query(self, statement, options):
            captured["statement"] = statement
            captured["options"] = options
            return Result()

    service.cluster = Cluster()
    monkeypatch.setattr(catalogue_module, "QueryOptions", lambda **kwargs: kwargs)
    result = service.count_titles(content_type="movie", genre="Comedy")
    assert result == {
        "count": 7,
        "contentType": "movie",
        "genre": "Comedy",
        "queryMode": "sqlpp_aggregate",
    }
    assert "COUNT(1)" in captured["statement"]
    assert "t.contentType=$contentType" in captured["statement"]
    assert "ANY g IN t.genres" in captured["statement"]
    assert captured["options"]["named_parameters"] == {
        "contentType": "movie",
        "genre": "Comedy",
    }
    assert captured["options"]["readonly"] is True


def test_count_tool_is_governed_and_mcp_approved() -> None:
    root = Path(__file__).resolve().parents[1]
    main = (root / "ui/app/main.py").read_text()
    agent = (root / "ui/app/agent_service.py").read_text()
    mcp_gateway = (root / "ui/app/mcp_gateway.py").read_text()
    native_tool = (root / "ui/agent_catalog/tools/streamai_mcp_tools.py").read_text()
    approved = json.loads((root / "config/mcp/approved-application-tools.json").read_text())
    assert 'intent="catalogue_count_query"' in main
    assert 'tools=["get_catalogue_statistics"]' in main
    assert '"get_catalogue_statistics": {"count_titles"}' in agent
    assert (
        '"get_catalogue_statistics": self._business_get_catalogue_statistics'
        in mcp_gateway
    )
    assert "def get_catalogue_statistics(" in native_tool
    assert "get_catalogue_statistics" in approved["businessToolsGovernedByAgentCatalog"]


def test_viewer_facing_copy_does_not_expose_database_vendor_terms() -> None:
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "ui/app/llm_service.py",
        root / "ui/static/app.js",
        root / "ui/static/index.html",
        root / "ui/agent_catalog/prompts/streaming_assistant_system.prompt",
    ]
    combined = "\n".join(path.read_text() for path in paths)
    forbidden = (
        "Couchbase catalogue",
        "recorded in Couchbase",
        "Couchbase entitlement document",
        "Grounded Couchbase catalogue results",
        "Every title answer is grounded in the Couchbase catalogue",
    )
    for phrase in forbidden:
        assert phrase not in combined
