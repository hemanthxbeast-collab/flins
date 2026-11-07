from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from flins.core.grammar import (
    ApprovalCommand,
    ModeCommand,
    ParseError,
    StatusCommand,
    parse,
    parse_time,
)
from flins.core.modes import ModeEvent

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 9, 4, 30, tzinfo=UTC)  # 10:00 IST


def p(text: str) -> object:
    return parse(text, now=NOW, tz=IST)


def local(h: int, m: int = 0, day: int = 9) -> datetime:
    return datetime(2026, 11, day, h, m, tzinfo=IST)


@pytest.mark.parametrize(
    ("text", "event"),
    [
        ("I'm in working mood", ModeEvent.ENTER_WORKING),
        ("i’m in working mood!", ModeEvent.ENTER_WORKING),  # noqa: RUF001 (curly apostrophe on purpose)
        ("Im in work mood", ModeEvent.ENTER_WORKING),
        ("working mode", ModeEvent.ENTER_WORKING),
        ("FLINS, I'm working now", ModeEvent.ENTER_WORKING),
        ("hey flins working mood please", ModeEvent.ENTER_WORKING),
        ("focus mode", ModeEvent.ENTER_FOCUS),
        ("Do not disturb", ModeEvent.ENTER_FOCUS),
        ("deep work", ModeEvent.ENTER_FOCUS),
        ("heading out", ModeEvent.ENTER_AWAY),
        ("I'm away", ModeEvent.ENTER_AWAY),
        ("I'm done", ModeEvent.EXIT_TO_NORMAL),
        ("done for today", ModeEvent.EXIT_TO_NORMAL),
        ("back to normal", ModeEvent.EXIT_TO_NORMAL),
        ("I'm back.", ModeEvent.EXIT_TO_NORMAL),
        ("STOP", ModeEvent.KILL),
        ("FLINS stop", ModeEvent.KILL),
        ("stop replying", ModeEvent.KILL),
        ("pause everything", ModeEvent.KILL),
        ("flins resume", ModeEvent.RESUME),
        ("cancel", ModeEvent.CANCEL),
        ("undo", ModeEvent.CANCEL),
    ],
)
def test_mode_phrases(text: str, event: ModeEvent) -> None:
    cmd = p(text)
    assert isinstance(cmd, ModeCommand)
    assert cmd.event == event


@pytest.mark.parametrize(
    "text",
    [
        "I'm leaving at 5 for the airport",
        "remember to stop at the shop",
        "working on the report",
        "mom said she is away this week",
        "I'm done with the dishes",
        "focus on the slides tomorrow",
        "ok",
        "yes",
        "no",
        "cancel the meeting with Rahul",
        "",
        "   ",
        "https://example.com",
    ],
)
def test_ordinary_sentences_are_not_commands(text: str) -> None:
    assert p(text) is None


@pytest.mark.parametrize(
    ("text", "until"),
    [
        ("I'm in working mood till 6pm", local(18)),
        ("working mood until 18:30", local(18, 30)),
        ("working mood till 6", local(18)),  # next 6 o'clock after 10:00 is 18:00
        ("working mood till 9", local(21)),
        ("working mood till 11", local(11)),
        ("working mood till 9am", local(9, day=10)),
        ("focus mode for 90 minutes", local(11, 30)),
        ("focus mode for 2 hours", local(12)),
        ("focus mode for an hour", local(11)),
        ("focus mode for half an hour", local(10, 30)),
        ("focus mode 1h30m", local(11, 30)),
        ("heading out for 45 min", local(10, 45)),
    ],
)
def test_durations(text: str, until: datetime) -> None:
    cmd = p(text)
    assert isinstance(cmd, ModeCommand), cmd
    assert cmd.until == until


def test_profile_suffix() -> None:
    cmd = p("working mood, strict")
    assert isinstance(cmd, ModeCommand)
    assert (cmd.event, cmd.profile) == (ModeEvent.ENTER_WORKING, "working-strict")
    cmd = p("working mood till 5pm, strict")
    assert isinstance(cmd, ModeCommand)
    assert (cmd.until, cmd.profile) == (local(17), "working-strict")


def test_bad_time_is_a_parse_error_not_a_silent_default() -> None:
    assert isinstance(p("working mood till lunchtime"), ParseError)
    assert isinstance(p("working mood till 25:00"), ParseError)
    assert isinstance(p("extend"), ParseError)


def test_extend() -> None:
    cmd = p("extend 1 hour")
    assert isinstance(cmd, ModeCommand)
    assert cmd.event == ModeEvent.EXTEND and cmd.until == NOW + timedelta(hours=1)
    cmd = p("extend till 7pm")
    assert isinstance(cmd, ModeCommand) and cmd.until == local(19)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("y3", ApprovalCommand("approve", 3)),
        ("approve 2", ApprovalCommand("approve", 2)),
        ("send it", ApprovalCommand("approve", None)),
        ("n 4", ApprovalCommand("reject", 4)),
        ("reject #1", ApprovalCommand("reject", 1)),
        ("e3 Almost done, sending by 5", ApprovalCommand("edit", 3, "Almost done, sending by 5")),
        ("edit 2: Call you at 7", ApprovalCommand("edit", 2, "Call you at 7")),
        ("read pending", ApprovalCommand("list")),
        ("status", StatusCommand()),
        ("what mode am I in?", StatusCommand()),
    ],
)
def test_approvals_and_status(text: str, expected: object) -> None:
    assert p(text) == expected


def test_parse_time_rejects_nonsense() -> None:
    for phrase in ["soon", "till later", "for ever", "until 13pm"]:
        assert parse_time(phrase, NOW, IST) is None
