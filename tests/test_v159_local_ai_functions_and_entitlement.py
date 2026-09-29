from pathlib import Path

from ui.app.llm_service import LocalModelService


ROOT = Path(__file__).resolve().parents[1]


def test_entitlement_question_extracts_only_supergirl_title() -> None:
    assert LocalModelService.extract_catalogue_title_question(
        "Is Supergirl included in my plan?"
    ) == {"type": "availability", "title": "Supergirl"}


def test_entitlement_question_variants_extract_only_title() -> None:
    cases = {
        "Is Dune covered under my subscription?": "Dune",
        "Can I watch Moana with my plan?": "Moana",
        "Do I have access to Disclosure Day?": "Disclosure Day",
        "Is The Odyssey included with this package?": "The Odyssey",
    }
    for question, title in cases.items():
        assert LocalModelService.extract_catalogue_title_question(question) == {
            "type": "availability",
            "title": title,
        }


def test_local_ui_gates_capella_only_ai_functions() -> None:
    app_js = (ROOT / "ui/static/app.js").read_text()
    assert '"AI Functions · Capella only"' in app_js
    assert "const available = aiFunctions.enabled === true" in app_js
    assert "aiFunctionsButton.disabled = !available" in app_js
    assert "s.enabled === false || s.healthy !== false" in app_js


def test_ai_functions_status_explains_capella_only_requirement() -> None:
    source = (ROOT / "ui/app/capella_ai_services.py").read_text()
    assert '"availability": "capella_only"' in source
    assert "Capella-only: enable AI Functions" in source
