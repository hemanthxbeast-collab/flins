"""Plans (JARVIS phase 3b): a goal turned into ordered steps. Each step is a task.

    make a plan to prepare for my physics exam     proposes steps, saves them only on "yes"
    show my plans / what's my plan for the exam
    what's next / next step
    delete the plan [for the exam]                 asks first

The model only proposes. A plan exists once you say yes, and its steps are things you do: no
step can act on anything (there are no tools yet).
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from flins.assistant.memory import tokens
from flins.assistant.memory_commands import Confirmer
from flins.assistant.tasks import Task, TaskStore
from flins.core.audit import AuditLog
from flins.core.clock import from_iso, to_iso
from flins.core.grammar import strip_address
from flins.llm.context import neutralise
from flins.llm.gateway import LLMError, LLMGateway, LLMRefusal
from flins.llm.prompts import Prompt

MAX_STEPS = 10
MAX_STEP_CHARS = 160
MAX_TITLE_CHARS = 80
MAX_GOAL_CHARS = 600

NO_AI = "Making a plan needs an AI model, and none is set up. See README, 'AI providers'."
BUDGET = "Today's AI budget is used up, so I can't make a plan until tomorrow."
DECLINED = "The AI model declined to plan that one."
FAILED = "I couldn't make that plan: the AI model didn't answer properly. Try again in a moment."
UNCLEAR = "What should the plan be for? Say 'make a plan to …' with the goal."


class PlanOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=200)
    steps: list[str] = Field(max_length=20)
    question: str | None = Field(default=None, max_length=300)


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}},
        "question": {"type": ["string", "null"]},
    },
    "required": ["title", "steps", "question"],
    "additionalProperties": False,
}


def _one_line(text: str, n: int) -> str:
    t = " ".join(neutralise(text).split()).strip(" -•*").strip()
    t = re.sub(r"^(?:step\s*)?\d+[.):]\s*", "", t, flags=re.IGNORECASE)  # "1. " / "Step 2: "
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def clean_steps(steps: list[str]) -> list[str]:
    """Model output made safe to show and store: one line each, clipped, no blanks, no
    repeats, at most MAX_STEPS."""
    out: list[str] = []
    seen: set[str] = set()
    for s in steps:
        line = _one_line(s, MAX_STEP_CHARS).rstrip(".")
        key = line.lower()
        if line and key not in seen:
            seen.add(key)
            out.append(line)
    return out[:MAX_STEPS]


@dataclass(frozen=True)
class Proposal:
    title: str
    steps: list[str]
    question: str | None = None


class Planner:
    """The one model call. No tools; it sees the time, your open tasks and the goal."""

    def __init__(self, gateway: LLMGateway, prompt: Prompt) -> None:
        self._gw = gateway
        self._prompt = prompt

    async def budget_ok(self) -> bool:
        return await self._gw.budget_ok()

    async def propose(self, goal: str, now_text: str, open_tasks: list[str]) -> Proposal:
        blocks = [f"<now>{now_text}</now>"]
        if open_tasks:
            lines = "\n".join(f"- {neutralise(t)[:200]}" for t in open_tasks[:15])
            blocks.append(f"<tasks>\n{lines}\n</tasks>")
        blocks.append(f"<goal>\n{neutralise(goal)[:MAX_GOAL_CHARS]}\n</goal>")
        out = await self._gw.json(
            prompt=self._prompt,
            user="\n".join(blocks),
            schema=PLAN_SCHEMA,
            out=PlanOut,
            role="reason",
        )
        steps = clean_steps(out.steps)
        question = _one_line(out.question, 300) if out.question else None
        return Proposal(_one_line(out.title, MAX_TITLE_CHARS).rstrip("."), steps, question)


@dataclass(frozen=True)
class Plan:
    id: int
    title: str
    goal: str
    status: str
    created_at: datetime
    steps: list[Task]

    @property
    def done(self) -> int:
        return sum(1 for s in self.steps if s.status == "done")

    @property
    def total(self) -> int:
        return sum(1 for s in self.steps if s.status != "cancelled")

    @property
    def next_step(self) -> Task | None:
        return next((s for s in self.steps if s.status == "open"), None)

    def progress(self) -> str:
        return f"{self.done} of {self.total} done"


class PlanStore:
    def __init__(self, conn: aiosqlite.Connection, tasks: TaskStore) -> None:
        self._conn = conn
        self._tasks = tasks
        self._audit = AuditLog(conn)

    async def _plan(self, r: aiosqlite.Row) -> Plan:
        return Plan(
            r["id"],
            r["title"],
            r["goal"],
            r["status"],
            from_iso(r["created_at"]),
            await self._tasks.steps(r["id"]),
        )

    async def get(self, plan_id: int) -> Plan | None:
        async with self._conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)) as cur:
            row = await cur.fetchone()
        return await self._plan(row) if row else None

    async def create(
        self,
        title: str,
        goal: str,
        steps: list[str],
        *,
        source: str,
        channel: str | None,
        now: datetime,
    ) -> Plan:
        steps = clean_steps(steps)
        title = _one_line(title, MAX_TITLE_CHARS) or _one_line(goal, MAX_TITLE_CHARS)
        if not steps or not title:
            raise ValueError("a plan needs a title and at least one step")
        cur = await self._conn.execute(
            "INSERT INTO plans (title, goal, source, channel, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, goal[:MAX_GOAL_CHARS], source, channel, to_iso(now), to_iso(now)),
        )
        await self._conn.commit()
        assert cur.lastrowid is not None
        plan_id = cur.lastrowid
        for n, step in enumerate(steps, start=1):
            await self._tasks.add(
                step,
                source=source,
                channel=channel,
                now=now,
                plan_id=plan_id,
                step_no=n,
                project=title,
            )
        await self._audit.record(
            "user", "plan.saved", {"id": plan_id, "steps": len(steps), "channel": channel}
        )
        plan = await self.get(plan_id)
        assert plan is not None
        return plan

    async def open(self) -> list[Plan]:
        """Plans still in progress, newest first."""
        async with self._conn.execute(
            "SELECT * FROM plans WHERE status = 'open' ORDER BY id DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [await self._plan(r) for r in rows]

    async def recent(self, limit: int = 10) -> list[Plan]:
        async with self._conn.execute(
            "SELECT * FROM plans WHERE status != 'cancelled' ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [await self._plan(r) for r in rows]

    async def find(self, words: str) -> list[Plan]:
        """Open plans whose title or goal matches `words` (or a plan number)."""
        q = words.strip().strip(".!?\"'").lower()
        plans = await self.open()
        if q.lstrip("#").isdigit():
            return [p for p in plans if p.id == int(q.lstrip("#"))]
        asked = tokens(q)
        hits = []
        for p in plans:
            have = tokens(p.title.lower()) | tokens(p.goal.lower())
            overlap = len(asked & have)
            if q and q in p.title.lower():
                hits.append((100, p))
            elif asked and overlap >= max(1, round(0.6 * len(asked))):
                hits.append((overlap, p))
        hits.sort(key=lambda h: (-h[0], -h[1].id))
        return [p for _, p in hits]

    async def cancel(self, plan: Plan, now: datetime, *, via: str) -> int:
        """Drop a plan: its open steps leave the task list. Returns how many were removed."""
        cur = await self._conn.execute(
            "UPDATE tasks SET status = 'cancelled', remind_at = NULL, updated_at = ? "
            "WHERE plan_id = ? AND status = 'open'",
            (to_iso(now), plan.id),
        )
        await self._conn.execute(
            "UPDATE plans SET status = 'cancelled', updated_at = ? WHERE id = ?",
            (to_iso(now), plan.id),
        )
        await self._conn.commit()
        await self._audit.record("user", "plan.cancelled", {"id": plan.id, "via": via})
        return cur.rowcount


I = re.IGNORECASE  # noqa: E741
_MAKE = re.compile(
    r"^(?:please\s+)?(?:can you\s+|could you\s+)?(?:"
    r"(?:make|create|build|draft|write|give)(?: me)? a plan\b(?: (?:for|to|on|about))?"
    r"|help me plan(?: (?:for|to|out))?"
    r"|plan out"
    r"|plan (?=(?:for|to|my|our|the|a|an|how)\b)"
    r"|break down"
    r")\s*(?P<x>.*?)(?:\s+into (?:steps|tasks))?[.?!]*$",
    I | re.DOTALL,
)
_SHOW = re.compile(
    r"^(?:(?:show|list|read)(?: me)? (?:my |the )?plans?|what(?:'s| is| are) (?:my|the) plans?"
    r"|my plans?)(?:\s+(?:for|to|about|on)\s+(?P<x>.+?))?[.?!]*$",
    I,
)
_NEXT = re.compile(
    r"^(?:what(?:'s| is)(?: the| my)? next(?: step)?|next step|what next"
    r"|what (?:do|should) i do next)(?:\s+(?:in|of|for|on)\s+(?P<x>.+?))?[.?!]*$",
    I,
)
_DROP = re.compile(
    r"^(?:delete|cancel|drop|remove|scrap) (?:the |my |that )?plan"
    r"(?:\s+(?:for|to|about|on|called))?(?:\s+(?P<x>.+?))?[.?!]*$",
    I,
)

Wait = Callable[[], Awaitable[None]]


def render(plan: Plan) -> str:
    lines = [f"Plan '{plan.title}' ({plan.progress()}):"]
    nxt = plan.next_step
    for s in plan.steps:
        if s.status == "cancelled":
            continue
        mark = " (done)" if s.status == "done" else " (next)" if nxt and s.id == nxt.id else ""
        lines.append(f"{s.step_no}. {s.title}{mark}")
    return "\n".join(lines)


class PlanCommands:
    def __init__(
        self,
        plans: PlanStore,
        tasks: TaskStore,
        confirmer: Confirmer,
        planner: Planner | None,
    ) -> None:
        self._plans = plans
        self._tasks = tasks
        self._confirmer = confirmer
        self._planner = planner

    async def handle(
        self, text: str, source: str, now: datetime, *, addressed: bool, wait: Wait | None = None
    ) -> str | None:
        """Reply text if `text` is a plan command, else None."""
        if not addressed:
            return None
        t = strip_address(text)
        m = _DROP.match(t)
        if m:
            return await self._drop(m.group("x"), source, now)
        m = _SHOW.match(t)
        if m:
            return await self._show(m.group("x"))
        m = _NEXT.match(t)
        if m:
            return await self._next((m.group("x") or "").strip())
        m = _MAKE.match(t)
        if m:
            return await self._make(m.group("x").strip(), source, now, wait)
        return None

    # ------------------------------------------------------------------ make

    async def _make(self, goal: str, source: str, now: datetime, wait: Wait | None) -> str:
        if len(goal) < 3:
            return UNCLEAR
        if self._planner is None:
            return NO_AI
        if not await self._planner.budget_ok():
            return BUDGET
        if wait is not None:
            with contextlib.suppress(Exception):  # a courtesy only
                await wait()
        local = now.astimezone(self._tasks.tz)
        open_tasks = [t.title for t in await self._tasks.open()]
        try:
            p = await self._planner.propose(goal, f"{local:%A %d %B %Y, %H:%M}", open_tasks)
        except LLMRefusal:
            return DECLINED
        except LLMError:
            return FAILED
        if not p.steps:
            return p.question or UNCLEAR
        title = p.title or _one_line(goal, MAX_TITLE_CHARS)

        async def run() -> str:
            plan = await self._plans.create(
                title, goal, p.steps, source="told", channel=source, now=now
            )
            first = plan.next_step
            n = plan.total
            return (
                f"Saved. {n} step{'s' if n != 1 else ''} are on your task list. "
                f"First: {first.title if first else plan.title}."
            )

        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(p.steps, start=1))
        return self._confirmer.confirm(source, f"Plan: {title}\n{steps}\nSave this plan?", run)

    # ------------------------------------------------------------------ show / next / drop

    async def _show(self, about: str | None) -> str | None:
        if about:
            found = await self._plans.find(about)
            # "what's my plan for today?" with no such plan is a question for the
            # conversation, not a command.
            return render(found[0]) if found else None
        plans = await self._plans.open()
        if not plans:
            return "You have no plans in progress. Say 'make a plan to …' to start one."
        if len(plans) == 1:
            return render(plans[0])
        lines = [f"#{p.id} {p.title} ({p.progress()})" for p in plans[:8]]
        return f"You have {len(plans)} plans in progress:\n" + "\n".join(lines)

    async def _next(self, about: str) -> str | None:
        plans = await self._plans.find(about) if about else await self._plans.open()
        if not plans:
            # No plan: "what's next" is an ordinary question. Let the conversation answer it.
            return None
        plan = plans[0]
        step = plan.next_step
        if step is None:
            return f"The plan '{plan.title}' is finished."
        return f"Next in '{plan.title}': step {step.step_no} of {plan.total}, {step.title}."

    async def _drop(self, about: str | None, source: str, now: datetime) -> str:
        plans = await self._plans.find(about) if about else await self._plans.open()
        if not plans:
            return (
                f"I don't see a plan like '{about.strip()}'."
                if about
                else "You have no plans in progress."
            )
        if len(plans) > 1:
            names = "; ".join(f"#{p.id} {p.title}" for p in plans[:4])
            return f"Which one? {names}. Say 'delete the plan {plans[0].id}'."
        plan = plans[0]

        async def run() -> str:
            n = await self._plans.cancel(plan, now, via=source)
            return f"Deleted the plan '{plan.title}' and its {n} open step{'s' if n != 1 else ''}."

        left = plan.total - plan.done
        return self._confirmer.confirm(
            source,
            f"Delete the plan '{plan.title}' ({left} step{'s' if left != 1 else ''} left)?",
            run,
        )
