"""Validation for persisted AI viewing guides; never an entitlement authority."""
from __future__ import annotations

import hashlib
import json
from typing import Any

VERSION = 1
FUNCTIONS = ("default:ai_classification", "default:ai_summary", "default:ai_sentiment")
FIELDS = ("classification", "summary", "sentiment")


def source_fingerprint(document: dict[str, Any]) -> str:
    material = {"title": str(document.get("title") or ""),
                "overview": str(document.get("overview") or "")}
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def response_text(value: Any) -> str:
    """Accept documented function responses and reject missing/error payloads."""
    if isinstance(value, str):
        result = value.strip()
    elif isinstance(value, list):
        result = "\n".join(response_text(part) for part in value)
    elif isinstance(value, dict) and not any(value.get(key) for key in ("error", "errors")):
        result = response_text(value.get("response"))
    else:
        raise ValueError("An AI Function returned an error or an unsupported response. No viewing guide was saved.")
    if not result:
        raise ValueError("An AI Function returned no text. No viewing guide was saved.")
    return result


def current_guide(document: dict[str, Any]) -> dict[str, Any] | None:
    guide = document.get("aiEnrichment")
    if not isinstance(guide, dict) or guide.get("version") != VERSION:
        return None
    if guide.get("sourceFingerprint") != source_fingerprint(document):
        return None
    if not all(isinstance(guide.get(field), str) and guide[field].strip() for field in FIELDS):
        return None
    if guide.get("status") != "succeeded" or not guide.get("executionId"):
        return None
    return guide
