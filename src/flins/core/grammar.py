"""Tier-A command grammar (SPEC §4.1). Deterministic, no LLM.

Shared by the CLI, the WhatsApp self-chat and (from M6) voice. A command must be the whole
message: "focus mode for 1h" is a command, "I'll be in focus mode later" is not. That keeps
ordinary notes-to-self from flipping modes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from flins.core.modes import ModeEvent

# ------------------------------------------------------------------ command types


@dataclass(frozen=True)
class ModeCommand:
    event: ModeEvent
    until: datetime | None = None
    profile: str | None = None


@dataclass(frozen=True)
class StatusCommand:
    pass


@dataclass(frozen=True)
class ApprovalCommand:
    op: Literal["approve", "reject", "edit", "list"]
    number: int | None = None
    text: str | None = None


@dataclass(frozen=True)
class ParseError:
    """Looked like a command but part of it couldn't be understood. `message` is user-facing."""

    message: str


Command = ModeCommand | StatusCommand | ApprovalCommand

# ------------------------------------------------------------------ phrases

_ENTER: dict[ModeEvent, list[str]] = {
    ModeEvent.ENTER_WORKING: [
        r"(?:i'?m|i am) (?:in )?(?:a )?work(?:ing)? mood",
        r"work(?:ing)? mood",
        r"work(?:ing)? mode",
        r"(?:i'?m|i am) working(?: now)?",
        r"(?:start |begin )?working",
        r"mode work(?:ing)?",
    ],
    ModeEvent.ENTER_FOCUS: [
        r"focus(?: mode)?",
        r"(?:i'?m|i am) focus(?:ing|ed)",
        r"do not disturb",
        r"dnd",
        r"deep work(?: mode)?",
        r"mode focus",
    ],
    ModeEvent.ENTER_AWAY: [
        r"(?:i'?m |i am )?heading out",
        r"(?:i'?m|i am) away",
        r"(?:i'?m |i am )?(?:leaving|going out|stepping out)(?: now)?",
        r"away(?: mode)?",
        r"mode away",
    ],
}
_EXIT = [
    r"(?:i'?m|i am) done(?: working| for (?:today|now|the day))?",
    r"done(?: working| for (?:today|now|the day))?",
    r"(?:i'?m|i am) back",
    r"back to normal",
    r"normal(?: mode)?",
    r"mode normal",
]
_KILL = [r"stop", r"pause", r"stop replying", r"pause everything", r"stop everything", r"kill"]
_RESUME = [r"resume", r"unpause", r"continue replying"]
_CANCEL = [r"cancel", r"undo"]
_STATUS = [r"status", r"mode", r"what mode(?: am i in)?", r"which mode", r"mode status"]

# Optional "FLINS," / "hey FLINS" in front and "please" / punctuation at the end.
_PREFIX = re.compile(r"^(?:(?:hey|ok|okay)\s+)?flins[\s,:]+", re.IGNORECASE)
_SUFFIX = re.compile(r"[\s,]*(?:please|pls|thanks|thank you)?[\s.!?]*$", re.IGNORECASE)

_TIME_TAIL = re.compile(
    r"^(?P<head>.*?)\s+(?P<time>(?:till|until|til|untill|upto|up to|to|for|by)\s+.+"
    r"|\d+\s*(?:h|hr|hrs|hours?|m|min|mins|minutes?)(?:\s*\d+\s*(?:m|min|mins|minutes?))?)$"
)
_PROFILE_TAIL = re.compile(r"^(?P<head>.*?)[\s,]+(?P<profile>strict|open)$")
_EXTEND = re.compile(r"^(?:extend|keep it on|keep going)(?:\s+(?P<time>.+))?$")

_APPROVE = re.compile(r"^(?:y|yes|approve|send(?: it)?|ok)\s*#?\s*(?P<n>\d+)?$")
_REJECT = re.compile(r"^(?:n|no|reject|don'?t send|skip)\s*#?\s*(?P<n>\d+)?$")
_EDIT = re.compile(r"^(?:e|edit)\s*#?\s*(?P<n>\d+)\s*[:\-]?\s+(?P<text>.+)$", re.DOTALL)
_PENDING = re.compile(r"^(?:read |show |list )?(?:pending|approvals|what'?s pending)$")


# Phones type curly apostrophes (U+2019, U+2018); treat them as straight ones.
_CURLY_QUOTES = str.maketrans({0x2019: "'", 0x2018: "'"})


def _normalise(text: str) -> str:
    t = text.strip().translate(_CURLY_QUOTES)
    t = _PREFIX.sub("", t)
    t = _SUFFIX.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def _full(patterns: list[str], text: str) -> bool:
    return any(re.fullmatch(p, text) for p in patterns)


# ------------------------------------------------------------------ time parsing

