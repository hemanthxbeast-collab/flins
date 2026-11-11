"""date_math: the clock and the calendar, so dates are computed and never guessed.

    now [<IANA timezone>]
    between <date> and <date>
    add <n> days|weeks|months|years to <date>      (n may be negative)
    weekday <date>

A date is YYYY-MM-DD, or today / tomorrow / yesterday.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flins.assistant.tools.base import Tool, ToolResult, fail, ok

USAGE = (
    "one of: 'now', 'now Asia/Tokyo' (an IANA timezone), 'between 2026-11-11 and 2026-12-25', "
    "'add 45 days to 2026-11-11' (days, weeks, months or years; negative to go back), "
    "'weekday 2026-12-25'. Dates are YYYY-MM-DD, or today / tomorrow / yesterday."
)
_DATE = r"(\d{4}-\d{2}-\d{2}|today|tomorrow|yesterday)"
_NOW = re.compile(r"^now(?:\s+(?:in\s+)?(?P<tz>[A-Za-z_]+(?:/[A-Za-z_+\-0-9]+)*))?$", re.I)
_BETWEEN = re.compile(rf"^(?:between|from)\s+{_DATE}\s+(?:and|to|until)\s+{_DATE}$", re.I)
_ADD = re.compile(
    rf"^(?P<op>add|subtract)\s+(?P<n>-?\d{{1,5}})\s+(?P<u>day|week|month|year)s?\s+"
    rf"(?:to|from)\s+{_DATE}$",
    re.I,
)
_WEEKDAY = re.compile(rf"^weekday(?:\s+of)?\s+{_DATE}$", re.I)


class DateError(ValueError):
    pass


def _date(word: str, today: date) -> date:
    w = word.lower()
    if w in ("today", "tomorrow", "yesterday"):
        return today + timedelta(days={"today": 0, "tomorrow": 1, "yesterday": -1}[w])
    try:
        return date.fromisoformat(word)
    except ValueError:
        raise DateError(f"'{word}' is not a real date") from None


def add_months(d: date, months: int) -> date:
    """Calendar months; the day is clamped (31 Jan + 1 month = 28/29 Feb)."""
    index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(index, 12)
    if not 1 <= year <= 9999:
        raise DateError("that date is out of range")
    return date(year, month + 1, min(d.day, calendar.monthrange(year, month + 1)[1]))


def _show(d: date) -> str:
    return f"{d:%A %d %B %Y} ({d.isoformat()})"


def compute(arg: str, now: datetime, tz: ZoneInfo) -> str:
    text = " ".join(arg.split()).strip(" .?")
    local = now.astimezone(tz)
    today = local.date()

    m = _NOW.match(text)
    if m:
        zone = tz
        if m.group("tz"):
            try:
                zone = ZoneInfo(m.group("tz"))
            except (ZoneInfoNotFoundError, ValueError):
                raise DateError(
                    f"'{m.group('tz')}' is not a timezone name I know (use e.g. Europe/London)"
                ) from None
        there = now.astimezone(zone)
        return f"It is {there:%A %d %B %Y, %H:%M} in {zone.key}."
    m = _BETWEEN.match(text)
    if m:
        a, b = _date(m.group(1), today), _date(m.group(2), today)
        days = (b - a).days
        n = abs(days)
        weeks = f" ({n // 7} weeks and {n % 7} days)" if n >= 14 else ""
        direction = "after" if days >= 0 else "before"
        return f"{_show(b)} is {n} day{'s' if n != 1 else ''}{weeks} {direction} {_show(a)}."
    m = _ADD.match(text)
    if m:
        n = int(m.group("n")) * (-1 if m.group("op").lower() == "subtract" else 1)
        start = _date(m.group(4), today)
        unit = m.group("u").lower()
        try:
            if unit == "day":
                end = start + timedelta(days=n)
            elif unit == "week":
                end = start + timedelta(weeks=n)
            else:
                end = add_months(start, n * (12 if unit == "year" else 1))
        except OverflowError:
            raise DateError("that date is out of range") from None
        return f"{n} {unit}{'s' if abs(n) != 1 else ''} from {_show(start)} is {_show(end)}."
    m = _WEEKDAY.match(text)
    if m:
        d = _date(m.group(1), today)
        return f"{d.isoformat()} is a {d:%A}."
    raise DateError(f"I couldn't read that. Use {USAGE}")


def make_date_tool(tz: ZoneInfo) -> Tool:
    async def run(arg: str, now: datetime) -> ToolResult:
        try:
            return ok(compute(arg or "now", now, tz))
        except DateError as exc:
            return fail(str(exc))

    return Tool(
        name="date_math",
        description=(
            "The time in another timezone, days between two dates, a date some days / weeks / "
            "months away, or the weekday of a date. Use it instead of counting days yourself."
        ),
        usage=USAGE,
        risk="read",
        run=run,
    )
