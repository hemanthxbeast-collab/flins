"""Short-term conversation memory: sessions and turns in SQLite.

A session is a run of turns with no gap longer than `idle`. The model sees the last few turns
plus a rolling summary of the older ones, never the whole history. Turns are deleted after
`keep_days`, and `clear()` removes everything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import aiosqlite

from flins.assistant.models import Turn
from flins.core.clock import from_iso, to_iso


@dataclass(frozen=True)
class Session:
    id: int
    started_at: datetime
    last_at: datetime
    summary: str | None
    summary_upto: int
    earlier: str | None = None  # the previous session's summary, when this one just began


class ConversationStore:
    def __init__(self, conn: aiosqlite.Connection, *, idle: timedelta, keep: timedelta) -> None:
        self._conn = conn
        self._idle = idle
        self._keep = keep

    async def _latest(self) -> aiosqlite.Row | None:
        async with self._conn.execute(
            "SELECT * FROM conversation_sessions ORDER BY id DESC LIMIT 1"
        ) as cur:
            return await cur.fetchone()

    async def session(self, now: datetime) -> Session:
        """The current session, or a new one if you've been quiet for longer than `idle`."""
        row = await self._latest()
        if row is not None and now - from_iso(row["last_at"]) <= self._idle:
            return Session(
                row["id"],
                from_iso(row["started_at"]),
                from_iso(row["last_at"]),
                row["summary"],
                row["summary_upto"],
            )
        await self.prune(now)
        cur = await self._conn.execute(
            "INSERT INTO conversation_sessions (started_at, last_at) VALUES (?, ?)",
            (to_iso(now), to_iso(now)),
        )
        await self._conn.commit()
        assert cur.lastrowid is not None
        earlier = row["summary"] if row is not None else None
        return Session(cur.lastrowid, now, now, None, 0, earlier)

    async def add(
        self,
        session: Session,
        *,
        role: str,
        text: str,
        source: str,
        now: datetime,
        route: str | None = None,
    ) -> int:
        cur = await self._conn.execute(
            "INSERT INTO conversation_turns (session_id, ts, source, role, route, text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session.id, to_iso(now), source, role, route, text),
        )
        await self._conn.execute(
            "UPDATE conversation_sessions SET last_at = ? WHERE id = ?", (to_iso(now), session.id)
        )
        await self._conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    @staticmethod
    def _turn(r: aiosqlite.Row) -> Turn:
        return Turn(r["id"], from_iso(r["ts"]), r["source"], r["role"], r["route"], r["text"])

    async def recent(self, session: Session, limit: int) -> list[Turn]:
        """The last `limit` turns that the summary doesn't cover yet, oldest first."""
        async with self._conn.execute(
            "SELECT * FROM conversation_turns WHERE session_id = ? AND id > ? "
            "ORDER BY id DESC LIMIT ?",
            (session.id, session.summary_upto, limit),
        ) as cur:
            rows = list(await cur.fetchall())
        return [self._turn(r) for r in reversed(rows)]

    async def unsummarised(self, session: Session) -> list[Turn]:
        async with self._conn.execute(
            "SELECT * FROM conversation_turns WHERE session_id = ? AND id > ? ORDER BY id",
            (session.id, session.summary_upto),
        ) as cur:
            return [self._turn(r) for r in await cur.fetchall()]

    async def set_summary(self, session: Session, summary: str, upto: int) -> None:
        await self._conn.execute(
            "UPDATE conversation_sessions SET summary = ?, summary_upto = ? WHERE id = ?",
            (summary, upto, session.id),
        )
        await self._conn.commit()

    async def last_reply_at(self, source: str) -> datetime | None:
        """When FLINS last *talked* on this channel (keeps a self-chat conversation open).
        Command answers don't count: after "working mood till 6", your next line is a note."""
        async with self._conn.execute(
            "SELECT ts FROM conversation_turns WHERE source = ? AND role = 'assistant' "
            "AND route IN ('chat', 'error', 'memory') ORDER BY id DESC LIMIT 1",
            (source,),
        ) as cur:
            row = await cur.fetchone()
        return from_iso(row[0]) if row else None

    async def history(self, limit: int = 50) -> list[Turn]:
        """Latest turns across sessions, oldest first (for the dashboard and `flins chat`)."""
        async with self._conn.execute(
            "SELECT * FROM conversation_turns ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = list(await cur.fetchall())
        return [self._turn(r) for r in reversed(rows)]

    async def prune(self, now: datetime) -> int:
        """Delete turns (and empty sessions) older than `keep`."""
        cutoff = to_iso(now - self._keep)
        cur = await self._conn.execute("DELETE FROM conversation_turns WHERE ts < ?", (cutoff,))
        await self._conn.execute(
            "DELETE FROM conversation_sessions WHERE last_at < ? AND id NOT IN "
            "(SELECT DISTINCT session_id FROM conversation_turns)",
            (cutoff,),
        )
        await self._conn.commit()
        return cur.rowcount

    async def clear(self) -> int:
        """Forget every conversation. Returns how many turns were deleted."""
        cur = await self._conn.execute("DELETE FROM conversation_turns")
        await self._conn.execute("DELETE FROM conversation_sessions")
        await self._conn.commit()
        return cur.rowcount
