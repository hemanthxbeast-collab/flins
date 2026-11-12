"""JARVIS phase 7: FLINS speaks first, a little (docs/milestones/J7.md).

Off by default. To you only. At most once each, capped per day, never while paused, written
but not spoken in focus / away / quiet hours. Built by code from your tasks and from counts.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite
import pytest
from pydantic import ValidationError

from flins import cli
from flins.assistant.proactive import Proactive
from flins.assistant.tasks import TaskStore
from flins.core.clock import to_iso
from flins.core.db import connect, migrate
from flins.core.levels import Mode
from flins.settings import ProactiveConfig, load_settings
from flins.wiring import _weather_line
from tests.unit.test_j1_assistant import Rig, build, chat

TZ = ZoneInfo("Asia/Kolkata")


def at(hour: int, minute: int = 0, day: int = 10) -> datetime:
    """A local time on Tuesday 10 November 2026 (or another day that month)."""
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


class Out:
    def __init__(self, fail_say: bool = False) -> None:
        self.notes: list[str] = []
        self.said: list[str] = []
        self.fail_say = fail_say

    async def note(self, text: str) -> None:
        self.notes.append(text)

    async def say(self, text: str) -> None:
        if self.fail_say:
            raise RuntimeError("no speakers")
        self.said.append(text)


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    c = await connect(tmp_path / "p.db")
    await migrate(c)
    yield c
    await c.close()


def make(conn: aiosqlite.Connection, **cfg: Any) -> tuple[Proactive, TaskStore]:
    tasks = TaskStore(conn, TZ)
    waiting = int(cfg.pop("_drafts", 0))
    settings = ProactiveConfig(**{"enabled": True, **cfg})

    async def drafts(now: datetime) -> int:
        return waiting

    return Proactive(conn, tasks, settings, pending_drafts=drafts), tasks


async def add(tasks: TaskStore, title: str, due: datetime | None, made: datetime) -> Any:
    return await tasks.add(title, source="told", channel="cli", now=made, at=due)


async def log(conn: aiosqlite.Connection) -> list[tuple[str, str, int]]:
    async with conn.execute("SELECT kind, outcome, spoken FROM proactive_log ORDER BY id") as cur:
        return [(r[0], r[1], r[2]) for r in await cur.fetchall()]


async def a_normal_day(tasks: TaskStore) -> None:
    await add(tasks, "call the bank", at(19), at(7))
    await add(tasks, "lab report", at(17, day=9), at(9, day=8))
    await add(tasks, "buy milk", None, at(7))


# ---------------------------------------------------------------------- off by default


async def test_off_by_default(conn: aiosqlite.Connection) -> None:
    assert ProactiveConfig().enabled is False
    proactive = Proactive(conn, TaskStore(conn, TZ), ProactiveConfig())
    await a_normal_day(TaskStore(conn, TZ))
    out = Out()
    assert await proactive.tick(at(8, 5), mode=Mode.NORMAL, note=out.note, say=out.say) == []
    assert out.notes == [] and out.said == [] and await log(conn) == []


@pytest.mark.parametrize(
    "bad",
    [{"morning": "25:00"}, {"evening": "9pm"}, {"quiet_hours": ["23:00"]}, {"max_per_day": 0},
     {"quiet_hours": ["23:00", "7"]}, {"nudge_minutes": 1}],
)  # fmt: skip
def test_bad_settings_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ProactiveConfig(**bad)


# ---------------------------------------------------------------------- morning


async def test_morning_briefing_once_a_day(conn: aiosqlite.Connection) -> None:
    proactive, tasks = make(conn, _drafts=2, nudges=False)
    await a_normal_day(tasks)
    out = Out()
    kw: dict[str, Any] = {"mode": Mode.NORMAL, "note": out.note, "say": out.say}
    assert await proactive.tick(at(7, 59), **kw) == []  # not yet
    sent = await proactive.tick(at(8, 0), **kw)
    assert sent == out.notes and out.notes[0].splitlines() == [
        "Good morning. Tuesday 10 November.",
        "Today: 1 thing.",
        "• call the bank (today 19:00)",
        "Overdue: 1.",
        "• lab report (Mon 9 Nov 17:00 overdue)",
        "1 other open task without a date.",
        "2 reply drafts are waiting for your approval.",
    ]
    assert out.said == [
        "Good morning. 1 thing today, 1 overdue, 2 drafts waiting. The list is in your self-chat."
    ]
    for minute in (1, 2, 30):  # the scheduler ticks every second: never again today
        assert await proactive.tick(at(8, minute), **kw) == []
    assert len(out.notes) == 1 and await log(conn) == [("morning", "sent", 1)]
    again = await proactive.tick(at(8, 0, day=11), **kw)  # tomorrow: a new one
    assert len(again) == 1 and again[0].startswith("Good morning. Wednesday 11 November.")


async def test_a_briefing_with_nothing_to_say_is_skipped(conn: aiosqlite.Connection) -> None:
    proactive, tasks = make(conn, nudges=False)
    await add(tasks, "buy milk", None, at(7))  # no date: not worth a briefing by itself
    out = Out()
    assert await proactive.tick(at(8, 5), mode=Mode.NORMAL, note=out.note, say=out.say) == []
    assert out.notes == [] and await log(conn) == [("morning", "empty", 0)]


async def test_a_late_briefing_is_skipped_not_sent(conn: aiosqlite.Connection) -> None:
    proactive, tasks = make(conn, nudges=False)
    await a_normal_day(tasks)
    out = Out()
    # FLINS was started at 11:30: three and a half hours after the briefing time.
    assert await proactive.tick(at(11, 30), mode=Mode.NORMAL, note=out.note, say=out.say) == []
    assert out.notes == [] and await log(conn) == [("morning", "late", 0)]


async def test_weather_line_and_its_failure(conn: aiosqlite.Connection) -> None:
    tasks = TaskStore(conn, TZ)
    await a_normal_day(tasks)

    async def weather() -> str | None:
        return "now 27°C, partly cloudy; today 19 to 30°C, rain chance 10%."

    async def broken() -> str | None:
        raise RuntimeError("offline")

    cfg = ProactiveConfig(enabled=True)
    brief = await Proactive(conn, tasks, cfg, weather=weather).briefing("morning", at(8))
    assert brief is not None and brief.text.splitlines()[-1].startswith("Weather: now 27°C")
    brief = await Proactive(conn, tasks, cfg, weather=broken).briefing("morning", at(8))
    assert brief is not None and "Weather" not in brief.text  # the briefing still goes out


# ---------------------------------------------------------------------- interrupt policy


async def test_paused_means_nothing_and_it_comes_after_resume(conn: aiosqlite.Connection) -> None:
    proactive, tasks = make(conn, nudges=False)
    await a_normal_day(tasks)
    out = Out()
    assert await proactive.tick(at(8, 5), mode=Mode.PAUSED, note=out.note, say=out.say) == []
    assert out.notes == [] and await log(conn) == []  # not used up
    sent = await proactive.tick(at(8, 40), mode=Mode.NORMAL, note=out.note, say=out.say)
    assert len(sent) == 1


@pytest.mark.parametrize(
    ("mode", "cfg", "spoken"),
    [
        (Mode.NORMAL, {}, True),
        (Mode.WORKING, {}, True),
        (Mode.FOCUS, {}, False),  # do not disturb: written only
        (Mode.AWAY, {}, False),  # you aren't there to hear it
        (Mode.NORMAL, {"speak": False}, False),
        (Mode.NORMAL, {"morning": "06:30"}, False),  # inside the default quiet hours
        (Mode.NORMAL, {"morning": "06:30", "quiet_hours": []}, True),
        (Mode.NORMAL, {"quiet_hours": ["07:30", "09:00"]}, False),
    ],
)
async def test_when_it_speaks(
    conn: aiosqlite.Connection, mode: Mode, cfg: dict[str, Any], spoken: bool
) -> None:
    proactive, tasks = make(conn, nudges=False, **cfg)
    await a_normal_day(tasks)
    out = Out()
    hour, minute = (6, 35) if cfg.get("morning") else (8, 5)
    sent = await proactive.tick(at(hour, minute), mode=mode, note=out.note, say=out.say)
    assert len(sent) == 1 and len(out.notes) == 1  # always written
    assert bool(out.said) is spoken
    assert await log(conn) == [("morning", "sent", int(spoken))]


async def test_no_voice_or_a_broken_speaker_still_writes_it(conn: aiosqlite.Connection) -> None:
    proactive, tasks = make(conn, nudges=False)
    await a_normal_day(tasks)
    out = Out(fail_say=True)
    assert len(await proactive.tick(at(8, 5), mode=Mode.NORMAL, note=out.note, say=out.say)) == 1
    assert (
        len(await proactive.tick(at(8, 5, day=11), mode=Mode.NORMAL, note=out.note, say=None)) == 1
    )
    assert len(out.notes) == 2


async def test_the_daily_cap_is_absolute(conn: aiosqlite.Connection) -> None:
    proactive, tasks = make(conn, max_per_day=1, nudge_minutes=60)
    await a_normal_day(tasks)
    await add(tasks, "submit the form", at(15), at(9))
    out = Out()
    kw: dict[str, Any] = {"mode": Mode.NORMAL, "note": out.note, "say": out.say}
    assert len(await proactive.tick(at(8, 5), **kw)) == 1  # the morning briefing
    assert await proactive.tick(at(14, 10), **kw) == []  # a nudge would be the second today
    assert await proactive.tick(at(21, 35), **kw) == []  # and so would the evening summary
    assert len(out.notes) == 1
    assert await log(conn) == [
        ("morning", "sent", 1),
        ("nudge", "capped", 0),
        ("evening", "capped", 0),
    ]
    assert len(await proactive.tick(at(8, 5, day=11), **kw)) == 1  # a new day, a new allowance


async def test_a_failed_note_is_not_repeated_every_second(conn: aiosqlite.Connection) -> None:
    proactive, tasks = make(conn, nudges=False)
    await a_normal_day(tasks)
    calls = {"n": 0}

    async def note(text: str) -> None:
        calls["n"] += 1
        raise RuntimeError("whatsapp is down")

    with pytest.raises(RuntimeError):
        await proactive.tick(at(8, 5), mode=Mode.NORMAL, note=note, say=None)
    assert await proactive.tick(at(8, 6), mode=Mode.NORMAL, note=note, say=None) == []
    assert calls["n"] == 1  # at most once, even though it never arrived


# ---------------------------------------------------------------------- evening


async def test_evening_summary(conn: aiosqlite.Connection) -> None:
    proactive, tasks = make(conn, _drafts=1, nudges=False, morning=None)
    done1 = await add(tasks, "call the bank", at(12), at(7))
    done2 = await add(tasks, "send the graphs", None, at(7))
    await tasks.complete(done1, at(12, 30))
    await tasks.complete(done2, at(16))
    old = await add(tasks, "yesterday's thing", None, at(7, day=9))
    await tasks.complete(old, at(20, day=9))  # not today
    await add(tasks, "lab report", at(17), at(9, day=8))
    await add(tasks, "dentist", at(10, day=11), at(9, day=8))
    await add(tasks, "next week", at(10, day=17), at(9, day=8))
    cur = await conn.execute(
        "INSERT INTO bursts (chat_jid, first_ts, last_ts) VALUES ('x', ?, ?)",
        (to_iso(at(10)), to_iso(at(10))),
    )
    seen = [("auto_safe", at(10)), ("notify", at(11)), ("notify", at(12)), ("draft", at(13))]
    seen += [("ignore", at(14)), ("notify", at(10, day=9))]
    for action, when in seen:
        await conn.execute(
            "INSERT INTO decisions (burst_id, mode, contact_tier, ceilings, final_level, action, "
            "created_at) VALUES (?, 'WORKING', 'trusted', '{}', 1, ?, ?)",
            (cur.lastrowid, action, to_iso(when)),
        )
    await conn.execute(
        "INSERT INTO messages (wa_msg_id, chat_jid, sender_jid, direction, type, text, ts) "
        "VALUES ('m1', 'x', 'x', 'IN', 'text', 'SECRET WORDS from a contact', ?)",
        (to_iso(at(10)),),
    )
    await conn.commit()
    out = Out()
    sent = await proactive.tick(at(21, 30), mode=Mode.NORMAL, note=out.note, say=out.say)
    assert len(sent) == 1 and sent[0].splitlines() == [
        "End of day.",
        "Done today: 2.",
        "• call the bank",
        "• send the graphs",
        "Still open: 1.",
        "• lab report (today 17:00 overdue)",
        "Tomorrow: 1.",
        "• dentist (tomorrow 10:00)",
        "Messages today: 5 (1 answered by FLINS, 1 drafted for you, 2 shown to you, 1 ignored).",
        "1 reply draft still waiting for your approval.",
    ]
    assert "SECRET" not in sent[0]  # counts only, never what a contact wrote
    assert out.said == [
        "End of day. 2 done, 1 still open, 1 for tomorrow. The summary is in your self-chat."
    ]


async def test_an_empty_day_gets_no_evening_summary(conn: aiosqlite.Connection) -> None:
    proactive, _ = make(conn, nudges=False)
    out = Out()
    assert await proactive.tick(at(21, 35), mode=Mode.NORMAL, note=out.note, say=out.say) == []
    assert ("evening", "empty", 0) in await log(conn)


# ---------------------------------------------------------------------- nudges


async def test_a_heads_up_before_a_deadline_once(conn: aiosqlite.Connection) -> None:
    proactive, tasks = make(conn, morning=None, evening=None, nudge_minutes=60)
    await add(tasks, "lab report", at(16), at(11))
    out = Out()
    kw: dict[str, Any] = {"mode": Mode.NORMAL, "note": out.note, "say": out.say}
    assert await proactive.tick(at(14, 30), **kw) == []  # more than an hour to go
    sent = await proactive.tick(at(15, 5), **kw)
    assert sent == ["Coming up soon:\n• lab report (in 55 minutes)"]
    assert out.said == ["Heads up: lab report is due soon."]
    assert await proactive.tick(at(15, 6), **kw) == [] and len(out.notes) == 1
    assert await proactive.tick(at(16, 5), **kw) == []  # past due: the reminder's job, not ours


async def test_no_heads_up_for_short_notice_done_or_switched_off(
    conn: aiosqlite.Connection,
) -> None:
    proactive, tasks = make(conn, morning=None, evening=None, nudge_minutes=60)
    await add(tasks, "call back in 20 minutes", at(15, 20), at(15))  # you just set it
    finished = await add(tasks, "already done", at(15, 40), at(9))
    await tasks.complete(finished, at(14))
    await add(tasks, "no date", None, at(9))
    out = Out()
    assert await proactive.tick(at(15, 5), mode=Mode.NORMAL, note=out.note, say=out.say) == []
    off, tasks2 = make(conn, morning=None, evening=None, nudges=False)
    await add(tasks2, "lab report", at(16), at(9))
    assert await off.tick(at(15, 30), mode=Mode.NORMAL, note=out.note, say=out.say) == []
    assert out.notes == []


async def test_several_deadlines_make_one_message(conn: aiosqlite.Connection) -> None:
    proactive, tasks = make(conn, morning=None, evening=None, nudge_minutes=60)
    await add(tasks, "lab report", at(16), at(9))
    await add(tasks, "submit the form", at(15, 45), at(9))
    out = Out()
    kw: dict[str, Any] = {"mode": Mode.FOCUS, "note": out.note, "say": out.say}
    sent = await proactive.tick(at(15, 15), **kw)
    assert len(sent) == 1 and sent[0].splitlines()[0] == "Coming up:"
    assert (
        "• submit the form (in 30 minutes)" in sent[0] and "• lab report (in 45 minutes)" in sent[0]
    )
    assert out.said == []  # focus: written, not spoken
    assert await proactive.tick(at(15, 16), **kw) == []  # neither is repeated
    async with conn.execute("SELECT COUNT(*) FROM proactive_log WHERE outcome = 'sent'") as cur:
        assert (await cur.fetchone())[0] == 1  # type: ignore[index]  # one against the cap


# ---------------------------------------------------------------------- asking for it


@pytest.fixture
async def rig(project: Path) -> AsyncIterator[Rig]:
    r = await build(project)
    yield r
    await r.modes.close()
    await r.conn.close()


@pytest.mark.parametrize(
    "phrase",
    ["brief me", "Hey FLINS, brief me", "morning briefing", "give me my briefing",
     "how does my day look?", "what's on today", "what do I have today?"],
)  # fmt: skip
async def test_you_can_ask_for_the_briefing_any_time(rig: Rig, phrase: str) -> None:
    assert rig.settings.app.assistant.proactive.enabled is False  # works even when it is off
    await rig.say("remind me to call the bank at 7pm", "cli")
    said = await rig.say(phrase, "cli")
    assert said is not None and said.startswith("Good morning. Tuesday 10 November.")
    assert "• call the bank (today 19:00)" in said
    assert not any(c[0].startswith("chat") for c in rig.backend.calls)  # no model


async def test_evening_on_demand_empty_days_and_self_chat(rig: Rig) -> None:
    assert await rig.say("brief me", "cli") == (
        "Nothing is scheduled today, nothing is overdue and no drafts are waiting."
    )
    assert await rig.say("evening summary", "cli") == "Nothing to report for today."
    await rig.say("add a task buy milk", "cli")
    await rig.say("done with buy milk", "cli")
    said = await rig.say("wrap up my day", "voice")
    assert said is not None and said.splitlines()[:3] == [
        "End of day.",
        "Done today: 1.",
        "• buy milk",
    ]
    assert await rig.say("brief me", "self_chat") is None  # a note to yourself, not a command
    asked = await rig.say("FLINS, end of day summary", "self_chat")
    assert asked is not None and asked.startswith("End of day.")
    rig.answers(chat=chat("A briefing is a short summary."))
    assert await rig.say("what is a briefing?", "cli") == "A briefing is a short summary."


async def test_audit_rows_have_no_titles(conn: aiosqlite.Connection) -> None:
    proactive, tasks = make(conn, nudge_minutes=60)
    await a_normal_day(tasks)
    await add(tasks, "secret project deadline", at(9), at(7, day=9))
    out = Out()
    await proactive.tick(at(8, 5), mode=Mode.NORMAL, note=out.note, say=out.say)
    await proactive.handle("brief me", "cli", at(8, 10), addressed=True)
    async with conn.execute(
        "SELECT payload FROM audit_log WHERE kind = 'assistant.proactive'"
    ) as cur:
        rows = [json.loads(r[0]) for r in await cur.fetchall()]
    outcomes = sorted((r["kind"], r["outcome"]) for r in rows)
    assert outcomes == [("morning", "asked"), ("morning", "sent"), ("nudge", "sent")]
    assert "secret" not in json.dumps(rows) and "bank" not in json.dumps(rows)


def test_cli_briefing_and_the_weather_line(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = ["--root", str(project)]
    assert cli.main([*root, "briefing"]) == 0
    out = capsys.readouterr().out
    assert "Nothing to report." in out and "assistant.proactive.enabled is false" in out
    assert cli.main([*root, "briefing", "evening"]) == 0
    assert _weather_line(load_settings(project)) is None  # the weather tool is off by default
