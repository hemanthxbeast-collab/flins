"""Mode state machine (SPEC §3).

`next_state()` is pure: (state, event, inputs) -> new state, or TransitionError.
`ModeManager` persists state in SQLite. It uses its own connection and `BEGIN IMMEDIATE`, so
`flins stop` in one terminal and `flins run` in another can't both change the mode at once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import aiosqlite

from flins.core import readback
from flins.core.audit import AuditLog
from flins.core.clock import Clock, from_iso, to_iso
from flins.core.db import connect
from flins.core.events import EventBus
from flins.core.levels import Level, Mode
from flins.core.policy_schema import TIMED_MODES, Policy


class ModeEvent(StrEnum):
    ENTER_WORKING = "ENTER_WORKING"
    ENTER_FOCUS = "ENTER_FOCUS"
    ENTER_AWAY = "ENTER_AWAY"
    EXIT_TO_NORMAL = "EXIT_TO_NORMAL"
    EXTEND = "EXTEND"
    EXPIRE = "EXPIRE"
    CANCEL = "CANCEL"
    KILL = "KILL"
    RESUME = "RESUME"


ENTER_EVENTS: dict[ModeEvent, Mode] = {
    ModeEvent.ENTER_WORKING: Mode.WORKING,
    ModeEvent.ENTER_FOCUS: Mode.FOCUS,
    ModeEvent.ENTER_AWAY: Mode.AWAY,
}

Target = Mode | Literal["PREVIOUS", "SAME"]

# The whole state machine. Any (mode, event) pair not listed here is rejected.
TRANSITIONS: dict[tuple[Mode, ModeEvent], Target] = {
    # NORMAL
    (Mode.NORMAL, ModeEvent.ENTER_WORKING): Mode.WORKING,
    (Mode.NORMAL, ModeEvent.ENTER_FOCUS): Mode.FOCUS,
    (Mode.NORMAL, ModeEvent.ENTER_AWAY): Mode.AWAY,
    (Mode.NORMAL, ModeEvent.EXIT_TO_NORMAL): Mode.NORMAL,
    (Mode.NORMAL, ModeEvent.KILL): Mode.PAUSED,
    # WORKING
    (Mode.WORKING, ModeEvent.ENTER_WORKING): Mode.WORKING,
    (Mode.WORKING, ModeEvent.ENTER_FOCUS): Mode.FOCUS,
    (Mode.WORKING, ModeEvent.ENTER_AWAY): Mode.AWAY,
    (Mode.WORKING, ModeEvent.EXIT_TO_NORMAL): Mode.NORMAL,
    (Mode.WORKING, ModeEvent.EXTEND): "SAME",
    (Mode.WORKING, ModeEvent.EXPIRE): Mode.NORMAL,
    (Mode.WORKING, ModeEvent.CANCEL): "PREVIOUS",
    (Mode.WORKING, ModeEvent.KILL): Mode.PAUSED,
    # FOCUS
    (Mode.FOCUS, ModeEvent.ENTER_WORKING): Mode.WORKING,
    (Mode.FOCUS, ModeEvent.ENTER_FOCUS): Mode.FOCUS,
    (Mode.FOCUS, ModeEvent.ENTER_AWAY): Mode.AWAY,
    (Mode.FOCUS, ModeEvent.EXIT_TO_NORMAL): Mode.NORMAL,
    (Mode.FOCUS, ModeEvent.EXTEND): "SAME",
    (Mode.FOCUS, ModeEvent.EXPIRE): Mode.NORMAL,
    (Mode.FOCUS, ModeEvent.CANCEL): "PREVIOUS",
    (Mode.FOCUS, ModeEvent.KILL): Mode.PAUSED,
    # AWAY
    (Mode.AWAY, ModeEvent.ENTER_WORKING): Mode.WORKING,
    (Mode.AWAY, ModeEvent.ENTER_FOCUS): Mode.FOCUS,
    (Mode.AWAY, ModeEvent.ENTER_AWAY): Mode.AWAY,
    (Mode.AWAY, ModeEvent.EXIT_TO_NORMAL): Mode.NORMAL,
    (Mode.AWAY, ModeEvent.EXTEND): "SAME",
    (Mode.AWAY, ModeEvent.EXPIRE): Mode.NORMAL,
    (Mode.AWAY, ModeEvent.CANCEL): "PREVIOUS",
    (Mode.AWAY, ModeEvent.KILL): Mode.PAUSED,
    # PAUSED answers "resume" only.
    (Mode.PAUSED, ModeEvent.KILL): Mode.PAUSED,
    (Mode.PAUSED, ModeEvent.RESUME): "PREVIOUS",
}

MAX_MODE_DURATION = timedelta(hours=12)
CANCEL_WINDOW = timedelta(seconds=10)
EXPIRY_WARNING = timedelta(minutes=10)


class TransitionError(ValueError):
    """The event isn't allowed right now. The message is safe to show the user."""


