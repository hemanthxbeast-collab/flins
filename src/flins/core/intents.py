"""Tier-B commands (SPEC §4.1): free-form text -> a structured intent, via the LLM.

Tier A (fixed phrases, core/grammar.py) is always tried first and needs no AI. Tier B only runs
when Tier A finds nothing. CommandRouter asks you to confirm anything Tier B would *do*.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from flins.llm.context import neutralise
from flins.llm.gateway import LLMGateway
from flins.llm.prompts import Prompt

Action = Literal[
    "mode",
    "extend",
    "done",
    "stop",
    "resume",
    "status",
    "pending",
    "summary",
    "approve",
    "reject",
    "edit",
    "send",
    "unknown",
]


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Action
    mode: Literal["working", "focus", "away"] | None = None
    duration_or_time: str | None = None
    number: int | None = Field(default=None, ge=1, le=999)
    contact: str | None = None
    text: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)


def _nullable(kind: str) -> dict[str, Any]:
    return {"type": [kind, "null"]}


INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(Action.__args__)},  # type: ignore[attr-defined]
        "mode": {"type": ["string", "null"], "enum": ["working", "focus", "away", None]},
        "duration_or_time": _nullable("string"),
        "number": _nullable("integer"),
        "contact": _nullable("string"),
        "text": _nullable("string"),
        "confidence": {"type": "number"},
    },
    "required": ["action", "mode", "duration_or_time", "number", "contact", "text", "confidence"],
    "additionalProperties": False,
}

MIN_CONFIDENCE = 0.5


class IntentParser:
    def __init__(self, gateway: LLMGateway, prompt: Prompt) -> None:
        self._gw = gateway
        self._prompt = prompt

    async def parse(self, text: str) -> Intent:
        user = f"<command>\n{neutralise(text)}\n</command>"
        intent = await self._gw.json(
            prompt=self._prompt, user=user, schema=INTENT_SCHEMA, out=Intent
        )
        if intent.confidence < MIN_CONFIDENCE:
            return Intent(action="unknown", confidence=intent.confidence)
        return intent


@dataclass(frozen=True)
class ContactMatch:
    jid: str
    name: str


async def find_contacts(conn: aiosqlite.Connection, spoken: str) -> list[ContactMatch]:
    """Contacts whose saved or self-chosen name matches what was said. Best first."""
    async with conn.execute(
        "SELECT wa_jid, COALESCE(display_name, push_name) AS name FROM contacts "
        "WHERE tier != 'blocked' AND COALESCE(display_name, push_name) IS NOT NULL"
    ) as cur:
        rows = [(r[0], str(r[1])) for r in await cur.fetchall()]
    want = spoken.strip().lower()
    if not want:
        return []
    exact = [ContactMatch(j, n) for j, n in rows if n.lower() == want]
    if exact:
        return exact
    starts = [ContactMatch(j, n) for j, n in rows if n.lower().split()[0] == want.split()[0]]
    if starts:
        return starts
    contains = [ContactMatch(j, n) for j, n in rows if want in n.lower()]
    if contains:
        return contains

    def squash(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum())

    # Compare without spaces, against the full name and the first name, allowing small
    # misspellings ("Radhi Ga" / "Radhiga" -> "Radhika").
    target = squash(want)
    scored = []
    for j, n in rows:
        full, first = squash(n), squash(n.split()[0])
        ratio = max(
            difflib.SequenceMatcher(None, target, full).ratio(),
            difflib.SequenceMatcher(None, target, first).ratio(),
        )
        if ratio >= 0.72:
            scored.append((ratio, ContactMatch(j, n)))
    scored.sort(key=lambda x: -x[0])
    if len(scored) > 1 and scored[0][0] - scored[1][0] >= 0.15:
        return [scored[0][1]]  # a clear winner
    return [m for _, m in scored[:3]]
