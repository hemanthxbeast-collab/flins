from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from flins.core.clock import FrozenClock
from flins.core.db import connect, migrate
from flins.core.events import Event, EventBus
from flins.core.levels import Mode
from flins.core.modes import (
    TRANSITIONS,
    ModeEvent,
    ModeManager,
    ModeState,
    TransitionError,
    effective_mode,
    initial_state,
    next_state,
)
from flins.core.policy_schema import Policy

T0 = datetime(2026, 11, 9, 4, 30, tzinfo=UTC)  # 10:00 in Asia/Kolkata


@pytest.fixture
def policy(example_policy_dict: dict[str, Any]) -> Policy:
    return Policy.model_validate(example_policy_dict)


def step(
    state: ModeState, event: ModeEvent, policy: Policy, now: datetime = T0, **kw: Any
) -> ModeState:
    return next_state(state, event, now=now, source="test", policy=policy, **kw)


# ---------------------------------------------------------------- pure transition table


def test_every_pair_is_either_listed_or_rejected(policy: Policy) -> None:
    """Exhaustive: each (mode, event) pair behaves exactly as the TRANSITIONS table says."""
    for mode in Mode:
        for event in ModeEvent:
            state = ModeState(
                mode=mode,
                started_at=T0,
                started_by="t",
                until=T0 + timedelta(hours=1)
                if mode in (Mode.WORKING, Mode.FOCUS, Mode.AWAY)
                else None,
                prev_mode=Mode.NORMAL,
            )
            now = T0 + timedelta(hours=2) if event == ModeEvent.EXPIRE else T0
            kw: dict[str, Any] = {}
            if event == ModeEvent.EXTEND:
                kw["until"] = T0 + timedelta(hours=3)
            if (mode, event) not in TRANSITIONS:
                with pytest.raises(TransitionError):
                    step(state, event, policy, now=now, **kw)
                continue
            new = step(state, event, policy, now=now, **kw)
            target = TRANSITIONS[(mode, event)]
            if target == "SAME":
                assert new.mode == mode
            elif target != "PREVIOUS":
                assert new.mode == target


def test_paused_only_answers_resume(policy: Policy) -> None:
    paused = step(initial_state(T0), ModeEvent.KILL, policy)
    for event in (ModeEvent.ENTER_WORKING, ModeEvent.EXIT_TO_NORMAL, ModeEvent.CANCEL):
        with pytest.raises(TransitionError, match="resume"):
            step(paused, event, policy)


def test_enter_uses_default_duration_and_profile(policy: Policy) -> None:
    s = step(initial_state(T0), ModeEvent.ENTER_WORKING, policy, profile="working-strict")
    assert s.mode == Mode.WORKING
    assert s.until == T0 + timedelta(hours=3)
    assert s.profile == "working-strict"
    with pytest.raises(TransitionError, match="profile"):
        step(initial_state(T0), ModeEvent.ENTER_FOCUS, policy, profile="working-strict")


@pytest.mark.parametrize(
    ("until", "message"),
    [(T0 - timedelta(minutes=1), "passed"), (T0 + timedelta(hours=12, minutes=1), "12 hours")],
)
def test_until_guards(policy: Policy, until: datetime, message: str) -> None:
    with pytest.raises(TransitionError, match=message):
        step(initial_state(T0), ModeEvent.ENTER_WORKING, policy, until=until)


def test_expire_only_after_until(policy: Policy) -> None:
    s = step(initial_state(T0), ModeEvent.ENTER_FOCUS, policy)  # 90 min
    with pytest.raises(TransitionError):
        step(s, ModeEvent.EXPIRE, policy, now=T0 + timedelta(minutes=89))
    assert step(s, ModeEvent.EXPIRE, policy, now=T0 + timedelta(minutes=90)).mode == Mode.NORMAL


def test_effective_mode_treats_expired_as_normal(policy: Policy) -> None:
    s = step(initial_state(T0), ModeEvent.ENTER_FOCUS, policy)
    assert effective_mode(s, T0) == Mode.FOCUS
    assert effective_mode(s, T0 + timedelta(minutes=90)) == Mode.NORMAL


def test_cancel_window(policy: Policy) -> None:
    working = step(initial_state(T0), ModeEvent.ENTER_WORKING, policy)
    focus = step(working, ModeEvent.ENTER_FOCUS, policy, now=T0 + timedelta(minutes=5))
    back = step(focus, ModeEvent.CANCEL, policy, now=T0 + timedelta(minutes=5, seconds=9))
    assert back.mode == Mode.WORKING and back.until == working.until
    with pytest.raises(TransitionError, match="Too late"):
        step(focus, ModeEvent.CANCEL, policy, now=T0 + timedelta(minutes=5, seconds=11))


