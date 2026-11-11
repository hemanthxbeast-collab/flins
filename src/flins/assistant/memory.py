"""What FLINS knows about you (JARVIS phase 2).

Two sources, both with provenance:
- `ProfileMemory`: the facts you wrote in about_me.yaml (read-only).
- `SqlMemory`: things you told FLINS to remember, or agreed it should. Nothing is written
  silently, secrets are refused, and every row keeps how and when it was learned, so FLINS
  can say where a memory came from without inventing it.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

import aiosqlite

from flins.assistant.models import MemoryItem
from flins.core.audit import AuditLog
from flins.core.clock import Clock, from_iso, to_iso
from flins.core.policy import BUILTIN_MONEY, BUILTIN_SENSITIVE
from flins.settings import AboutMe, AssistantConfig

Kind = Literal["semantic", "episodic", "relationship", "task"]
KINDS: tuple[str, ...] = ("semantic", "episodic", "relationship", "task")
MAX_CONTENT = 500

_WORD = re.compile(r"[a-z0-9']{3,}")
_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "you",
        "your",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "not",
        "but",
        "what",
        "when",
        "where",
        "who",
        "why",
        "how",
        "about",
        "from",
        "they",
        "them",
        "his",
        "her",
        "its",
        "our",
        "out",
        "can",
        "will",
        "would",
        "should",
        "could",
        "did",
        "does",
        "remember",
        "know",
        "tell",
        "said",
        "say",
        "any",
        "all",
        "some",
        "more",
        "very",
        "just",
        "also",
        "than",
        "then",
        "there",
        "here",
        "into",
        "been",
        "being",
        "over",
        "under",
        "too",
    }
)
# Never stored: FLINS is not a password manager, and the database is not a vault.
_SECRET = re.compile(
    r"\bpass(?:word|code|phrase)\b|\botp\b|\bcvv\b|\b(?:atm|upi|card|bank)?\s?pin\b(?!\s?code)"
    r"|verification code|security code|\bapi[\s_-]?key\b|\bsecret key\b|\b(?:access|auth) token\b"
    r"|\b(?:\d[ -]?){12,19}\b",
    re.IGNORECASE,
)
_SENSITIVE = re.compile(
    "|".join(f"(?:{p})" for p in (*BUILTIN_SENSITIVE, *BUILTIN_MONEY, r"\bsalary\b", r"\bdebt\b")),
    re.IGNORECASE,
)
_EPISODIC = re.compile(
    r"\b(?:decided|chose|agreed|finished|completed|started|met|visited|bought|submitted|"
    r"yesterday|today|tonight|this (?:morning|evening|week)|last (?:night|week|month|year)|"
    r"on (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d))\b",
    re.IGNORECASE,
)
_RELATION = re.compile(
    r"\bmy (?:friend|best friend|brother|sister|mom|mother|dad|father|wife|husband|partner|"
    r"girlfriend|boyfriend|boss|manager|teacher|professor|roommate|cousin|uncle|aunt|"
    r"classmate|colleague|mentor)\b|\bis my\b|'s birthday\b",
    re.IGNORECASE,
)
_STRONG = re.compile(
    r"\b(?:always|never|prefer|allergic|birthday|deadline|exam|important|must|hate|love|"
    r"favourite|favorite)\b",
    re.IGNORECASE,
)


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def is_secret(text: str) -> bool:
    return bool(_SECRET.search(text))


def guess_kind(text: str) -> Kind:
    if _RELATION.search(text):
        return "relationship"
    if _EPISODIC.search(text):
        return "episodic"
    return "semantic"


def score_importance(text: str, given: float | None = None) -> float:
    """0..1. Things you explicitly asked FLINS to remember start high; lasting preferences and
    dates a bit higher; long rambles a bit lower."""
    value = 0.75 if given is None else max(0.0, min(given, 1.0))
    if _STRONG.search(text):
        value += 0.1
    if len(text) > 300:
        value -= 0.1
    return round(max(0.1, min(value, 0.95)), 2)


class MemoryStore(Protocol):
    async def relevant(self, query: str, limit: int) -> list[MemoryItem]:
        """Things worth putting in front of the model for this message, best first."""
        ...


class NoMemory:
    async def relevant(self, query: str, limit: int) -> list[MemoryItem]:
        return []


class ProfileMemory:
    """Read-only: the facts and ready answers in about_me.yaml. You are the one asking, so
    every fact is available whatever its `share_with` (that setting is about other people)."""

    SOURCE = "about_me.yaml"

    def __init__(self, about: AboutMe) -> None:
        self._items = [f.fact for f in about.facts] + [
            f"If asked {a.question!r}: {a.answer}" for a in about.answers
        ]

    async def relevant(self, query: str, limit: int) -> list[MemoryItem]:
        """All of them if they fit; otherwise the ones sharing most words with the message."""
        asked = tokens(query)
        scored = sorted(
            ((len(asked & tokens(text)), i, text) for i, text in enumerate(self._items)),
            key=lambda p: (-p[0], p[1]),
        )
        if len(scored) <= limit:
            scored.sort(key=lambda p: p[1])  # keep your own order when everything fits
        return [
            MemoryItem(text, self.SOURCE, origin="profile", matched=score > 0)
            for score, _, text in scored[:limit]
        ]


# ---------------------------------------------------------------------- stored memories


@dataclass(frozen=True)
class MemoryRecord:
    id: int
    kind: str
    content: str
    source: str  # told | suggested | dashboard | cli
    channel: str | None
    importance: float
    confidence: float
    privacy: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    uses: int

    def where(self, tz: ZoneInfo) -> str:
        """Where this memory came from, in words. Built from the row, never by a model."""
        day = f"{self.created_at.astimezone(tz):%d %b %Y}".lstrip("0")
        via = {"voice": "by voice", "self_chat": "in your self-chat", "cli": "in the terminal"}
        how = via.get(self.channel or "", "on the dashboard" if self.channel else "")
        if self.source == "suggested":
            lead = "I asked and you agreed"
        elif self.source in ("dashboard", "cli"):
            lead = "you added it"
            how = "on the dashboard" if self.source == "dashboard" else "in the terminal"
        else:
            lead = "you told me"
        return " ".join(x for x in (lead, f"on {day}", how) if x)


@dataclass(frozen=True)
class SaveResult:
    status: Literal["saved", "duplicate", "refused"]
    record: MemoryRecord | None = None
    reason: str | None = None


class SqlMemory:
    def __init__(self, conn: aiosqlite.Connection, tz: ZoneInfo, clock: Clock) -> None:
        self._conn = conn
        self._tz = tz
        self._clock = clock
        self._audit = AuditLog(conn)

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    # ------------------------------------------------------------------ switch

    async def enabled(self) -> bool:
        async with self._conn.execute(
            "SELECT value FROM assistant_state WHERE key = 'memory'"
        ) as cur:
            row = await cur.fetchone()
        return row is None or row[0] != "off"

    async def set_enabled(self, on: bool) -> None:
        await self._conn.execute(
            "INSERT INTO assistant_state (key, value) VALUES ('memory', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("on" if on else "off",),
        )
        await self._conn.commit()
        await self._audit.record("user", "memory.switch", {"on": on})

    # ------------------------------------------------------------------ reading

    @staticmethod
    def _record(r: aiosqlite.Row) -> MemoryRecord:
        return MemoryRecord(
            r["id"],
            r["kind"],
            r["content"],
            r["source"],
            r["channel"],
            float(r["importance"]),
            float(r["confidence"]),
            r["privacy"],
            from_iso(r["created_at"]),
            from_iso(r["updated_at"]),
            from_iso(r["expires_at"]) if r["expires_at"] else None,
            int(r["uses"]),
        )

    async def all(self, now: datetime | None = None) -> list[MemoryRecord]:
        """Newest first. With `now`, memories past their expiry are left out."""
        async with self._conn.execute("SELECT * FROM memories ORDER BY id DESC") as cur:
            rows = [self._record(r) for r in await cur.fetchall()]
        if now is not None:
            rows = [r for r in rows if r.expires_at is None or r.expires_at > now]
        return rows

    async def get(self, memory_id: int) -> MemoryRecord | None:
        async with self._conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)) as cur:
            row = await cur.fetchone()
        return self._record(row) if row else None

    async def count(self) -> int:
        async with self._conn.execute("SELECT COUNT(*) FROM memories") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def search(self, query: str, now: datetime) -> list[MemoryRecord]:
        """Memories that mention the words in `query` (or contain it), best match first."""
        asked = tokens(query)
        phrase = " ".join(query.lower().split())
        hits = []
        for r in await self.all(now):
            overlap = len(asked & tokens(r.content))
            contains = bool(phrase) and phrase in r.content.lower()
            if overlap or contains:
                hits.append((overlap + (2 if contains else 0), r))
        hits.sort(key=lambda p: (-p[0], -p[1].id))
        return [r for _, r in hits]

    async def duplicate_of(self, content: str) -> MemoryRecord | None:
        norm = " ".join(sorted(tokens(content))) or content.lower().strip()
        for r in await self.all():
            other = " ".join(sorted(tokens(r.content))) or r.content.lower().strip()
            if norm == other or difflib.SequenceMatcher(None, norm, other).ratio() > 0.92:
                return r
        return None

    async def relevant(self, query: str, limit: int, *, pinned: int = 6) -> list[MemoryItem]:
        """For the model: memories matching this message, plus your most important standing
        ones (preferences and the like). Sensitive memories only when the message is about
        them. Matched memories get their use counted."""
        if limit <= 0 or not await self.enabled():
            return []
        now = self._clock.now()
        records = await self.all(now)
        asked = tokens(query)
        df: dict[str, int] = {}
        toks = {r.id: tokens(r.content) for r in records}
        for words in toks.values():
            for w in words:
                df[w] = df.get(w, 0) + 1
        scored = []
        for r in records:
            score = sum(1.0 / df[w] for w in asked & toks[r.id])
            if score > 0:
                scored.append((score, r))
        scored.sort(key=lambda p: (-p[0], -p[1].importance, -p[1].id))
        matched = [r for _, r in scored[: max(limit - pinned, limit // 2)]]
        seen = {r.id for r in matched}
        standing = sorted(
            (
                r
                for r in records
                if r.id not in seen
                and r.privacy == "normal"
                and r.importance >= 0.8
                and r.kind in ("semantic", "relationship")
            ),
            key=lambda r: (-r.importance, -r.id),
        )[: max(0, min(pinned, limit - len(matched)))]
        if matched:
            marks = ",".join("?" * len(matched))
            await self._conn.execute(
                f"UPDATE memories SET uses = uses + 1, last_used_at = ? WHERE id IN ({marks})",
                (to_iso(now), *[r.id for r in matched]),
            )
            await self._conn.commit()
        return [
            MemoryItem(r.content, r.where(self._tz), r.kind, "memory", r.id, True)  # type: ignore[arg-type]
            for r in matched
        ] + [
            MemoryItem(r.content, r.where(self._tz), r.kind, "memory", r.id, False)  # type: ignore[arg-type]
            for r in standing
        ]

    # ------------------------------------------------------------------ writing

    async def save(
        self,
        content: str,
        *,
        source: str,
        channel: str | None,
        now: datetime,
        kind: str | None = None,
        importance: float | None = None,
        expires_in: timedelta | None = None,
    ) -> SaveResult:
        content = " ".join(content.split()).strip()
        if len(content) < 3:
            return SaveResult("refused", reason="there's nothing to remember")
        if len(content) > MAX_CONTENT:
            return SaveResult("refused", reason=f"that's too long (limit {MAX_CONTENT} characters)")
        if is_secret(content):
            return SaveResult(
                "refused",
                reason="I don't store passwords, PINs, codes or card numbers. "
                "Keep that in a password manager",
            )
        existing = await self.duplicate_of(content)
        if existing is not None:
            await self._conn.execute(
                "UPDATE memories SET updated_at = ? WHERE id = ?", (to_iso(now), existing.id)
            )
            await self._conn.commit()
            return SaveResult("duplicate", existing)
        kind = kind if kind in KINDS else guess_kind(content)
        value = score_importance(content, importance)
        privacy = "sensitive" if _SENSITIVE.search(content) else "normal"
        cur = await self._conn.execute(
            "INSERT INTO memories (kind, content, source, channel, importance, privacy, "
            "created_at, updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                content,
                source,
                channel,
                value,
                privacy,
                to_iso(now),
                to_iso(now),
                to_iso(now + expires_in) if expires_in else None,
            ),
        )
        await self._conn.commit()
        assert cur.lastrowid is not None
        await self._audit.record(
            "assistant",
            "memory.saved",
            {
                "id": cur.lastrowid,
                "kind": kind,
                "source": source,
                "channel": channel,
                "importance": value,
                "privacy": privacy,
                "chars": len(content),
            },
        )
        record = await self.get(cur.lastrowid)
        return SaveResult("saved", record)

    async def edit(self, memory_id: int, content: str, now: datetime) -> SaveResult:
        """Correct a memory. The same rules as saving apply to the new text."""
        content = " ".join(content.split()).strip()
        if await self.get(memory_id) is None:
            return SaveResult("refused", reason="there's no memory with that number")
        if len(content) < 3 or len(content) > MAX_CONTENT:
            return SaveResult("refused", reason="the new text is empty or too long")
        if is_secret(content):
            return SaveResult("refused", reason="I don't store passwords, PINs, codes or cards")
        privacy = "sensitive" if _SENSITIVE.search(content) else "normal"
        await self._conn.execute(
            "UPDATE memories SET content = ?, privacy = ?, updated_at = ? WHERE id = ?",
            (content, privacy, to_iso(now), memory_id),
        )
        await self._conn.commit()
        await self._audit.record("user", "memory.edited", {"id": memory_id, "chars": len(content)})
        return SaveResult("saved", await self.get(memory_id))

    async def forget(self, ids: list[int], *, via: str) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        cur = await self._conn.execute(f"DELETE FROM memories WHERE id IN ({marks})", ids)
        await self._conn.commit()
        await self._audit.record("user", "memory.forgotten", {"ids": ids, "via": via})
        return cur.rowcount

    async def forget_all(self, *, via: str) -> int:
        cur = await self._conn.execute("DELETE FROM memories")
        await self._conn.commit()
        await self._audit.record("user", "memory.cleared", {"count": cur.rowcount, "via": via})
        return cur.rowcount

    async def expire(self, now: datetime) -> int:
        cur = await self._conn.execute(
            "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?", (to_iso(now),)
        )
        await self._conn.commit()
        return cur.rowcount


class CombinedMemory:
    """What the conversation model gets: your about_me facts and your stored memories."""

    def __init__(
        self, profile: ProfileMemory | None, stored: SqlMemory | None, cfg: AssistantConfig
    ) -> None:
        self._profile = profile
        self._stored = stored
        self._cfg = cfg

    async def relevant(self, query: str, limit: int) -> list[MemoryItem]:
        out: list[MemoryItem] = []
        m = self._cfg.memory
        if self._stored is not None and m.enabled:
            out += await self._stored.relevant(query, m.max_in_context, pinned=m.pinned)
        if self._profile is not None and self._cfg.use_about_me and self._cfg.max_facts:
            out += await self._profile.relevant(query, self._cfg.max_facts)
        return out
