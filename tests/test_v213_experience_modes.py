from __future__ import annotations

from pathlib import Path

from ui.app.config import build_ui_experience_config


ROOT = Path(__file__).resolve().parents[1]


def test_showcase_is_the_backward_compatible_default_experience() -> None:
    config = build_ui_experience_config(
        "showcase",
        switch_enabled=True,
        showcase_enabled=True,
    )

    assert config["defaultExperience"] == "showcase"
    assert config["switchEnabled"] is True
    assert config["availableExperiences"] == ["showcase", "customer"]


def test_invalid_experience_mode_fails_safely_to_showcase() -> None:
    config = build_ui_experience_config(
        "unexpected-value",
        switch_enabled=True,
        showcase_enabled=True,
    )

    assert config["defaultExperience"] == "showcase"


def test_disabling_showcase_forces_and_locks_customer_experience() -> None:
    config = build_ui_experience_config(
        "showcase",
        switch_enabled=True,
        showcase_enabled=False,
    )

    assert config["defaultExperience"] == "customer"
    assert config["switchEnabled"] is False
    assert config["showcaseEnabled"] is False
    assert config["availableExperiences"] == ["customer"]


def test_customer_hidden_surfaces_are_explicitly_marked_in_markup() -> None:
    page = (ROOT / "ui/static/index.html").read_text(encoding="utf-8")

    required = (
        'id="metricsNavButton" data-showcase-only',
        'id="inspectorNavButton" data-showcase-only',
        'id="showcaseNavButton" data-showcase-only',
        'id="agentInspectorSection" data-showcase-only',
        'id="metricsSection" data-showcase-only',
        'class="data-plane-section" data-showcase-only',
        'id="memoryButton" data-showcase-only',
        'id="assistantInspectorButton" data-showcase-only',
        'id="searchMode" aria-label="Search mode" data-showcase-only',
    )
    for marker in required:
        assert marker in page


def test_browser_mode_is_persistent_and_hides_technical_metadata() -> None:
    script = (ROOT / "ui/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "ui/static/styles.css").read_text(encoding="utf-8")
    main = (ROOT / "ui/app/main.py").read_text(encoding="utf-8")

    assert 'const EXPERIENCE_STORAGE_KEY = "streamai.experience-mode.v1"' in script
    assert 'window.localStorage.setItem(EXPERIENCE_STORAGE_KEY, mode)' in script
    assert 'await api("/api/ui-config")' in script
    assert 'if (!isShowcaseExperience()) return;' in script
    assert 'html[data-experience-mode="customer"] [data-showcase-only]' in css
    assert 'html[data-experience-mode="customer"] .message-meta' in css
    assert '@app.get("/api/ui-config")' in main


def test_every_environment_profile_documents_experience_controls(effective_profile) -> None:
    profiles = (
        ".env.example",
        ".env.local.example",
        ".env.server.example",
        ".env.capella.example",
        ".env.capella-full-aidp.example",
        "ui/.env.example",
    )

    for relative in profiles:
        text = effective_profile(relative)
        assert "UI_EXPERIENCE_MODE=showcase" in text
        assert "UI_EXPERIENCE_SWITCH_ENABLED=true" in text
