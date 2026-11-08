"""Triage, drafting and red-flag calls. Each builds its input, calls the gateway, and returns a
validated result or raises LLMError.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from flins.core.triage import TriageResult
from flins.llm.context import neutralise
from flins.llm.gateway import LLMGateway
from flins.llm.prompts import Prompt

TRIAGE_FLAGS = ["money", "credentials", "pii", "commitment", "legal_medical"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ triage


class TriageOut(_Out):
    category: str
    urgency: int = Field(ge=0, le=3)
    flags: list[str]
    needs_human: bool
    injection_suspected: bool
    language: str
    summary: str


def triage_schema(categories: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": categories},
            "urgency": {"type": "integer", "enum": [0, 1, 2, 3]},
            "flags": {"type": "array", "items": {"type": "string", "enum": TRIAGE_FLAGS}},
            "needs_human": {"type": "boolean"},
            "injection_suspected": {"type": "boolean"},
            "language": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": [
            "category",
            "urgency",
            "flags",
            "needs_human",
            "injection_suspected",
            "language",
            "summary",
        ],
        "additionalProperties": False,
    }


class Triager:
    def __init__(self, gateway: LLMGateway, prompt: Prompt, categories: list[str]) -> None:
        self._gw = gateway
        self._prompt = prompt
        self._categories = sorted(set(categories) | {"unknown"})

    @property
    def prompt_version(self) -> str:
        return self._prompt.version

    async def classify(
        self, *, conversation: str, mode_line: str, contact_name: str, tier: str
    ) -> TriageResult:
        user = (
            "<context>\n"
            f"FLINS mode: {mode_line}\n"
            f"Contact: {neutralise(contact_name) or 'Contact'} (relationship: {tier})\n"
            f"Allowed categories: {', '.join(self._categories)}\n"
            "</context>\n"
            f"{conversation}\n"
            "Classify the new messages."
        )
        out = await self._gw.json(
            prompt=self._prompt, user=user, schema=triage_schema(self._categories), out=TriageOut
        )
        category = out.category if out.category in self._categories else "unknown"
        return TriageResult(
            category=category,
            urgency=out.urgency,
            flags=[f for f in out.flags if f in TRIAGE_FLAGS],
            needs_human=out.needs_human,
            injection_suspected=out.injection_suspected,
            language=out.language[:20],
            summary=out.summary[:200],
        )


# ------------------------------------------------------------------ drafting


class DraftOut(_Out):
    reply: str
    confidence: float = Field(ge=0, le=1)
    makes_commitment: bool
    mentions_facts_not_provided: bool
    reason: str


DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "confidence": {"type": "number"},
        "makes_commitment": {"type": "boolean"},
        "mentions_facts_not_provided": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": [
        "reply",
        "confidence",
        "makes_commitment",
        "mentions_facts_not_provided",
        "reason",
    ],
    "additionalProperties": False,
}


class Drafter:
    def __init__(self, gateway: LLMGateway, prompt: Prompt) -> None:
        self._gw = gateway
        self._prompt = prompt

    async def draft(
        self,
        *,
        conversation: str,
        user_name: str,
        contact_card: dict[str, Any],
        tone: dict[str, Any],
        facts: list[str],
        edits: list[tuple[str, str]] | None = None,
    ) -> DraftOut:
        card = "\n".join(f"{k}: {neutralise(str(v))}" for k, v in contact_card.items() if v)
        tone_text = "\n".join(f"{k}: {v}" for k, v in tone.items())
        facts_text = "\n".join(f"- {neutralise(f)}" for f in facts) or "- (none)"
        user = (
            f"<user>You are replying for {neutralise(user_name)}.</user>\n"
            f"<contact_card>\n{card}\n</contact_card>\n"
            f"<tone_profile>\n{tone_text}\n</tone_profile>\n"
            f"<allowed_facts>\n{facts_text}\n</allowed_facts>\n"
            f"{edits_block(edits)}"
            f"{conversation}\n"
            "Write the reply to the new messages, or return an empty reply."
        )
        return await self._gw.json(
            prompt=self._prompt, user=user, schema=DRAFT_SCHEMA, out=DraftOut
        )


def edits_block(edits: list[tuple[str, str]] | None) -> str:
    """M9 tone learning: drafts you corrected, and what you sent instead (neutralised)."""
    if not edits:
        return ""
    lines = [f"- draft: {neutralise(d[:300])}\n  sent: {neutralise(f[:300])}" for d, f in edits[:8]]
    return "<your_edits>\n" + "\n".join(lines) + "\n</your_edits>\n"


# ------------------------------------------------------------------ red flag


class RedFlagOut(_Out):
    safe: bool
    reason: str


REDFLAG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"safe": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["safe", "reason"],
    "additionalProperties": False,
}


class RedFlagChecker:
    def __init__(self, gateway: LLMGateway, prompt: Prompt) -> None:
        self._gw = gateway
        self._prompt = prompt

    async def check(self, *, conversation: str, reply: str) -> RedFlagOut:
        user = (
            f"{conversation}\n<proposed_reply>\n{neutralise(reply)}\n</proposed_reply>\n"
            "Is this reply safe to send unreviewed?"
        )
        return await self._gw.json(
            prompt=self._prompt, user=user, schema=REDFLAG_SCHEMA, out=RedFlagOut
        )