@dataclass(frozen=True)
class ModeState:
    mode: Mode
    started_at: datetime
    started_by: str
    until: datetime | None = None
    profile: str | None = None
    prev_mode: Mode | None = None
    prev_until: datetime | None = None
    prev_profile: str | None = None
    rev: int = 0

    def is_expired(self, now: datetime) -> bool:
        return self.until is not None and now >= self.until


def initial_state(now: datetime) -> ModeState:
    return ModeState(mode=Mode.NORMAL, started_at=now, started_by="init")


def effective_mode(state: ModeState, now: datetime) -> Mode:
    """The mode to *act* on. An expired timed mode counts as NORMAL even before it's ticked."""
    if state.mode in TIMED_MODES and state.is_expired(now):
        return Mode.NORMAL
    return state.mode


def _label(event: ModeEvent) -> str:
    return event.value.lower().replace("_", " ")


def next_state(
    state: ModeState,
    event: ModeEvent,
    *,
    now: datetime,
    source: str,
    policy: Policy,
    until: datetime | None = None,
    profile: str | None = None,
) -> ModeState:
    """Pure transition function. Returns the new state (the same object if nothing changes)."""
    target = TRANSITIONS.get((state.mode, event))
    if target is None:
        if state.mode == Mode.PAUSED:
            raise TransitionError("FLINS is paused. Say 'resume' first.")
        if event == ModeEvent.RESUME:
            raise TransitionError(
                f"FLINS isn't paused. It's in {state.mode.value.lower()} mode, nothing to resume."
            )
        raise TransitionError(f"Can't {_label(event)} while in {state.mode.value} mode.")

    def remember(new: ModeState) -> ModeState:
        return replace(
            new,
            prev_mode=state.mode,
            prev_until=state.until,
            prev_profile=state.profile,
            rev=state.rev + 1,
        )

    if event in ENTER_EVENTS:
        mode = ENTER_EVENTS[event]
        mode_policy = policy.modes[mode]
        assert mode_policy.default_duration is not None  # guaranteed by the policy schema
        end = until or now + mode_policy.default_duration
        _check_until(end, now)
        if profile is not None:
            prof = policy.profiles.get(profile)
            if prof is None or prof.base != mode:
                raise TransitionError(
                    f"There's no {mode.value.lower()} profile called {profile!r}."
                )
        return remember(
            ModeState(mode=mode, started_at=now, started_by=source, until=end, profile=profile)
        )

    if event == ModeEvent.EXTEND:
        if until is None:
            raise TransitionError("Extend until when?")
        _check_until(until, now)
        return replace(state, until=until, rev=state.rev + 1)

    if event == ModeEvent.EXPIRE:
        if not state.is_expired(now):
            raise TransitionError("The mode hasn't ended yet.")
        return remember(ModeState(mode=Mode.NORMAL, started_at=now, started_by=source))

    if event == ModeEvent.CANCEL:
        if now - state.started_at > CANCEL_WINDOW or state.prev_mode is None:
            raise TransitionError("Too late to cancel. Say 'I'm done' to go back to normal.")
        return _restore(state, now, source)

    if event == ModeEvent.RESUME:
        return _restore(state, now, source)

    if event == ModeEvent.KILL:
        if state.mode == Mode.PAUSED:
            return state
        return remember(ModeState(mode=Mode.PAUSED, started_at=now, started_by=source))

    if event == ModeEvent.EXIT_TO_NORMAL:
        if state.mode == Mode.NORMAL:
            return state
        return remember(ModeState(mode=Mode.NORMAL, started_at=now, started_by=source))

    raise AssertionError(f"unhandled transition {state.mode} {event}")  # pragma: no cover


