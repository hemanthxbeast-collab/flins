"""Fixed text FLINS says back after a mode change or a status request. No LLM involved."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from flins.core.levels import Mode

if TYPE_CHECKING:
    from flins.core.modes import ModeEvent, ModeState

ACTION_WORDS = {
    "auto": "drafted for your approval",
    "auto_safe": "got a holding reply",
    "draft": "drafted for your approval",
    "notify": "flagged for you",
    "ignore": "ignored",
}
MAX_ITEMS = 10


@dataclass(frozen=True)
class Debrief:
    actions: dict[str, int] = field(default_factory=dict)
    escalations: int = 0
    suspicious: int = 0
    items: tuple[str, ...] = ()  # one line per message, urgent first

    @property
    def total(self) -> int:
        return sum(self.actions.values())


@dataclass(frozen=True)
class Context:
    tz: ZoneInfo
    now: datetime
    dry_run: bool
    auto_contacts: int
    debrief: Debrief | None


def clock_time(dt: datetime, ctx: Context) -> str:
    local = dt.astimezone(ctx.tz)
    if local.date() == ctx.now.astimezone(ctx.tz).date():
        return local.strftime("%H:%M")
    return local.strftime("%a %H:%M")


def _mode_name(mode: Mode) -> str:
    return mode.value.capitalize()


def _dry(ctx: Context) -> str:
    return " (Dry run: nothing will be sent.)" if ctx.dry_run else ""


def _debrief_text(d: Debrief) -> str:
    if d.total == 0:
        return "No messages came in."
    parts = [f"{n} {ACTION_WORDS.get(a, a)}" for a, n in sorted(d.actions.items()) if n]
    text = f"{d.total} message{'s' if d.total != 1 else ''}: " + ", ".join(parts) + "."
    if d.escalations:
        text += f" {d.escalations} marked urgent."
    if d.suspicious:
        text += f" {d.suspicious} looked like a scam (asked for a code or password)."
    if d.items:
        shown = list(d.items[:MAX_ITEMS])
        if len(d.items) > MAX_ITEMS:
            shown.append(f"…and {len(d.items) - MAX_ITEMS} more on the dashboard.")
        text += "\n" + "\n".join(f"• {line}" for line in shown)
    return text


def transition(
    event: ModeEvent, before: ModeState, after: ModeState, changed: bool, ctx: Context
) -> str:
    from flins.core.modes import ModeEvent

    if not changed:
        if after.mode == Mode.PAUSED:
            return "Already paused."
        return f"Already in {after.mode.value.lower()} mode."

    if after.mode == Mode.PAUSED:
        return "Paused. FLINS won't reply to anyone until you say 'resume'."

    if event == ModeEvent.EXTEND and after.until:
        return f"{_mode_name(after.mode)} mode extended until {clock_time(after.until, ctx)}."

    if event in (ModeEvent.RESUME, ModeEvent.CANCEL):
        word = "Resumed" if event == ModeEvent.RESUME else "Cancelled"
        if after.mode == Mode.NORMAL:
            return f"{word}. Back to normal."
        assert after.until is not None
        return f"{word}. {_mode_name(after.mode)} mode until {clock_time(after.until, ctx)}."

    if after.mode == Mode.NORMAL:
        lead = (
            f"{_mode_name(before.mode)} mode ended."
            if event == ModeEvent.EXPIRE
            else "Back to normal."
        )
        if ctx.debrief is not None and before.mode != Mode.PAUSED:
            return f"{lead} {_debrief_text(ctx.debrief)}"
        return lead

    assert after.until is not None
    profile = f" ({after.profile})" if after.profile else ""
    who = (
        f"I may reply to {ctx.auto_contacts} of your close contacts by myself; "
        "everything else waits for you."
        if ctx.auto_contacts
        else "Everything waits for you."
    )
    return (
        f"{_mode_name(after.mode)} mode{profile} on until {clock_time(after.until, ctx)}. "
        f"{who} Say 'cancel' within 10 seconds to undo.{_dry(ctx)}"
    )


def status(state: ModeState, ctx: Context) -> str:
    if state.mode == Mode.PAUSED:
        return "Paused. Say 'resume' to continue."
    if state.mode == Mode.NORMAL or state.until is None:
        return f"Normal mode.{_dry(ctx)}"
    profile = f" ({state.profile})" if state.profile else ""
    if state.until <= ctx.now:
        return f"{_mode_name(state.mode)} mode has ended; switching back to normal."
    return (
        f"{_mode_name(state.mode)} mode{profile} until {clock_time(state.until, ctx)}.{_dry(ctx)}"
    )


def expiring(state: ModeState, tz: ZoneInfo) -> str:
    assert state.until is not None
    local = state.until.astimezone(tz).strftime("%H:%M")
    return (
        f"{_mode_name(state.mode)} mode ends at {local}. "
        "Say 'extend 1 hour' (or a time) to keep it on."
    )
