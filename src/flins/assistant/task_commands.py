"""Task and reminder commands: fixed phrases, no AI model.

    remind me to call the bank at 7        (also: remember to …, set a reminder to …)
    add a task finish the report [tomorrow at 5]
    what are my tasks / what's on my list / what's due
    done with the report / mark the report done
    delete the reminder to call the bank
    snooze [20 minutes]

A time FLINS can't read is never guessed: it asks for it. Reminders only ever go to you.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from flins.assistant.memory_commands import Confirmer
from flins.assistant.tasks import Task, TaskStore
from flins.assistant.timeparse import describe, extract
from flins.core.grammar import strip_address

I = re.IGNORECASE  # noqa: E741
# "remind me …" is always meant for FLINS. "remember to …" could be a note to yourself, so in
# your self-chat it only counts when you are talking to FLINS (the core decides that).
_REMIND = re.compile(
    r"^(?:please\s+)?(?:remind me(?:\s+(?:to|about|that))?|set (?:a |an )?(?:reminder|alarm)"
    r"(?:\s+(?:to|for|about))?)\s+(?P<x>.+)$",
    I | re.DOTALL,
)
_REMEMBER_TO = re.compile(r"^(?:remember|don'?t (?:let me )?forget) to\s+(?P<x>.+)$", I | re.DOTALL)
_ADD = re.compile(
    r"^(?:add|create|new|make)(?: a| an| me a)? (?:task|todo|to-do)(?:\s+(?:to|for))?[:,]?\s+"
    r"(?P<x>.+)$"
    r"|^add (?P<y>.+?) to (?:my |the )?(?:tasks?|to-?do(?: list)?|list)$",
    I | re.DOTALL,
)
_LIST = re.compile(
    r"^(?:what(?:'s| is| are)? (?:on )?my (?:tasks?|to-?dos?|to-?do list|list|reminders?)"
    r"|(?:show|list|read)(?: me)? (?:my |the )?(?:tasks?|to-?dos?|to-?do list|reminders?)"
    r"|what(?:'s| is) due(?: today| now)?|what do i have (?:to do|pending|due)(?: today)?"
    r"|my (?:tasks|reminders|to-?do list))$",
    I,
)
_DONE = re.compile(
    r"^(?:(?:i'?m |i am )?done with|(?:i(?:'ve| have)? )?(?:finished|completed)|i did)\s+(?P<x>.+)$"
    r"|^mark\s+(?P<y>.+?)\s+(?:as\s+)?(?:done|complete|completed|finished)$"
    r"|^(?P<z>.+?)\s+is (?:done|finished|complete)$",
    I,
)
_DELETE = re.compile(
    r"^(?:delete|remove|cancel|drop) (?:the |my )?(?:task|reminder|todo|to-do|alarm)"
    r"(?:\s+(?:to|about|for|called))?\s+(?P<x>.+)$",
    I,
)
_SNOOZE = re.compile(r"^snooze(?: (?:it|that|this))?(?:\s+(?:for|by|until|till))?\s*(?P<x>.*)$", I)

RECENT = timedelta(minutes=15)
WHEN = "When should I remind you? Say it with a time, like 'remind me to {title} at 7'."


def _clean(title: str) -> str:
    return title.strip().strip(".!?\"'").strip()


class TaskCommands:
    def __init__(self, tasks: TaskStore, confirmer: Confirmer) -> None:
        self._tasks = tasks
        self._confirmer = confirmer

    async def handle(self, text: str, source: str, now: datetime, *, addressed: bool) -> str | None:
        """Reply text if `text` is a task command, else None. `addressed` is False for a
        self-chat message that wasn't clearly meant for FLINS."""
        t = strip_address(text)
        m = _REMIND.match(t)
        if m:
            return await self._remind(m.group("x"), source, now)
        if not addressed:
            return None
        m = _REMEMBER_TO.match(t)
        if m:
            return await self._remind(m.group("x"), source, now)
        m = _ADD.match(t)
        if m:
            return await self._add(m.group("x") or m.group("y"), source, now)
        if _LIST.match(t):
            return await self._list(now)
        m = _DELETE.match(t)
        if m:
            return await self._delete(m.group("x"), source, now)
        m = _DONE.match(t)
        if m:
            return await self._done(m.group("x") or m.group("y") or m.group("z"), now)
        m = _SNOOZE.match(t)
        if m:
            return await self._snooze(m.group("x"), now)
        return None

    def _repeat(self, repeat: str) -> str:
        return {
            "daily": ", every day",
            "weekdays": ", every weekday",
            "weekly": ", every week",
        }.get(repeat, "")

    async def _remind(self, said: str, source: str, now: datetime) -> str:
        when = extract(said, now, self._tasks.tz)
        title = _clean(when.title)
        if not title:
            return "Remind you of what? Say 'remind me to … at 7'."
        if when.at is None:
            return WHEN.format(title=title)
        task = await self._tasks.add(
            title, source="told", channel=source, now=now, at=when.at, repeat=when.repeat
        )
        at = describe(when.at, now, self._tasks.tz)
        return f"Reminder set for {at}{self._repeat(task.repeat)}: {task.title}."

    async def _add(self, said: str, source: str, now: datetime) -> str:
        when = extract(said, now, self._tasks.tz)
        title = _clean(when.title)
        if not title:
            return "Add what? Say 'add a task …'."
        task = await self._tasks.add(
            title, source="told", channel=source, now=now, at=when.at, repeat=when.repeat
        )
        if when.at is None:
            return f"Added to your tasks: {task.title}."
        at = describe(when.at, now, self._tasks.tz)
        return f"Added: {task.title}, due {at}{self._repeat(task.repeat)}. I'll remind you then."

    async def _list(self, now: datetime) -> str:
        tasks = await self._tasks.open()
        if not tasks:
            return "Your task list is empty."
        lines = [f"{i}. {t.line(now, self._tasks.tz)}" for i, t in enumerate(tasks[:10], start=1)]
        more = f"\n…and {len(tasks) - 10} more." if len(tasks) > 10 else ""
        n = len(tasks)
        return f"You have {n} open task{'s' if n != 1 else ''}:\n" + "\n".join(lines) + more

    async def _pick(self, words: str) -> tuple[Task | None, str]:
        """One matching open task, or a message saying why not."""
        found = await self._tasks.find(_clean(words))
        if not found:
            return None, f"I don't see an open task like '{_clean(words)}'."
        if len(found) > 1:
            names = "; ".join(f"#{t.id} {t.title}" for t in found[:4])
            return None, f"Which one? {names}. Say it with the number, like 'task {found[0].id}'."
        return found[0], ""

    async def _done(self, words: str, now: datetime) -> str:
        words = re.sub(
            r"^(?:the |my )?(?:task|reminder)(?:\s+(?:to|about))?\s+", "", words, flags=I
        )
        task, problem = await self._pick(words)
        if task is None:
            return problem
        after = await self._tasks.complete(task, now)
        if after.status == "open" and after.when is not None:  # it repeats
            return f"Done: {task.title}. Next one {describe(after.when, now, self._tasks.tz)}."
        return f"Done: {task.title}."

    async def _delete(self, words: str, source: str, now: datetime) -> str:
        task, problem = await self._pick(words)
        if task is None:
            return problem
        target = task

        async def run() -> str:
            await self._tasks.cancel(target, now, via=source)
            return f"Deleted: {target.title}."

        if now - task.created_at <= RECENT:
            return await run()  # you just made it: no need to ask
        return self._confirmer.confirm(source, f"Delete the task '{task.title}'?", run)

    async def _snooze(self, said: str, now: datetime) -> str:
        task = await self._tasks.last_reminded()
        if (
            task is None
            or task.last_reminded_at is None
            or now - task.last_reminded_at > timedelta(minutes=30)
        ):
            return "There's no recent reminder to snooze."
        said = said.strip()
        if not said:
            until = now + timedelta(minutes=10)
        else:
            phrase = (
                said
                if re.match(r"^(?:in|at|until|till|tomorrow|tonight)\b", said, I)
                else f"in {said}"
            )
            until_dt = extract(f"x {phrase}", now, self._tasks.tz).at
            if until_dt is None:
                return "Snooze until when? Try 'snooze 20 minutes' or 'snooze until 6'."
            until = until_dt
        await self._tasks.snooze(task, until, now)
        return f"Snoozed until {describe(until, now, self._tasks.tz)}: {task.title}."


async def deliver_due(
    tasks: TaskStore,
    now: datetime,
    *,
    paused: bool,
    focus: bool,
    note: object,
    say: object | None,
) -> list[str]:
    """The scheduler step: deliver reminders that are due. Returns the texts delivered.

    Paused ("stop"): nothing is delivered; they wait for "resume". Focus: written to your
    self-chat but not spoken, unless the task is high priority. `note(text)` and `say(text)`
    are async callables.
    """
    if paused:
        return []
    out: list[str] = []
    for task in await tasks.due(now):
        assert task.remind_at is not None
        late = now - task.remind_at > timedelta(minutes=10)
        was = f" (was due {describe(task.remind_at, now, tasks.tz)})" if late else ""
        text = f"⏰ Reminder: {task.title}{was}"
        await note(text)  # type: ignore[operator]
        # Marked as soon as it's written down: a speaker problem must not repeat the note
        # every second.
        await tasks.mark_reminded(task, now)
        out.append(text)
        if say is not None and (not focus or task.priority == "high"):
            try:
                await say(f"Reminder: {task.title}.")  # type: ignore[operator]
            except Exception:  # the written note already went out
                continue
    return out
