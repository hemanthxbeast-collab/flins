"""Triage result shape (SPEC §6 step 6).

The LLM triage arrives in M3. Until then, and whenever triage fails, `fallback_category()`
gives a deterministic category, and `decide()` treats a missing triage as 'unknown' (DRAFT at most).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from flins.core.levels import Level


class TriageResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str
    urgency: int = Field(default=0, ge=0, le=3)
    flags: list[str] = Field(default_factory=list)
    needs_human: bool = False
    injection_suspected: bool = False
    language: str | None = None
    summary: str | None = None

    def ceiling(self) -> Level | None:
        """Triage can only lower the level. This is its own cap, if any."""
        if self.injection_suspected:
            return Level.NOTIFY
        if self.needs_human:
            return Level.DRAFT
        return None

    def all_flags(self) -> frozenset[str]:
        extra = {"injection_suspected"} if self.injection_suspected else set()
        return frozenset(self.flags) | extra


def fallback_category(*, msg_type: str, text: str | None, is_group: bool) -> str:
    """Category without an LLM: only the cases that are obvious from structure."""
    if is_group:
        return "group_message"
    if msg_type != "text" and not (text or "").strip():
        return "media_only"
    return "unknown"
