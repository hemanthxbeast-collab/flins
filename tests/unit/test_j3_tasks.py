"""JARVIS phase 3a: tasks and reminders (docs/milestones/J3.md).

Fixed phrases, no AI model. A time FLINS can't read is asked for, never guessed. Reminders
fire once, wait while FLINS is stopped, and only ever go to you.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from flins import cli
from flins.assistant.task_commands import TaskCommands, deliver_due
from flins.assistant.tasks import TaskStore
from flins.assistant.timeparse import describe, extract, next_occurrence
from flins.core.clock import FrozenClock
from flins.core.db import connect, migrate
from flins.llm.prompts import load_prompt
from flins.settings import load_settings
from flins.web.app import create_app
from tests.conftest import REPO_ROOT
from tests.unit.test_j1_assistant import Rig, build, chat

TZ = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 11, 10, 8, 30, tzinfo=UTC)  # Tuesday 14:00 in India


def ist(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


@pytest.fixture
async def rig(project: Path) -> AsyncIterator[Rig]:
    r = await build(project)
    yield r
    await r.modes.close()
    await r.conn.close()


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[TaskStore]:
    conn = await connect(tmp_path / "t.db")
    await migrate(conn)
    yield TaskStore(conn, TZ)
    await conn.close()


async def rows(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    async with conn.execute("SELECT * FROM tasks ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]


class Sink:
    def __init__(self) -> None:
        self.notes: list[str] = []
        self.spoken: list[str] = []

    async def note(self, text: str) -> None:
        self.notes.append(text)

    async def say(self, text: str) -> None:
        self.spoken.append(text)


# ---------------------------------------------------------------------- time phrases


@pytest.mark.parametrize(
    ("said", "title", "at", "repeat"),
    [
        ("call the bank at 7", "call the bank", ist(10, 19), "none"),
        ("call the bank at 7am", "call the bank", ist(11, 7), "none"),
        ("call the bank at 9:30 pm", "call the bank", ist(10, 21, 30), "none"),
        ("submit the report tomorrow at 5", "submit the report", ist(11, 17), "none"),
        ("check the oven in 20 minutes", "check the oven", ist(10, 14, 20), "none"),
        ("stretch in half an hour", "stretch", ist(10, 14, 30), "none"),
        ("pay rent on Friday", "pay rent", ist(13, 9), "none"),
        ("revise physics tomorrow evening", "revise physics", ist(11, 18), "none"),
        # A bare hour on a repeating reminder is read as the morning; the reply says the time.
        ("take medicine every day at 8", "take medicine", ist(11, 8), "daily"),
        ("stand up every weekday at 10am", "stand up", ist(11, 10), "weekdays"),
        ("water the plants every sunday at 9am", "water the plants", ist(15, 9), "weekly"),
        ("go to the gym at 18:00", "go to the gym", ist(10, 18), "none"),
    ],
)
def test_time_phrases(said: str, title: str, at: datetime, repeat: str) -> None:
    w = extract(said, T0, TZ)
    assert (w.title, w.at, w.repeat) == (title, at, repeat)


@pytest.mark.parametrize("said", ["call the bank", "buy 2 notebooks", "read chapter 7", "at 99"])
def test_no_clear_time_is_never_guessed(said: str) -> None:
    assert extract(said, T0, TZ).at is None


def test_repeats_move_forward_and_skip_weekends() -> None:
    fri = datetime(2026, 11, 13, 8, 0, tzinfo=TZ)
    assert next_occurrence(fri, "none", fri) is None
    assert next_occurrence(fri, "daily", fri) == fri + timedelta(days=1)
    assert next_occurrence(fri, "weekdays", fri) == fri + timedelta(days=3)  # Monday
    assert next_occurrence(fri, "weekly", fri) == fri + timedelta(days=7)
    # After a long gap (the PC was off) the next time is in the future, not a backlog.
    later = fri + timedelta(days=10, hours=1)
    nxt = next_occurrence(fri, "daily", later)
    assert nxt is not None and later < nxt <= later + timedelta(days=1)


def test_times_are_described_relative_to_today() -> None:
    assert "today" in describe(ist(10, 19), T0, TZ) and "19:00" in describe(ist(10, 19), T0, TZ)
    assert "tomorrow" in describe(ist(11, 7), T0, TZ)


# ---------------------------------------------------------------------- commands, no model


async def test_remind_me_creates_a_reminder_without_any_model(rig: Rig) -> None:
    said = await rig.say("remind me to call the bank at 7", source="cli")
    assert said is not None and said.startswith("Reminder set for today") and "19:00" in said
    assert said.endswith("call the bank.")
    (row,) = await rows(rig.conn)
    assert row["title"] == "call the bank" and row["status"] == "open"
    assert datetime.fromisoformat(row["remind_at"]) == ist(10, 19)
    assert row["channel"] == "cli" and row["repeat"] == "none"
    assert rig.jobs() == []  # no model was called
    # The audit row names the task by id, never by title.
    async with rig.conn.execute("SELECT payload FROM audit_log WHERE kind = 'task.added'") as cur:
        (payload,) = [json.loads(r[0]) for r in await cur.fetchall()]
    assert payload["id"] == row["id"] and "bank" not in json.dumps(payload)


async def test_remember_to_is_a_reminder_now(rig: Rig) -> None:
    said = await rig.say("remember to call mom at 7", source="cli")
    assert said is not None and said.startswith("Reminder set")
    said = await rig.say("FLINS, set a reminder to stretch in 20 minutes", source="voice")
    assert said is not None and "stretch" in said
    assert [r["title"] for r in await rows(rig.conn)] == ["call mom", "stretch"]


async def test_a_reminder_without_a_time_asks_instead_of_guessing(rig: Rig) -> None:
    said = await rig.say("remind me to call the bank", source="cli")
    assert said is not None and said.startswith("When should I remind you?")
    assert await rows(rig.conn) == [] and rig.jobs() == []


async def test_add_list_done_delete(rig: Rig) -> None:
    assert await rig.say("add a task finish the lab report", "cli") == (
        "Added to your tasks: finish the lab report."
    )
    said = await rig.say("add buy milk to my list", "cli")
    assert said == "Added to your tasks: buy milk."
    await rig.say("remind me to call the bank tomorrow at 9am", "cli")
    listing = await rig.say("what are my tasks", "cli")
    assert listing is not None and listing.startswith("You have 3 open tasks:")
    # Timed ones first, then the rest in the order they were added.
    assert listing.index("call the bank") < listing.index("finish the lab report")
    assert listing.index("finish the lab report") < listing.index("buy milk")

    assert await rig.say("done with the lab report", "cli") == "Done: finish the lab report."
    assert await rig.say("mark buy milk as done", "cli") == "Done: buy milk."
    assert (
        await rig.say("done with the essay", "cli") == "I don't see an open task like 'the essay'."
    )
    # Just created, so deleting needs no confirmation.
    assert await rig.say("delete the reminder to call the bank", "cli") == (
        "Deleted: call the bank."
    )
    assert await rig.say("what are my tasks", "cli") == "Your task list is empty."
    assert [r["status"] for r in await rows(rig.conn)] == ["done", "done", "cancelled"]
    assert rig.jobs() == []


async def test_deleting_an_older_task_asks_first(rig: Rig) -> None:
    await rig.say("add a task renew passport", "cli")
    rig.clock.advance(timedelta(hours=2))
    asked = await rig.say("delete the task renew passport", "cli")
    assert asked is not None and asked.startswith("Delete the task 'renew passport'?")
    assert (await rows(rig.conn))[0]["status"] == "open"
    assert await rig.say("yes", "cli") == "Deleted: renew passport."
    assert (await rows(rig.conn))[0]["status"] == "cancelled"


async def test_two_matches_ask_which_one(rig: Rig) -> None:
    await rig.say("add a task call the bank", "cli")
    await rig.say("add a task call the bank about the card", "cli")
    said = await rig.say("done with call", "cli")
    assert said is not None and said.startswith("Which one? #1 call the bank; #2 ")
    assert all(r["status"] == "open" for r in await rows(rig.conn))


async def test_self_chat_notes_to_yourself_are_left_alone(rig: Rig) -> None:
    """In your notes-to-self chat, "remember to …" is your own note unless you're talking to
    FLINS. "remind me …" is always for FLINS."""
    assert await rig.say("remember to buy milk at 6", source="self_chat") is None
    assert await rig.say("add a task water plants", source="self_chat") is None
    assert await rows(rig.conn) == []
    said = await rig.say("remind me to buy milk at 6", source="self_chat")
    assert said is not None and said.startswith("Reminder set")
    said = await rig.say("FLINS, add a task water plants", source="self_chat")
    assert said == "Added to your tasks: water plants."


async def test_a_task_command_is_not_the_answer_to_an_open_question(rig: Rig) -> None:
    await rig.say("add a task renew passport", "cli")
    rig.clock.advance(timedelta(hours=2))
    await rig.say("delete the task renew passport", "cli")  # asks yes/no
    await rig.say("no", "cli")
    assert (await rows(rig.conn))[0]["status"] == "open"


# ---------------------------------------------------------------------- the scheduler


async def test_a_reminder_fires_once_when_due(store: TaskStore) -> None:
    await store.add("call the bank", source="told", channel="cli", now=T0, at=ist(10, 19))
    sink = Sink()
    kw: dict[str, Any] = {"paused": False, "focus": False, "note": sink.note, "say": sink.say}
    assert await deliver_due(store, ist(10, 18, 59), **kw) == []
    assert await deliver_due(store, ist(10, 19), **kw) == ["⏰ Reminder: call the bank"]
    assert await deliver_due(store, ist(10, 19, 1), **kw) == []  # never twice
    assert sink.notes == ["⏰ Reminder: call the bank"] and sink.spoken == [
        "Reminder: call the bank."
    ]
    (task,) = await store.open()  # still on the list until you say it's done
    assert task.remind_at is None and task.last_reminded_at == ist(10, 19)


async def test_a_broken_speaker_does_not_repeat_the_reminder(store: TaskStore) -> None:
    await store.add("call the bank", source="told", channel="cli", now=T0, at=ist(10, 19))
    sink = Sink()

    async def broken(text: str) -> None:
        raise RuntimeError("no audio device")

    kw: dict[str, Any] = {"paused": False, "focus": False, "note": sink.note, "say": broken}
    assert len(await deliver_due(store, ist(10, 19), **kw)) == 1
    assert await deliver_due(store, ist(10, 19, 1), **kw) == []
    assert len(sink.notes) == 1


async def test_reminders_wait_while_stopped_and_say_they_are_late(store: TaskStore) -> None:
    await store.add("call the bank", source="told", channel="cli", now=T0, at=ist(10, 19))
    sink = Sink()
    held = await deliver_due(
        store, ist(10, 19, 5), paused=True, focus=False, note=sink.note, say=sink.say
    )
    assert held == [] and sink.notes == []
    late = await deliver_due(
        store, ist(10, 20), paused=False, focus=False, note=sink.note, say=sink.say
    )
    assert len(late) == 1 and "(was due today" in late[0] and "19:00" in late[0]


async def test_focus_mode_writes_but_does_not_speak_unless_high_priority(
    store: TaskStore,
) -> None:
    await store.add("drink water", source="told", channel="cli", now=T0, at=ist(10, 15))
    await store.add(
        "leave for the exam", source="told", channel="cli", now=T0, at=ist(10, 15), priority="high"
    )
    sink = Sink()
    await deliver_due(store, ist(10, 15), paused=False, focus=True, note=sink.note, say=sink.say)
    assert len(sink.notes) == 2 and sink.spoken == ["Reminder: leave for the exam."]


async def test_a_repeating_reminder_moves_to_its_next_time(store: TaskStore) -> None:
    await store.add(
        "take medicine", source="told", channel="cli", now=T0, at=ist(10, 20), repeat="daily"
    )
    sink = Sink()
    kw: dict[str, Any] = {"paused": False, "focus": False, "note": sink.note, "say": None}
    assert len(await deliver_due(store, ist(10, 20), **kw)) == 1
    (task,) = await store.open()
    assert task.remind_at == ist(11, 20) and task.status == "open"
    assert await deliver_due(store, ist(10, 23), **kw) == []
    assert len(await deliver_due(store, ist(11, 20), **kw)) == 1


async def test_snooze_moves_the_last_reminder(rig: Rig) -> None:
    await rig.say("remind me to call the bank in 10 minutes", "cli")
    assert await rig.say("snooze", "cli") == "There's no recent reminder to snooze."
    tasks = TaskStore(rig.conn, TZ)
    sink = Sink()
    rig.clock.advance(timedelta(minutes=10))
    fired = await deliver_due(
        tasks, rig.clock.now(), paused=False, focus=False, note=sink.note, say=None
    )
    assert len(fired) == 1
    said = await rig.say("snooze 20 minutes", "cli")
    assert said is not None and said.startswith("Snoozed until today") and "call the bank" in said
    (task,) = await tasks.open()
    assert task.remind_at is not None
    assert task.remind_at - rig.clock.now() == timedelta(minutes=20)


async def test_done_on_a_repeating_task_keeps_it(rig: Rig) -> None:
    await rig.say("remind me to take medicine every day at 8pm", "cli")
    said = await rig.say("done with medicine", "cli")
    assert said is not None and said.startswith("Done: take medicine. Next one")
    (row,) = await rows(rig.conn)
    assert row["status"] == "open" and datetime.fromisoformat(row["remind_at"]) == ist(11, 20)


def test_the_run_loop_delivers_reminders_and_nothing_here_can_message_a_contact() -> None:
    src = REPO_ROOT / "src" / "flins"
    assert "deliver_due(" in (src / "cli.py").read_text(encoding="utf-8")
    for name in ("tasks.py", "task_commands.py", "timeparse.py"):
        text = (src / "assistant" / name).read_text(encoding="utf-8")
        assert "send_text" not in text and "outbox" not in text.lower(), name


# ---------------------------------------------------------------------- the model sees the list


async def test_open_tasks_reach_the_conversation_model(rig: Rig) -> None:
    await rig.say("remind me to call the bank at 7", "cli")
    await rig.say("add a task finish <the> report", "cli")
    rig.answers(chat=chat("Call the bank at 7, then the report."))
    assert await rig.say("what should I work on this evening?", "cli") == (
        "Call the bank at 7, then the report."
    )
    ctx = rig.last_context()
    assert "<tasks>\n- call the bank (today 19:00)\n- finish " in ctx
    block = ctx.split("<tasks>")[1].split("</tasks>")[0]
    assert "report" in block and "<" not in block and ">" not in block  # can't open a block


def test_chat_prompt_v3_knows_about_tasks() -> None:
    p = load_prompt(REPO_ROOT / "prompts", "chat")
    assert p.version == "chat_v3" and "<tasks>" in p.system and "remind me to" in p.system
    assert "reminders, tasks, calendar" not in p.system  # no longer "not available"
    assert "you cannot create it from here" in p.system


# ---------------------------------------------------------------------- CLI and dashboard


def test_cli_tasks(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = ["--root", str(project)]
    assert cli.main([*root, "tasks", "add", "renew", "passport", "tomorrow", "at", "10am"]) == 0
    assert "Added #1: renew passport (tomorrow 10:00)" in capsys.readouterr().out
    assert cli.main([*root, "tasks", "add", "buy milk"]) == 0
    assert cli.main([*root, "tasks"]) == 0
    out = capsys.readouterr().out
    assert "2 open tasks." in out and "#2 buy milk" in out
    assert cli.main([*root, "tasks", "done", "2"]) == 0
    assert cli.main([*root, "tasks", "delete", "1"]) == 0
    assert cli.main([*root, "tasks", "done", "9"]) == 1
    assert cli.main([*root, "tasks", "done"]) == 2
    capsys.readouterr()
    assert cli.main([*root, "tasks"]) == 0
    out = capsys.readouterr().out
    assert "0 open tasks." in out and "Recently done:" in out


def test_dashboard_tasks_page(project: Path) -> None:
    settings = load_settings(project)
    app = create_app(
        settings.db_path,
        settings.policy,
        clock=FrozenClock(T0),
        port=8765,
        token="T",
        settings=settings,
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = "T"
        assert client.get("/tasks").status_code == 200
        r = client.post("/tasks", data={"action": "add", "text": "pay rent on Friday"})
        assert r.status_code == 422  # no token
        r = client.post(
            "/tasks",
            data={"action": "add", "text": "pay rent on Friday", "token": token},
            follow_redirects=False,
        )
        assert r.status_code == 303
        page = client.get("/tasks").text
        assert "pay rent (Fri 13 Nov 09:00)" in page
        client.post("/tasks", data={"action": "done", "task_id": "1", "token": token})
        page = client.get("/tasks").text
        assert "Nothing on your list." in page and "✓ pay rent" in page
        bad = client.post("/tasks", data={"action": "add", "text": "x", "token": "wrong"})
        assert bad.status_code == 403


def test_task_commands_need_no_gateway() -> None:
    """The command class takes a store and the yes/no machinery: no model anywhere."""
    import inspect

    assert list(inspect.signature(TaskCommands.__init__).parameters) == [
        "self",
        "tasks",
        "confirmer",
    ]
