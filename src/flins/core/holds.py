"""Hold: pause FLINS's replies to one person for a while (JARVIS phase 6b).

While someone is on hold FLINS sends them nothing, not even the holding reply: their messages
wait in your summary, and an urgent one still alerts you. In `decide()` a hold is one more
ceiling (NOTIFY), so it can only lower what FLINS does. A hold always has an end time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import aiosqlite

from flins.core.clock import from_iso, to_iso

MAX_HOLD = timedelta(hours=12)
DEFAULT_HOLD = timedelta(hours=4)

_NAME = r"(?P<name>.+?)"
_STRICT = [
    re.compile(p)
    for p in (
        rf"hold {_NAME}['’]s (?:messages?|chat|texts?|replies)",
        rf"hold (?:all )?(?:replies|messages?|texts?) (?:to|from|for) {_NAME}",
        rf"(?:don'?t|do not) (?:reply|respond) to {_NAME}",
        rf"put {_NAME} on hold",
    )
]
_LOOSE = [re.compile(rf"hold {_NAME}")]  # "hold the door" is not a command: needs a contact
_RELEASE = [
    re.compile(p)
    for p in (
        rf"(?:release|unhold|un-hold) (?:the )?hold on {_NAME}",
        rf"(?:release|unhold|un-hold) {_NAME}(?:['’]s (?:messages?|chat|texts?|replies))?",
        rf"stop holding {_NAME}(?:['’]s (?:messages?|chat|texts?|replies))?",
        rf"take {_NAME} off hold",
    )
]
_LIST = re.compile(
    r"holds|list holds|show holds|who(?:'s| is|se) on hold|what(?:'s| is) on hold|"
    r"is (?:anyone|anybody) on hold"
)
_EVERYONE = {"all", "everyone", "everybody", "all holds", "them all"}
_TAIL_START = re.compile(
    r"\s+(?=(?:till|until|til|untill|upto|up to|for|by)\s+\S)"
    r"|\s+(?=\d+\s*(?:h|hr|hrs|hours?|m|min|mins|minutes?)\b)"
)


class HoldError(ValueError):
    pass


@dataclass(frozen=True)
class Hold:
    jid: str
    name: str
    until: datetime


@dataclass(frozen=True)
class HoldCommand:
    op: Literal["hold", "release", "release_all", "list"]
    name: str = ""
    time: str | None = None  # "till 6pm", "for 2 hours", or None
    strict: bool = True  # False: plain "hold X", only a command if X is a contact


def parse_hold(text: str) -> HoldCommand | None:
    """`text` is already lower-cased with "FLINS," and trailing politeness removed."""
    t = text.strip()
    if _LIST.fullmatch(t):
        return HoldCommand("list")
    for rx in _RELEASE:
        m = rx.fullmatch(t)
        if m:
            name = m.group("name").strip()
            return HoldCommand("release_all" if name in _EVERYONE else "release", name)
    # "hold rahul till 6": the name is what comes before the time.
    # Earliest split first ("hold rahul" + "for 2 hours"), the whole text last.
    heads: list[tuple[str, str | None]] = [
        (t[: m.start()], t[m.end() :]) for m in _TAIL_START.finditer(t)
    ]
    heads.append((t, None))
    for strict, patterns in ((True, _STRICT), (False, _LOOSE)):
        for head, time in heads:
            for rx in patterns:
                m = rx.fullmatch(head)
                if m and m.group("name").strip():
                    return HoldCommand("hold", m.group("name").strip(), time, strict)
    return None


class HoldStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def set(self, jid: str, until: datetime, *, source: str, now: datetime) -> None:
        if until <= now:
            raise HoldError("That time has already passed.")
        if until - now > MAX_HOLD:
            hours = int(MAX_HOLD.total_seconds() // 3600)
            raise HoldError(f"A hold can last at most {hours} hours.")
        await self._conn.execute(
            "INSERT INTO contact_holds (wa_jid, until, source, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (wa_jid) DO UPDATE SET until = excluded.until, source = excluded.source, "
            "created_at = excluded.created_at",
            (jid, to_iso(until), source, to_iso(now)),
        )
        await self._conn.commit()

    async def release(self, jid: str) -> bool:
        cur = await self._conn.execute("DELETE FROM contact_holds WHERE wa_jid = ?", (jid,))
        await self._conn.commit()
        return bool(cur.rowcount)

    async def release_all(self) -> int:
        cur = await self._conn.execute("DELETE FROM contact_holds")
        await self._conn.commit()
        return int(cur.rowcount or 0)

    async def active(self, jid: str, now: datetime) -> datetime | None:
        """When the hold on this contact ends, or None if there is none (or it has ended)."""
        async with self._conn.execute(
            "SELECT until FROM contact_holds WHERE wa_jid = ?", (jid,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        until = from_iso(row[0])
        return until if until > now else None

    async def all(self, now: datetime) -> list[Hold]:
        async with self._conn.execute(
            "SELECT h.wa_jid, COALESCE(c.display_name, c.push_name, h.wa_jid), h.until "
            "FROM contact_holds h LEFT JOIN contacts c ON c.wa_jid = h.wa_jid ORDER BY h.until"
        ) as cur:
            rows = await cur.fetchall()
        holds = [Hold(str(r[0]), str(r[1]), from_iso(r[2])) for r in rows]
        return [h for h in holds if h.until > now]
