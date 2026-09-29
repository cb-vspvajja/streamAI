from __future__ import annotations

import re
from typing import Any, Iterable


def _normalised_values(values: Iterable[Any]) -> set[str]:
    return {
        re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()
        for value in values
        if value
    }


def exclusive_content_policy(profile: dict[str, Any]) -> dict[str, Any] | None:
    """Return the allow-list created by an explicit “I only like …” turn."""
    if str(profile.get("exclusivePreferenceMode") or "").lower() != "content":
        return None
    allowed_genres = [
        str(value).strip() for value in profile.get("preferredGenres", []) if str(value).strip()
    ]
    allowed_themes = [
        str(value).strip() for value in profile.get("preferredThemes", []) if str(value).strip()
    ]
    if not allowed_genres and not allowed_themes:
        return None
    labels = [*allowed_genres, *allowed_themes]
    return {
        "active": True,
        "mode": "content",
        "allowedGenres": allowed_genres,
        "allowedThemes": allowed_themes,
        "label": ", ".join(labels),
    }


def exclusive_content_conflict(
    title: dict[str, Any], profile: dict[str, Any]
) -> dict[str, Any] | None:
    """Return a deterministic denial when a title is outside an exclusive allow-list."""
    policy = exclusive_content_policy(profile)
    if policy is None:
        return None
    title_genres = _normalised_values(title.get("genres", []))
    title_themes = _normalised_values(title.get("keywords", []))
    allowed_genres = _normalised_values(policy["allowedGenres"])
    allowed_themes = _normalised_values(policy["allowedThemes"])
    if title_genres.intersection(allowed_genres) or title_themes.intersection(allowed_themes):
        return None
    label = str(policy["label"])
    return {
        "code": "exclusive_content_preference",
        "reason": f"blocked by the viewer's exclusive {label} preference",
        "violation": f"exclusive preference: only {label} content is allowed",
        "policy": policy,
    }


def request_conflicts_with_exclusive_policy(
    requested_genres: Iterable[Any], policy: dict[str, Any] | None
) -> bool:
    if policy is None:
        return False
    requested = _normalised_values(requested_genres)
    if not requested:
        return False
    allowed = _normalised_values(policy.get("allowedGenres", []))
    return not bool(requested.intersection(allowed))
