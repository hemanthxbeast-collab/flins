"""Tasks and reminders (JARVIS phase 3a). A reminder is a task with `remind_at` set.

Reminders are notes to you only: nothing here can message a contact. Every change is audited
by task id, without the title.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import aiosqlite

from flins.assistant.memory import tokens
from flins.assistant.timeparse import describe, next_occurrence
from flins.core.audit import AuditLog
from flins.core.clock import from_iso, to_iso

MAX_TITLE = 200
PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    priority: str
    status: str
    due_at: datetime | None
    remind_at: datetime | None
    repeat: str
    created_at: datetime
    completed_at: datetime | None
    last_reminded_at: datetime | None

    @property
    def when(self) -> datetime | None:
        """The time that matters for ordering: the next reminder, else the due time."""
        return self.remind_at or self.due_at

    def line(self, now: datetime, tz: ZoneInfo) -> str:
        """'call the bank (today 19:00, every day) [high]'"""
        bits = []
        if self.when is not None:
            late = " overdue" if self.when < now and self.status == "open" else ""
            bits.append(describe(self.when, now, tz) + late)
        if self.repeat != "none":
            bits.append(
                {"daily": "every day", "weekdays": "weekdays", "weekly": "weekly"}[self.repeat]
            )
        extra = f" ({', '.join(bits)})" if bits else ""
        flag = " [high]" if self.priority == "high" else ""
        return f"{self.title}{extra}{flag}"


class TaskStore:
    def __init__(self, conn: aiosqlite.Connection, tz: ZoneInfo) -> None:
        self._conn = conn
        self._tz = tz
        self._audit = AuditLog(conn)

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    @staticmethod
    def _task(r: aiosqlite.Row) -> Task:
        def dt(v: str | None) -> datetime | None:
            return from_iso(v) if v else None

        return Task(
            r["id"],
            r["title"],
            r["priority"],
            r["status"],
            dt(r["due_at"]),
            dt(r["remind_at"]),
            r["repeat"],
            from_iso(r["created_at"]),
            dt(r["completed_at"]),
            dt(r["last_reminded_at"]),
        )

    async def get(self, task_id: int) -> Task | None:
        async with self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
            row = await cur.fetchone()
        return self._task(row) if row else None

    async def add(
        self,
        title: str,
        *,
        source: str,
        channel: str | None,
        now: datetime,
        at: datetime | None = None,
        repeat: str = "none",
        priority: str = "normal",
        remind: bool = True,
    ) -> Task:
        """`at` is when it's due; with `remind` FLINS also reminds you then."""
        title = " ".join(title.split())[:MAX_TITLE]
        if not title:
            raise ValueError("a task needs a title")
        cur = await self._conn.execute(
            "INSERT INTO tasks (title, priority, due_at, remind_at, repeat, source, channel, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                title,
                priority if priority in PRIORITY_RANK else "normal",
                to_iso(at) if at else None,
                to_iso(at) if at and remind else None,
                repeat if at else "none",
                source,
                channel,
                to_iso(now),
                to_iso(now),
            ),
        )
        await self._conn.commit()
        assert cur.lastrowid is not None
        await self._audit.record(
            "assistant",
            "task.added",
            {"id": cur.lastrowid, "source": source, "channel": channel, "reminder": bool(at)},
        )
        task = await self.get(cur.lastrowid)
        assert task is not None
        return task

    async def open(self) -> list[Task]:
        """Open tasks: timed ones soonest first, then the rest by priority and age."""
        async with self._conn.execute("SELECT * FROM tasks WHERE status = 'open'") as cur:
            tasks = [self._task(r) for r in await cur.fetchall()]
        far = datetime.max.replace(tzinfo=self._tz)
        return sorted(tasks, key=lambda t: (t.when or far, PRIORITY_RANK[t.priority], t.id))

    async def recently_done(self, limit: int = 10) -> list[Task]:
        async with self._conn.execute(
            "SELECT * FROM tasks WHERE status = 'done' ORDER BY completed_at DESC LIMIT ?", (limit,)
        ) as cur:
            return [self._task(r) for r in await cur.fetchall()]

    async def find(self, words: str) -> list[Task]:
        """Open tasks matching `words` (a task number, the title, or most of its words)."""
        q = words.strip().strip(".!?\"'").lower()
        tasks = await self.open()
        if q.lstrip("#").isdigit():
            return [t for t in tasks if t.id == int(q.lstrip("#"))]
        asked = tokens(q)
        hits = []
        for t in tasks:
            title = t.title.lower()
            overlap = len(asked & tokens(title))
            if q and (q in title or title in q):
                hits.append((100, t))
            elif asked and overlap >= max(1, round(0.6 * len(asked))):
                hits.append((overlap, t))
        hits.sort(key=lambda p: (-p[0], p[1].id))
        if hits and hits[0][0] == 100:
            return [t for score, t in hits if score == 100]
        return [t for _, t in hits]

    async def latest(self) -> Task | None:
        async with self._conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 1") as cur:
            row = await cur.fetchone()
        return self._task(row) if row else None

    async def last_reminded(self) -> Task | None:
        async with self._conn.execute(
            "SELECT * FROM tasks WHERE status = 'open' AND last_reminded_at IS NOT NULL "
            "ORDER BY last_reminded_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return self._task(row) if row else None

    async def complete(self, task: Task, now: datetime) -> Task:
        """Done. A repeating task stays open and moves to its next time."""
        nxt = next_occurrence(task.when, task.repeat, now) if task.when else None
        if nxt is not None:
            await self._conn.execute(
                "UPDATE tasks SET due_at = ?, remind_at = ?, updated_at = ? WHERE id = ?",
                (to_iso(nxt), to_iso(nxt), to_iso(now), task.id),
            )
        else:
            await self._conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ?, remind_at = NULL, "
                "updated_at = ? WHERE id = ?",
                (to_iso(now), to_iso(now), task.id),
            )
        await self._conn.commit()
        await self._audit.record("user", "task.done", {"id": task.id, "repeats": nxt is not None})
        done = await self.get(task.id)
        assert done is not None
        return done

    async def cancel(self, task: Task, now: datetime, *, via: str) -> None:
        await self._conn.execute(
            "UPDATE tasks SET status = 'cancelled', remind_at = NULL, updated_at = ? WHERE id = ?",
            (to_iso(now), task.id),
        )
        await self._conn.commit()
        await self._audit.record("user", "task.cancelled", {"id": task.id, "via": via})

    async def snooze(self, task: Task, until: datetime, now: datetime) -> None:
        await self._conn.execute(
            "UPDATE tasks SET remind_at = ?, updated_at = ? WHERE id = ?",
            (to_iso(until), to_iso(now), task.id),
        )
        await self._conn.commit()
        await self._audit.record("user", "task.snoozed", {"id": task.id})

    async def due(self, now: datetime) -> list[Task]:
        """Open tasks whose reminder time has come."""
        async with self._conn.execute(
            "SELECT * FROM tasks WHERE status = 'open' AND remind_at IS NOT NULL "
            "AND remind_at <= ? ORDER BY remind_at, id",
            (to_iso(now),),
        ) as cur:
            return [self._task(r) for r in await cur.fetchall()]

    async def mark_reminded(self, task: Task, now: datetime) -> None:
        """Called once the reminder was delivered: it never fires twice for the same time. A
        repeating one moves to its next time; a one-off stays open until you say it's done."""
        nxt = next_occurrence(task.remind_at, task.repeat, now) if task.remind_at else None
        await self._conn.execute(
            "UPDATE tasks SET remind_at = ?, due_at = COALESCE(?, due_at), last_reminded_at = ?, "
            "updated_at = ? WHERE id = ?",
            (
                to_iso(nxt) if nxt else None,
                to_iso(nxt) if nxt else None,
                to_iso(now),
                to_iso(now),
                task.id,
            ),
        )
        await self._conn.commit()
        await self._audit.record("assistant", "task.reminded", {"id": task.id})