def test_resume_returns_to_unexpired_mode_else_normal(policy: Policy) -> None:
    working = step(initial_state(T0), ModeEvent.ENTER_WORKING, policy)
    paused = step(working, ModeEvent.KILL, policy, now=T0 + timedelta(minutes=1))
    resumed = step(paused, ModeEvent.RESUME, policy, now=T0 + timedelta(minutes=2))
    assert resumed.mode == Mode.WORKING and resumed.until == working.until
    late = step(paused, ModeEvent.RESUME, policy, now=T0 + timedelta(hours=4))
    assert late.mode == Mode.NORMAL and late.until is None


def test_noops_keep_state(policy: Policy) -> None:
    s = initial_state(T0)
    assert step(s, ModeEvent.EXIT_TO_NORMAL, policy) is s
    paused = step(s, ModeEvent.KILL, policy)
    assert step(paused, ModeEvent.KILL, policy) is paused


# ---------------------------------------------------------------- persistence


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Path]:
    path = tmp_path / "t.db"
    conn = await connect(path)
    await migrate(conn)
    await conn.close()
    yield path


async def test_restart_after_expiry_comes_back_normal(db: Path, policy: Policy) -> None:
    clock = FrozenClock(T0)
    m = await ModeManager.open(db, EventBus(), policy, clock)
    await m.apply(ModeEvent.ENTER_WORKING, source="test")
    await m.close()

    clock.advance(timedelta(hours=3, seconds=1))  # FLINS was off past `until`
    m2 = await ModeManager.open(db, EventBus(), policy, clock)
    try:
        assert m2.state.mode == Mode.NORMAL
        assert m2.state.started_by == "restart"
    finally:
        await m2.close()


async def test_restart_before_expiry_keeps_mode(db: Path, policy: Policy) -> None:
    clock = FrozenClock(T0)
    m = await ModeManager.open(db, EventBus(), policy, clock)
    await m.apply(ModeEvent.ENTER_WORKING, source="test")
    await m.close()
    clock.advance(timedelta(hours=1))
    m2 = await ModeManager.open(db, EventBus(), policy, clock)
    try:
        assert m2.state.mode == Mode.WORKING
    finally:
        await m2.close()


async def test_history_audit_and_readback(db: Path, policy: Policy) -> None:
    clock = FrozenClock(T0)
    bus = EventBus()
    seen: list[Event] = []

    async def on(e: Event) -> None:
        seen.append(e)

    bus.subscribe("mode.changed", on)
    m = await ModeManager.open(db, bus, policy, clock)
    try:
        r = await m.apply(ModeEvent.ENTER_WORKING, source="test", until=T0 + timedelta(hours=8))
        assert "Working mode on until 18:00" in r.readback  # 10:00 IST + 8h
        assert "Dry run" in r.readback
        async with m._conn.execute("SELECT to_mode, readback FROM mode_history") as cur:
            rows = await cur.fetchall()
        assert [(r2[0], r2[1]) for r2 in rows] == [("WORKING", r.readback)]
        assert [e.data["to"] for e in seen] == ["WORKING"]
    finally:
        await m.close()


async def test_tick_expires_and_warns_once(db: Path, policy: Policy) -> None:
    clock = FrozenClock(T0)
    bus = EventBus()
    warnings: list[Event] = []

    async def on(e: Event) -> None:
        warnings.append(e)

    bus.subscribe("mode.expiring", on)
    m = await ModeManager.open(db, bus, policy, clock)
    try:
        await m.apply(ModeEvent.ENTER_FOCUS, source="test")  # until T0+90m
        clock.advance(timedelta(minutes=81))
        assert await m.tick() is None
        assert await m.tick() is None
        assert len(warnings) == 1
        clock.advance(timedelta(minutes=9))
        result = await m.tick()
        assert result is not None and result.after.mode == Mode.NORMAL
    finally:
        await m.close()


async def test_change_from_another_process_is_seen(db: Path, policy: Policy) -> None:
    clock = FrozenClock(T0)
    running = await ModeManager.open(db, EventBus(), policy, clock)
    cli = await ModeManager.open(db, EventBus(), policy, clock)
    try:
        await running.apply(ModeEvent.ENTER_WORKING, source="voice")
        await cli.load()
        await cli.apply(ModeEvent.KILL, source="cli")  # `flins stop` in another terminal
        assert running.state.mode == Mode.WORKING
        assert await running.refresh() is True
        assert running.state.mode == Mode.PAUSED
    finally:
        await running.close()
        await cli.close()
