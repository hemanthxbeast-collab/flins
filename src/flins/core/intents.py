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

    async def parse(self, text: str, names: list[str] | None = None) -> Intent:
        user = f"<command>\n{neutralise(text)}\n</command>"
        if names:
            # Names only, never numbers: lets the model map "roul" -> "Rahul".
            user += "\n<contacts>\n" + ", ".join(neutralise(n) for n in names) + "\n</contacts>"
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


_SOUNDS = (
    ("ph", "f"),
    ("sh", "s"),
    ("th", "t"),
    ("dh", "d"),
    ("kh", "k"),
    ("bh", "b"),
    ("gh", "g"),
    ("ck", "k"),
    ("q", "k"),
    ("z", "j"),
    ("w", "v"),
    ("ee", "i"),
    ("oo", "u"),
)


def sound_key(name: str) -> str:
    """A rough 'sounds like' key for Indian names as speech-to-text spells them:
    'Rahul' / 'Roul' -> 'rl', 'Radhika' / 'Radika' -> 'ysd'."""
    s = "".join(ch for ch in name.lower() if ch.isalpha())
    for a, b in _SOUNDS:
        s = s.replace(a, b)
    s = s.replace("h", "")
    if not s:
        return ""
    rest = "".join(ch for ch in s[1:] if ch not in "aeiouy")
    out = s[0]
    for ch in rest:
        if ch != out[-1]:
            out += ch
    return out


async def contact_names(conn: aiosqlite.Connection, limit: int = 30) -> list[str]:
    """Names you're most likely to say: close contacts and recent chats, most recent first."""
    async with conn.execute(
        """
        SELECT COALESCE(c.display_name, c.push_name) AS name,
               MAX(COALESCE(m.ts, '')) AS last
        FROM contacts c
        LEFT JOIN messages m ON m.chat_jid = c.wa_jid AND m.ts >= datetime('now', '-30 days')
        WHERE c.tier != 'blocked' AND COALESCE(c.display_name, c.push_name) IS NOT NULL
          AND (c.tier IN ('inner', 'trusted') OR m.id IS NOT NULL)
        GROUP BY c.id
        ORDER BY last DESC
        LIMIT ?
        """,
        (limit,),
    ) as cur:
        return [str(r[0]) for r in await cur.fetchall()]


async def find_contacts(conn: aiosqlite.Connection, spoken: str) -> list[ContactMatch]:
    """Contacts whose saved or self-chosen name (or phone number) matches what was said."""
    digits = "".join(ch for ch in spoken if ch.isdigit())
    if len(digits) >= 7:
        async with conn.execute(
            "SELECT wa_jid, COALESCE(display_name, push_name, wa_jid) FROM contacts "
            "WHERE tier != 'blocked' AND wa_jid LIKE ?",
            (f"%{digits[-10:]}@%",),
        ) as cur:
            return [ContactMatch(r[0], str(r[1])) for r in await cur.fetchall()]
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
    if scored:
        return [m for _, m in scored[:3]]
    # Last resort: names that *sound* the same ("roul" -> "Rahul").
    key = sound_key(want.split()[0])
    if len(key) < 2:
        return []
    return [ContactMatch(j, n) for j, n in rows if key in (sound_key(n.split()[0]), sound_key(n))][
        :3
    ]