def _restore(state: ModeState, now: datetime, source: str) -> ModeState:
    """Go back to the previous mode, unless it was timed and has since ended."""
    prev = state.prev_mode or Mode.NORMAL
    if prev == Mode.PAUSED:
        prev = Mode.NORMAL
    if prev in TIMED_MODES and (state.prev_until is None or state.prev_until <= now):
        prev = Mode.NORMAL
    timed = prev in TIMED_MODES
    return ModeState(
        mode=prev,
        started_at=now,
        started_by=source,
        until=state.prev_until if timed else None,
        profile=state.prev_profile if timed else None,
        rev=state.rev + 1,
    )


def _check_until(until: datetime, now: datetime) -> None:
    if until <= now:
        raise TransitionError("That time has already passed.")
    if until - now > MAX_MODE_DURATION:
        hours = int(MAX_MODE_DURATION.total_seconds() // 3600)
        raise TransitionError(f"Modes can last at most {hours} hours.")


# ---------------------------------------------------------------------------- persistence


@dataclass(frozen=True)
class TransitionResult:
    event: ModeEvent
    before: ModeState
    after: ModeState
    changed: bool
    readback: str


def _row_to_state(row: aiosqlite.Row) -> ModeState:
    def opt_dt(v: str | None) -> datetime | None:
        return from_iso(v) if v else None

    return ModeState(
        mode=Mode(row["mode"]),
        started_at=from_iso(row["started_at"]),
        started_by=row["started_by"],
        until=opt_dt(row["until"]),
        profile=row["profile"],
        prev_mode=Mode(row["prev_mode"]) if row["prev_mode"] else None,
        prev_until=opt_dt(row["prev_until"]),
        prev_profile=row["prev_profile"],
        rev=int(row["rev"]),
    )


class ModeManager:
    """Owns the persisted mode. Create with `await ModeManager.open(...)`."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        bus: EventBus,
        policy: Policy,
        clock: Clock,
    ) -> None:
        self._conn = conn
        self._audit = AuditLog(conn)
        self._bus = bus
        self._policy = policy
        self._clock = clock
        self._tz = ZoneInfo(policy.user.timezone)
        self.state: ModeState = initial_state(clock.now())
        self._warned_for: tuple[int, datetime | None] | None = None

    @classmethod
    async def open(cls, db_path: Path, bus: EventBus, policy: Policy, clock: Clock) -> ModeManager:
        """Open with a dedicated connection and load (or expire) the saved state."""
        manager = cls(await connect(db_path), bus, policy, clock)
        await manager.load()
        return manager

    async def close(self) -> None:
        await self._conn.close()

    # ----------------------------------------------------------------- reading

    async def _read(self) -> ModeState | None:
        async with self._conn.execute("SELECT * FROM mode_state WHERE id = 1") as cur:
            row = await cur.fetchone()
        return _row_to_state(row) if row else None

    async def load(self) -> ModeState:
        """Read the saved mode. A timed mode that ended while FLINS was off becomes NORMAL."""
        saved = await self._read()
        if saved is None:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                if await self._read() is None:
                    await self._write(initial_state(self._clock.now()))
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise
            saved = await self._read()
            assert saved is not None
        self.state = saved
        if saved.mode in TIMED_MODES and saved.is_expired(self._clock.now()):
            await self.apply(ModeEvent.EXPIRE, source="restart")
        return self.state

    async def refresh(self) -> bool:
        """Pick up changes made by another process. Returns True if the mode changed."""
        saved = await self._read()
        if saved is None or saved.rev == self.state.rev:
            return False
        before, self.state = self.state, saved
        await self._bus.publish(
            "mode.changed",
            {"from": before.mode.value, "to": saved.mode.value, "source": "external"},
        )
        return True

    # ----------------------------------------------------------------- writing

    async def _write(self, s: ModeState) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO mode_state (id, mode, profile, until, started_at, started_by, "
            "prev_mode, prev_until, prev_profile, rev) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s.mode.value,
                s.profile,
                to_iso(s.until) if s.until else None,
                to_iso(s.started_at),
                s.started_by,
                s.prev_mode.value if s.prev_mode else None,
                to_iso(s.prev_until) if s.prev_until else None,
                s.prev_profile,
                s.rev,
            ),
        )

    async def apply(
        self,
        event: ModeEvent,
        *,
        source: str,
        until: datetime | None = None,
        profile: str | None = None,
    ) -> TransitionResult:
        """Apply an event atomically. Raises TransitionError if it isn't allowed."""
        now = self._clock.now()
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            before = await self._read() or initial_state(now)
            after = next_state(
                before,
                event,
                now=now,
                source=source,
                policy=self._policy,
                until=until,
                profile=profile,
            )
            changed = after != before
            text = await self._readback(event, before, after, changed, now)
            if changed:
                await self._write(after)
                await self._conn.execute(
                    "INSERT INTO mode_history (from_mode, to_mode, event, source, ts, until, "
                    "readback) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        before.mode.value,
                        after.mode.value,
                        event.value,
                        source,
                        to_iso(now),
                        to_iso(after.until) if after.until else None,
                        text,
                    ),
                )
            await self._conn.commit()
        except BaseException:
            await self._conn.rollback()
            raise
        self.state = after
        result = TransitionResult(event, before, after, changed, text)
        if changed:
            data = {
                "event": event.value,
                "from": before.mode.value,
                "to": after.mode.value,
                "until": to_iso(after.until) if after.until else None,
                "profile": after.profile,
                "source": source,
            }
            await self._audit.record("modes", "mode.changed", data)
            await self._bus.publish("mode.changed", data)
        return result

    async def tick(self) -> TransitionResult | None:
        """Call about once a second: ends expired modes and warns once before the end."""
        await self.refresh()
        now = self._clock.now()
        s = self.state
        if s.mode not in TIMED_MODES or s.until is None:
            return None
        if s.is_expired(now):
            try:
                return await self.apply(ModeEvent.EXPIRE, source="timer")
            except TransitionError:
                return None  # another process already changed the mode
        key = (s.rev, s.until)
        if s.until - now <= EXPIRY_WARNING and self._warned_for != key:
            self._warned_for = key
            await self._bus.publish(
                "mode.expiring",
                {
                    "mode": s.mode.value,
                    "until": to_iso(s.until),
                    "text": readback.expiring(s, self._tz),
                },
            )
        return None

    # ----------------------------------------------------------------- readbacks

    async def _readback(
        self, event: ModeEvent, before: ModeState, after: ModeState, changed: bool, now: datetime
    ) -> str:
        ctx = readback.Context(
            tz=self._tz,
            now=now,
            dry_run=self._policy.dry_run,
            auto_contacts=await self._auto_contact_count(after),
            debrief=await self._debrief(before) if after.mode == Mode.NORMAL and changed else None,
        )
        return readback.transition(event, before, after, changed, ctx)

    async def status_text(self) -> str:
        now = self._clock.now()
        ctx = readback.Context(
            tz=self._tz,
            now=now,
            dry_run=self._policy.dry_run,
            auto_contacts=await self._auto_contact_count(self.state),
            debrief=None,
        )
        return readback.status(self.state, ctx)

    async def _auto_contact_count(self, s: ModeState) -> int:
        """How many contacts this mode would let FLINS answer by itself (AUTO_SAFE or higher)."""
        if s.mode not in TIMED_MODES:
            return 0
        mode_ceiling = self._policy.modes[s.mode].ceiling
        tiers = [
            name
            for name, t in self._policy.tiers.items()
            if min(mode_ceiling, t.ceiling) >= Level.AUTO_SAFE
        ]
        if not tiers:
            return 0
        marks = ",".join("?" * len(tiers))
        async with self._conn.execute(
            f"SELECT COUNT(*) FROM contacts WHERE tier IN ({marks})", tiers
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def _debrief(self, since: ModeState) -> readback.Debrief:
        """Summarise decisions since the session began: the last time FLINS left NORMAL.

        Using that rather than `since.started_at` means a stop/resume in the middle of a
        working session doesn't cut the earlier messages out of the summary.
        """
        timed = tuple(m.value for m in TIMED_MODES)
        async with self._conn.execute(
            "SELECT ts FROM mode_history WHERE from_mode = 'NORMAL' "
            f"AND to_mode IN ({','.join('?' * len(timed))}) ORDER BY id DESC LIMIT 1",
            timed,
        ) as cur:
            row = await cur.fetchone()
        start = row[0] if row else to_iso(since.started_at)
        async with self._conn.execute(
            "SELECT action, escalate, flags FROM decisions WHERE created_at >= ?",
            (start,),
        ) as cur:
            rows = await cur.fetchall()
        actions: dict[str, int] = {}
        escalations = scams = 0
        for r in rows:
            actions[r["action"]] = actions.get(r["action"], 0) + 1
            escalations += int(r["escalate"])
            scams += int("credentials" in json.loads(r["flags"] or "[]"))
        return readback.Debrief(actions=actions, escalations=escalations, suspicious=scams)
