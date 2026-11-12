"""FLINS speaks first, a little (JARVIS phase 7, docs/milestones/J7.md).

A morning briefing, an evening summary and a heads-up before a deadline. All of it goes to
you only (self-chat note, and a spoken line when allowed), is built by code from your own
tasks and from counts, and is bounded: off by default, a daily cap, quiet hours, never while
paused, at most once each.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

import aiosqlite

from flins.assistant.tasks import Task, TaskStore
from flins.core.audit import AuditLog
from flins.core.clock import to_iso
from flins.core.grammar import strip_address
from flins.core.levels import Mode
from flins.settings import ProactiveConfig

Kind = Literal["morning", "evening", "nudge"]
Outcome = Literal["sent", "empty", "capped", "late"]
Note = Callable[[str], Awaitable[object]]
Say = Callable[[str], Awaitable[object]]

LATE_AFTER = timedelta(hours=3)  # a briefing this late is skipped, not sent
MAX_LINES = 6
SPEAKING_MODES = (Mode.NORMAL, Mode.WORKING)

_MORNING = re.compile(
    r"^(?:(?:give me |show me |what(?:'s| is) )?(?:my |the )?(?:morning |daily )?briefing|"
    r"brief me|(?:how|what) does my day look(?: like)?|what(?:'s| is) my day(?: like)?|"
    r"what(?:'s| is) (?:on|planned|up) (?:for )?today|what do i have today)[\s.?!]*$",
    re.IGNORECASE,
)
_EVENING = re.compile(
    r"^(?:(?:give me |show me )?(?:my |the |an? )?(?:evening|end[- ]of[- ]day|day'?s?) "
    r"(?:summary|wrap[- ]?up|review)|wrap up my day|how did my day go|"
    r"what did i (?:get )?do(?:ne)? today)[\s.?!]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Brief:
    text: str  # the note for your self-chat
    spoken: str  # the short version for voice


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _bullets(tasks: list[Task], now: datetime, tz: ZoneInfo) -> list[str]:
    lines = [f"• {t.line(now, tz)}" for t in tasks[:MAX_LINES]]
    if len(tasks) > MAX_LINES:
        lines.append(f"• … and {len(tasks) - MAX_LINES} more")
    return lines


class Proactive:
    def __init__(
        self,
        conn: aiosqlite.Connection,
        tasks: TaskStore,
        cfg: ProactiveConfig,
        *,
        pending_drafts: Callable[[datetime], Awaitable[int]] | None = None,
        weather: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        self._conn = conn
        self._tasks = tasks
        self._cfg = cfg
        self._tz = tasks.tz
        self._audit = AuditLog(conn)
        self._pending = pending_drafts
        self._weather = weather

    # ------------------------------------------------------------------ what to say

    def _day(self, now: datetime, offset: int = 0) -> tuple[datetime, datetime]:
        local = now.astimezone(self._tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset)
        return start, start + timedelta(days=1)

    async def _drafts(self, now: datetime) -> int:
        if self._pending is None:
            return 0
        try:
            return int(await self._pending(now))
        except Exception:  # a count is a bonus; never fail a briefing over it
            return 0

    async def briefing(self, kind: Literal["morning", "evening"], now: datetime) -> Brief | None:
        """The text for now, or None when there is nothing worth saying."""
        return await (self._morning(now) if kind == "morning" else self._evening(now))

    async def _morning(self, now: datetime) -> Brief | None:
        _, end = self._day(now)
        open_tasks = await self._tasks.open()
        overdue = [t for t in open_tasks if t.when is not None and t.when < now]
        today = [t for t in open_tasks if t.when is not None and now <= t.when < end]
        undated = [t for t in open_tasks if t.when is None]
        drafts = await self._drafts(now)
        if not (overdue or today or drafts):
            return None
        local = now.astimezone(self._tz)
        lines = [f"Good morning. {local:%A} {local.day} {local:%B}."]
        if today:
            lines += [f"Today: {_plural(len(today), 'thing')}.", *_bullets(today, now, self._tz)]
        else:
            lines.append("Nothing is scheduled for today.")
        if overdue:
            lines += [f"Overdue: {len(overdue)}.", *_bullets(overdue, now, self._tz)]
        if undated:
            lines.append(f"{_plural(len(undated), 'other open task')} without a date.")
        if drafts:
            waiting = "is" if drafts == 1 else "are"
            lines.append(f"{_plural(drafts, 'reply draft')} {waiting} waiting for your approval.")
        if self._weather is not None:
            try:
                weather = await self._weather()
            except Exception:
                weather = None
            if weather:
                lines.append(f"Weather: {weather}")
        said = [f"Good morning. {_plural(len(today), 'thing')} today"]
        if overdue:
            said.append(f"{len(overdue)} overdue")
        if drafts:
            said.append(f"{_plural(drafts, 'draft')} waiting")
        return Brief("\n".join(lines), ", ".join(said) + ". The list is in your self-chat.")

    async def _evening(self, now: datetime) -> Brief | None:
        start, end = self._day(now)
        done = await self._tasks.done_between(start, end)
        open_tasks = await self._tasks.open()
        left = [t for t in open_tasks if t.when is not None and t.when < end]
        tomorrow = [
            t for t in open_tasks if t.when is not None and end <= t.when < end + (end - start)
        ]
        counts = await self._message_counts(start, end)
        drafts = await self._drafts(now)
        if not (done or left or tomorrow or drafts or sum(counts.values())):
            return None
        lines = ["End of day."]
        if done:
            lines += [f"Done today: {len(done)}.", *[f"• {t.title}" for t in done[:MAX_LINES]]]
            if len(done) > MAX_LINES:
                lines.append(f"• … and {len(done) - MAX_LINES} more")
        else:
            lines.append("Nothing was marked done today.")
        if left:
            lines += [f"Still open: {len(left)}.", *_bullets(left, now, self._tz)]
        if tomorrow:
            lines += [f"Tomorrow: {len(tomorrow)}.", *_bullets(tomorrow, now, self._tz)]
        total = sum(counts.values())
        if total:
            answered = counts.get("auto", 0) + counts.get("auto_safe", 0)
            lines.append(
                f"Messages today: {total} ({answered} answered by FLINS, "
                f"{counts.get('draft', 0)} drafted for you, "
                f"{counts.get('notify', 0)} shown to you, {counts.get('ignore', 0)} ignored)."
            )
        if drafts:
            lines.append(f"{_plural(drafts, 'reply draft')} still waiting for your approval.")
        said = (
            f"End of day. {len(done)} done, {len(left)} still open, {len(tomorrow)} for tomorrow."
        )
        return Brief("\n".join(lines), said + " The summary is in your self-chat.")

    async def _message_counts(self, start: datetime, end: datetime) -> dict[str, int]:
        async with self._conn.execute(
            "SELECT action, COUNT(*) FROM decisions WHERE created_at >= ? AND created_at < ? "
            "GROUP BY action",
            (to_iso(start), to_iso(end)),
        ) as cur:
            return {str(r[0]): int(r[1]) for r in await cur.fetchall()}

    # ------------------------------------------------------------------ on demand

    async def handle(self, text: str, source: str, now: datetime, *, addressed: bool) -> str | None:
        """ "brief me" / "evening summary". You asked, so no cap and no quiet hours."""
        if not addressed:
            return None
        said = strip_address(text)
        kind: Literal["morning", "evening"]
        if _MORNING.match(said):
            kind = "morning"
        elif _EVENING.match(said):
            kind = "evening"
        else:
            return None
        brief = await self.briefing(kind, now)
        await self._audit.record(
            "assistant", "assistant.proactive", {"kind": kind, "outcome": "asked", "source": source}
        )
        if brief is None:
            if kind == "morning":
                return "Nothing is scheduled today, nothing is overdue and no drafts are waiting."
            return "Nothing to report for today."
        return brief.text

    # ------------------------------------------------------------------ on its own

    def _quiet(self, now: datetime) -> bool:
        if not self._cfg.quiet_hours:
            return False
        start, end = (parse_hhmm(x) for x in self._cfg.quiet_hours)
        t = now.astimezone(self._tz).time()
        return start <= t < end if start <= end else (t >= start or t < end)

    async def _seen(self, key: str) -> bool:
        async with self._conn.execute("SELECT 1 FROM proactive_log WHERE key = ?", (key,)) as cur:
            return await cur.fetchone() is not None

    async def _sent_today(self, now: datetime) -> int:
        start, _ = self._day(now)
        async with self._conn.execute(
            "SELECT COUNT(*) FROM proactive_log WHERE outcome = 'sent' AND at >= ?",
            (to_iso(start),),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def _log(
        self, kind: Kind, key: str, outcome: Outcome, now: datetime, spoken: bool
    ) -> bool:
        """Record it. False if this key was already recorded (someone else got there first)."""
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO proactive_log (kind, key, outcome, spoken, at) "
            "VALUES (?, ?, ?, ?, ?)",
            (kind, key, outcome, int(spoken), to_iso(now)),
        )
        await self._conn.commit()
        if not cur.rowcount:
            return False
        await self._audit.record(
            "assistant", "assistant.proactive", {"kind": kind, "outcome": outcome, "spoken": spoken}
        )
        return True

    async def _deliver(
        self,
        kind: Kind,
        key: str,
        brief: Brief,
        now: datetime,
        mode: Mode,
        note: Note,
        say: Say | None,
    ) -> str | None:
        if await self._sent_today(now) >= self._cfg.max_per_day:
            await self._log(kind, key, "capped", now, False)
            return None
        speak = (
            say is not None and self._cfg.speak and mode in SPEAKING_MODES and not self._quiet(now)
        )
        # Logged first: whatever goes wrong after this, it is never sent twice.
        if not await self._log(kind, key, "sent", now, speak):
            return None
        await note(brief.text)
        if speak and say is not None:
            with contextlib.suppress(Exception):  # the written note already went out
                await say(brief.spoken)
        return brief.text

    async def tick(self, now: datetime, *, mode: Mode, note: Note, say: Say | None) -> list[str]:
        """The scheduler step. Returns what was written to you."""
        cfg = self._cfg
        if not cfg.enabled or mode == Mode.PAUSED:
            return []
        out: list[str] = []
        local = now.astimezone(self._tz)
        scheduled: tuple[tuple[Literal["morning", "evening"], str | None], ...] = (
            ("morning", cfg.morning),
            ("evening", cfg.evening),
        )
        for kind, at in scheduled:
            if at is None:
                continue
            due = datetime.combine(local.date(), parse_hhmm(at), tzinfo=self._tz)
            key = f"{kind}:{local.date().isoformat()}"
            if now < due or await self._seen(key):
                continue
            if now - due > LATE_AFTER:
                await self._log(kind, key, "late", now, False)
                continue
            brief = await self.briefing(kind, now)
            if brief is None:
                await self._log(kind, key, "empty", now, False)
                continue
            sent = await self._deliver(kind, key, brief, now, mode, note, say)
            if sent:
                out.append(sent)
        if cfg.nudges:
            nudge = await self._nudge(now, mode, note, say)
            if nudge:
                out.append(nudge)
        return out

    async def _nudge(self, now: datetime, mode: Mode, note: Note, say: Say | None) -> str | None:
        lead = timedelta(minutes=self._cfg.nudge_minutes)
        soon: list[tuple[Task, str]] = []
        for task in await self._tasks.open():
            due = task.due_at
            if due is None or not now < due <= now + lead:
                continue
            if due - task.created_at < 2 * lead:
                continue  # set at short notice: you know about it, and its reminder will fire
            key = f"nudge:{task.id}:{to_iso(due)}"
            if not await self._seen(key):
                soon.append((task, key))
        if not soon:
            return None
        rows = []
        for task, _ in soon[:MAX_LINES]:
            minutes = max(1, round((task.due_at - now).total_seconds() / 60))  # type: ignore[operator]
            rows.append(f"• {task.title} (in {_plural(minutes, 'minute')})")
        head = "Coming up:" if len(soon) > 1 else "Coming up soon:"
        first = soon[0][0].title
        spoken = (
            f"Heads up: {first} is due soon."
            if len(soon) == 1
            else f"Heads up: {len(soon)} things are due soon, starting with {first}."
        )
        # One message for all of them; every task is marked so none is repeated.
        primary = soon[0][1]
        sent = await self._deliver(
            "nudge", primary, Brief("\n".join([head, *rows]), spoken), now, mode, note, say
        )
        for _, key in soon[1:]:
            await self._conn.execute(
                "INSERT OR IGNORE INTO proactive_log (kind, key, outcome, spoken, at) "
                "VALUES ('nudge', ?, 'empty', 0, ?)",
                (key, to_iso(now)),
            )
        await self._conn.commit()
        return sent
