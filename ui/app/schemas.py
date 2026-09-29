from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RegisterViewerRequest(BaseModel):
    name: str = Field(default="Sai Vajja", min_length=1, max_length=120)
    login_id: str = Field(min_length=3, max_length=64)
    pin: str = Field(min_length=4, max_length=12)


class LoginRequest(BaseModel):
    login_id: str = Field(min_length=3, max_length=64)
    pin: str = Field(min_length=4, max_length=12)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    capture_long_term: bool = True
    search_mode: str = Field(default="hybrid", pattern="^(fts|vector|hybrid)$")


class NewSessionRequest(BaseModel):
    label: str | None = Field(default=None, max_length=120)


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    scope: str = Field(default="all", pattern="^(short|long|all)$")


class CatalogueSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    mode: str = Field(default="hybrid", pattern="^(fts|vector|hybrid)$")
    content_type: str | None = Field(default=None, pattern="^(movie|tv)$")
    limit: int = Field(default=20, ge=1, le=50)


class InteractionRequest(BaseModel):
    title_id: str = Field(min_length=1, max_length=120)
    action: str = Field(pattern="^(play|progress|complete|like|dislike|watchlist|remove_watchlist)$")
    progress_pct: float | None = Field(default=None, ge=0, le=100)


class PreferenceUpdateRequest(BaseModel):
    preferred_genres: list[str] = Field(default_factory=list, max_length=50)
    disliked_genres: list[str] = Field(default_factory=list, max_length=50)
    preferred_themes: list[str] = Field(default_factory=list, max_length=50)
    disliked_themes: list[str] = Field(default_factory=list, max_length=50)
    preferred_people: list[str] = Field(default_factory=list, max_length=50)
    disliked_people: list[str] = Field(default_factory=list, max_length=50)
    preferred_languages: list[str] = Field(default_factory=list, max_length=30)
    preferred_content_types: list[str] = Field(default_factory=list, max_length=5)
    max_runtime_minutes: int | None = Field(default=None, ge=30, le=600)
    avoid_graphic_violence: bool = False
    avoid_adult_content: bool = False


class StructuredPreference(BaseModel):
    kind: str
    value: str | int | bool
    sentiment: str = "like"
    fact: str
    source: str = "explicit_viewer_statement"
    metadata: dict[str, Any] = Field(default_factory=dict)

class ShowcasePersonaRequest(BaseModel):
    persona_id: str = Field(default="executive", pattern="^(executive|family|guest)$")


class ShowcaseFaultRequest(BaseModel):
    mcp_unavailable: bool = False
    embedding_timeout: bool = False
    model_unavailable: bool = False
    search_unavailable: bool = False
    agent_memory_unavailable: bool = False


class SearchLabRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    content_type: str | None = Field(default=None, pattern="^(movie|tv)$")
    limit: int = Field(default=8, ge=1, le=20)


class RecommendationEvidenceRequest(BaseModel):
    title_id: str = Field(min_length=1, max_length=160)
    item: dict[str, Any] = Field(default_factory=dict)


class EntitlementSimulationRequest(BaseModel):
    title_id: str = Field(min_length=1, max_length=160)
    region: str | None = Field(default=None, max_length=3)
    tier: str | None = Field(default=None, pattern="^(free|basic|standard|premium)$")
    parental_rating: str | None = Field(default=None, max_length=12)
    device_type: str | None = Field(default=None, max_length=60)
    roaming_allowed: bool | None = None