_DUR = re.compile(
    r"^(?:for\s+)?(?:(?P<h>\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hours?))?\s*(?:and\s+)?"
    r"(?:(?P<m>\d+)\s*(?:m|min|mins|minutes?))?$"
)
_HALF = re.compile(r"^(?:for\s+)?(?:an?\s+)?(?P<n>half an|an|a|one|two|three|four)\s+hours?$")
_CLOCK = re.compile(
    r"^(?:till|until|til|untill|upto|up to|to|by)\s+"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm|a\.m\.|p\.m\.)?$"
)
_WORD_HOURS = {"half an": 0.5, "an": 1, "a": 1, "one": 1, "two": 2, "three": 3, "four": 4}


def parse_time(phrase: str, now: datetime, tz: ZoneInfo) -> datetime | None:
    """Parse 'for 2 hours', '90m', 'till 6pm', 'until 18:30' or 'till 6' (the next 6 o'clock).

    Returns None if unclear, so the caller can ask instead of guessing.
    """
    p = phrase.strip().lower()
    m = _HALF.fullmatch(p)
    if m:
        return now + timedelta(hours=_WORD_HOURS[m.group("n")])
    m = _DUR.fullmatch(p)
    if m and (m.group("h") or m.group("m")):
        return now + timedelta(hours=float(m.group("h") or 0), minutes=int(m.group("m") or 0))
    m = _CLOCK.fullmatch(p)
    if m:
        hour, minute = int(m.group("h")), int(m.group("m") or 0)
        ap = (m.group("ap") or "").replace(".", "")
        if minute > 59 or hour > 23 or (ap and not 1 <= hour <= 12):
            return None
        local_now = now.astimezone(tz)
        if ap:
            hour = hour % 12 + (12 if ap == "pm" else 0)
            candidates = [hour]
        elif hour <= 12:
            candidates = [hour % 12, hour % 12 + 12]  # "till 6" = next 6 o'clock, am or pm
        else:
            candidates = [hour]
        best: datetime | None = None
        for h in candidates:
            t = local_now.replace(hour=h, minute=minute, second=0, microsecond=0)
            if t <= local_now:
                t += timedelta(days=1)
            if best is None or t < best:
                best = t
        return best
    return None


# ------------------------------------------------------------------ parse


def parse(text: str, *, now: datetime, tz: ZoneInfo) -> Command | ParseError | None:
    """Parse one message. Returns a command, a ParseError, or None (not a command at all)."""
    t = _normalise(text).lower()
    if not t:
        return None

    # Edited text is sent exactly as typed: only drop a leading "FLINS,", never trailing
    # punctuation or "please"/"thanks" (those are part of the message).
    m = _EDIT.fullmatch(_PREFIX.sub("", text.strip()))
    if m:
        return ApprovalCommand("edit", int(m.group("n")), m.group("text").strip())
    for op, rx in (("approve", _APPROVE), ("reject", _REJECT)):
        m = rx.fullmatch(t)
        if m:
            n = m.group("n")
            # A bare "ok"/"yes"/"no" with no number is conversation, not a command.
            if n is None and t in {"ok", "yes", "no", "y", "n"}:
                return None
            return ApprovalCommand(op, int(n) if n else None)  # type: ignore[arg-type]
    if _PENDING.fullmatch(t):
        return ApprovalCommand("list")

    if _full(_STATUS, t):
        return StatusCommand()
    if _full(_KILL, t):
        return ModeCommand(ModeEvent.KILL)
    if _full(_RESUME, t):
        return ModeCommand(ModeEvent.RESUME)
    if _full(_CANCEL, t):
        return ModeCommand(ModeEvent.CANCEL)
    if _full(_EXIT, t):
        return ModeCommand(ModeEvent.EXIT_TO_NORMAL)

    m = _EXTEND.fullmatch(t)
    if m:
        if not m.group("time"):
            return ParseError("Extend until when? Try 'extend 1 hour' or 'extend till 7pm'.")
        until = parse_time(m.group("time"), now, tz)
        if until is None:
            return ParseError(f"I didn't understand the time {m.group('time')!r}.")
        return ModeCommand(ModeEvent.EXTEND, until=until)

    head, profile = t, None
    pm = _PROFILE_TAIL.fullmatch(head)
    if pm:
        head, profile = pm.group("head"), pm.group("profile")
    time_phrase = None
    tm = _TIME_TAIL.fullmatch(head)
    if tm and any(_full(ps, tm.group("head")) for ps in _ENTER.values()):
        head, time_phrase = tm.group("head"), tm.group("time")
    if profile is None:
        pm = _PROFILE_TAIL.fullmatch(head)
        if pm:
            head, profile = pm.group("head"), pm.group("profile")

    for event, phrases in _ENTER.items():
        if _full(phrases, head):
            until = None
            if time_phrase is not None:
                until = parse_time(time_phrase, now, tz)
                if until is None:
                    return ParseError(f"I didn't understand the time {time_phrase!r}.")
            mode_word = event.value.removeprefix("ENTER_").lower()
            return ModeCommand(
                event, until=until, profile=f"{mode_word}-{profile}" if profile else None
            )
    return None
