"""Time phrases for tasks and reminders: "at 7", "in 20 minutes", "tomorrow at 9",
"on Friday evening", "every weekday at 8".

`extract(text, now, tz)` pulls the time out of a sentence and returns what is left as the
title. Nothing is guessed: if there is no clear time, `at` is None and the caller asks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

Repeat = Literal["none", "daily", "weekdays", "weekly"]

_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DAY = "|".join(_DAYS)
_PART_HOUR = {"morning": 9, "noon": 12, "afternoon": 15, "evening": 18, "night": 21, "tonight": 21}
_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10,
        "fifteen": 15, "twenty": 20, "thirty": 30, "half an": 0.5}  # fmt: skip
_UNIT = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 604800}

_RELATIVE = re.compile(
    r"\b(?:in|after)\s+(?P<n>\d+(?:\.\d+)?|half an|an?|one|two|three|four|five|ten|fifteen|twenty"
    r"|thirty)\s+(?P<u>minute|min|hour|hr|day|week)s?\b",
    re.I,
)
_EVERY = re.compile(
    rf"\b(?:every\s?(?P<what>day|weekday|morning|evening|night|week|{_DAY})s?|(?P<daily>daily))\b",
    re.I,
)
_DAYWORD = re.compile(
    rf"\b(?P<dat>day after tomorrow)|\b(?P<tom>tomorrow|tmrw)|\b(?P<tod>today|tonight)"
    rf"|\b(?:on\s+|this\s+|next\s+)?(?P<wd>{_DAY})\b",
    re.I,
)
_CLOCK = re.compile(
    r"\b(?:at|by|around|@)\s*(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?)?"
    r"(?:\s*o'?clock)?(?![\w:])"
    r"|\b(?P<h2>\d{1,2})(?:[:.](?P<m2>\d{2}))?\s*(?P<ap2>a\.?m\.?|p\.?m\.?)(?![\w])"
    r"|\b(?P<h3>\d{1,2}):(?P<m3>\d{2})\b",
    re.I,
)
_PART = re.compile(
    r"\b(?:in the |this |at )?(?P<p>morning|noon|afternoon|evening|night|tonight)\b", re.I
)
_TIDY = re.compile(
    r"\s+(?:at|on|by|in|every|around)\s*$|^\s*(?:to|that i should|that i need to)\s+", re.I
)


@dataclass(frozen=True)
class When:
    title: str  # the sentence with the time phrase taken out
    at: datetime | None  # None = no clear time was given
    repeat: Repeat = "none"


def _cut(text: str, spans: list[tuple[int, int]]) -> str:
    out, last = [], 0
    for a, b in sorted(spans):
        out.append(text[last:a])
        last = max(last, b)
    out.append(text[last:])
    title = re.sub(r"\s{2,}", " ", "".join(out)).strip(" ,.-")
    for _ in range(3):
        title = _TIDY.sub("", title).strip(" ,.-")
    return title


def _hour(h: int, ap: str | None) -> int | None:
    if h > 23 or (ap and not 1 <= h <= 12):
        return None
    if ap:
        return h % 12 + (12 if ap.lower().startswith("p") else 0)
    return h


def extract(text: str, now: datetime, tz: ZoneInfo) -> When:
    local = now.astimezone(tz)
    spans: list[tuple[int, int]] = []
    repeat: Repeat = "none"
    part: str | None = None

    m = _RELATIVE.search(text)
    if m:
        n = m.group("n").lower()
        amount = float(n) if n[0].isdigit() else float(_NUM[n])
        at = local + timedelta(seconds=amount * _UNIT[m.group("u").lower()])
        return When(_cut(text, [m.span()]), at)

    weekday: int | None = None
    every = _EVERY.search(text)
    if every:
        spans.append(every.span())
        what = (every.group("what") or "day").lower()
        if what in _DAYS:
            repeat, weekday = "weekly", _DAYS.index(what)
        elif what == "weekday":
            repeat = "weekdays"
        elif what == "week":
            repeat = "weekly"
        else:
            repeat = "daily"
            if what in _PART_HOUR:
                part = what

    days = 0
    explicit_day = False
    d = _DAYWORD.search(text)
    if d and not (every and d.start() >= every.start() and d.end() <= every.end()):
        spans.append(d.span())
        explicit_day = True
        if d.group("dat"):
            days = 2
        elif d.group("tom"):
            days = 1
        elif d.group("tod"):
            if d.group("tod").lower() == "tonight":
                part = "tonight"
        else:
            weekday = _DAYS.index(d.group("wd").lower())
    if weekday is not None and repeat != "weekdays":
        days = (weekday - local.weekday()) % 7
        explicit_day = True

    hour: int | None = None
    minute = 0
    ampm = False
    c = _CLOCK.search(text)
    if c:
        h = c.group("h") or c.group("h2") or c.group("h3")
        mm = c.group("m") or c.group("m2") or c.group("m3")
        ap = c.group("ap") or c.group("ap2")
        hour = _hour(int(h), ap)
        minute = int(mm or 0)
        ampm = bool(ap) or (bool(c.group("h3")) and int(h) > 12)
        if hour is None or minute > 59:
            return When(text.strip(), None)
        spans.append(c.span())
    p = _PART.search(text)
    if p and not any(a <= p.start() < b for a, b in spans):
        part = p.group("p").lower()
        spans.append(p.span())

    if hour is None and part is None and not explicit_day and repeat == "none":
        return When(text.strip(), None)

    if hour is None:
        hour = _PART_HOUR[part] if part else 9  # "tomorrow" alone = tomorrow morning
    elif not ampm and hour <= 12:
        if part in ("afternoon", "evening", "night", "tonight"):
            hour = hour % 12 + 12
        elif part == "morning":
            hour = hour % 12
        elif (explicit_day and days > 0) or repeat != "none":
            hour = hour + 12 if 1 <= hour <= 6 else hour  # "tomorrow at 5" = 5 pm, "at 9" = 9 am
        else:
            # "at 7" today: the next 7 o'clock, morning or evening.
            options = sorted({hour % 12, hour % 12 + 12})
            later = [h for h in options if (h, minute) > (local.hour, local.minute)]
            hour = later[0] if later else options[0]

    at = local.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days)
    if repeat == "weekdays":
        while at <= local or at.weekday() >= 5:
            at += timedelta(days=1)
    elif at <= local:
        at += timedelta(days=7 if weekday is not None else 1)
    return When(_cut(text, spans), at, repeat)


def next_occurrence(at: datetime, repeat: str, now: datetime) -> datetime | None:
    """When a repeating reminder fires next after `now` (same local clock time)."""
    if repeat == "none":
        return None
    step = timedelta(days=7 if repeat == "weekly" else 1)
    nxt = at + step
    while nxt <= now or (repeat == "weekdays" and nxt.weekday() >= 5):
        nxt += step if repeat != "weekdays" else timedelta(days=1)
    return nxt


def describe(at: datetime, now: datetime, tz: ZoneInfo) -> str:
    """'today 19:00', 'tomorrow 09:00', 'Fri 14 Nov 17:00'."""
    a, n = at.astimezone(tz), now.astimezone(tz)
    gap = (a.date() - n.date()).days
    if gap == 0:
        return f"today {a:%H:%M}"
    if gap == 1:
        return f"tomorrow {a:%H:%M}"
    return f"{a:%a} {a.day} {a:%b %H:%M}"
