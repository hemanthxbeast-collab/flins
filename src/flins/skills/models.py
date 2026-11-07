"""What goes into and comes out of a skill (SPEC §9.2). Validated at the process boundary."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ContactCard(BaseModel):
    """Only with `contacts:read_card`. Never the phone number."""

    first_name: str
    tier: str
    language: str = "en"


class SkillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger: str  # "message" | "command:summary"
    messages: list[str] = Field(default_factory=list)  # the incoming burst (text only)
    category: str | None = None
    mode: str
    until_local: str | None = None  # "18:00" while a timed mode is on
    now_local: str  # "2026-11-08 14:05"
    timezone: str
    user_name: str
    signature: str = ""  # empty when this contact shouldn't be told about the AI
    contact: ContactCard | None = None
    facts: list[str] = Field(default_factory=list)  # what FLINS may already say about the user
    since_local: str | None = None  # command:summary: start of the session


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["reply", "notify", "note", "calendar_hold"]
    text: str = Field(default="", max_length=2000)
    payload: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0, le=1)
    facts_used: list[str] = Field(default_factory=list)


class SkillOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[Proposal] = Field(default_factory=list, max_length=10)
    facts: dict[str, str] = Field(default_factory=dict)
    logs: list[str] = Field(default_factory=list, max_length=50)

    def first(self, kind: str) -> Proposal | None:
        return next((p for p in self.proposals if p.kind == kind and p.text.strip()), None)
