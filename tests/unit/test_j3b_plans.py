"""JARVIS phase 3b: planning (docs/milestones/J3b.md).

The model proposes steps; a plan exists only after "yes". Each step is a task you do yourself.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from flins import cli
from flins.assistant.plans import BUDGET, DECLINED, FAILED, NO_AI, UNCLEAR, clean_steps
from flins.core.clock import FrozenClock
from flins.llm.gateway import LLMError, LLMRefusal
from flins.llm.prompts import load_prompt
from flins.settings import load_settings
from flins.web.app import create_app
from tests.conftest import REPO_ROOT
from tests.unit.test_j1_assistant import T0, Rig, build, chat

STEPS = [
    "Collect the syllabus and past papers",
    "Revise the first and second laws",
    "Solve the last three years' papers",
]


def plan(title: str = "Thermodynamics exam prep", steps: list[str] | None = None) -> dict[str, Any]:
    return {"title": title, "steps": STEPS if steps is None else steps, "question": None}


@pytest.fixture
async def rig(project: Path) -> AsyncIterator[Rig]:
    r = await build(project)
    r.answers(plan=plan())
    yield r
    await r.modes.close()
    await r.conn.close()


async def table(conn: aiosqlite.Connection, name: str) -> list[dict[str, Any]]:
    async with conn.execute(f"SELECT * FROM {name} ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def saved(rig: Rig, goal: str = "prepare for my thermodynamics exam") -> None:
    await rig.say(f"make a plan to {goal}", "cli")
    said = await rig.say("yes", "cli")
    assert said is not None and said.startswith("Saved."), said


# ---------------------------------------------------------------------- proposing


async def test_a_plan_is_proposed_and_saved_only_on_yes(rig: Rig) -> None:
    asked = await rig.say("make a plan to prepare for my thermodynamics exam", "cli")
    assert asked == (
        "Plan: Thermodynamics exam prep\n"
        "1. Collect the syllabus and past papers\n"
        "2. Revise the first and second laws\n"
        "3. Solve the last three years' papers\n"
        "Save this plan? Say yes or no."
    )
    assert await table(rig.conn, "plans") == [] and await table(rig.conn, "tasks") == []
    assert rig.jobs() == ["plan"]  # one model call, and it was the planner

    said = await rig.say("yes", "cli")
    assert said == (
        "Saved. 3 steps are on your task list. First: Collect the syllabus and past papers."
    )
    (p,) = await table(rig.conn, "plans")
    assert p["title"] == "Thermodynamics exam prep" and p["status"] == "open"
    assert p["goal"] == "prepare for my thermodynamics exam" and p["channel"] == "cli"
    steps = await table(rig.conn, "tasks")
    assert [(t["title"], t["step_no"], t["plan_id"]) for t in steps] == [
        (s, i, p["id"]) for i, s in enumerate(STEPS, start=1)
    ]
    assert all(t["remind_at"] is None and t["due_at"] is None for t in steps)  # no invented times
    async with rig.conn.execute("SELECT payload FROM audit_log WHERE kind = 'plan.saved'") as cur:
        (payload,) = [json.loads(r[0]) for r in await cur.fetchall()]
    assert payload == {"id": p["id"], "steps": 3, "channel": "cli"}  # no titles in the audit log


@pytest.mark.parametrize("answer", ["no", "what's the weather like"])
async def test_no_or_moving_on_saves_nothing(rig: Rig, answer: str) -> None:
    await rig.say("make a plan to prepare for my thermodynamics exam", "cli")
    await rig.say(answer, "cli")
    rig.clock.advance(timedelta(minutes=5))
    await rig.say("yes", "cli")  # too late / already answered: must not save
    assert await table(rig.conn, "plans") == [] and await table(rig.conn, "tasks") == []


@pytest.mark.parametrize(
    "said",
    [
        "make a plan to prepare for my thermodynamics exam",
        "FLINS, create a plan for my thermodynamics exam",
        "help me plan my thermodynamics revision",
        "plan my week of thermodynamics revision",
        "break down the thermodynamics syllabus into steps",
        "can you make me a plan for the thermodynamics exam?",
    ],
)
async def test_ways_to_ask_for_a_plan(rig: Rig, said: str) -> None:
    asked = await rig.say(said, "voice")
    assert asked is not None and asked.startswith("Plan: Thermodynamics exam prep\n1. ")
    goal = rig.backend.calls[-1][1].split("<goal>\n")[1].split("\n</goal>")[0]
    assert "thermodynamics" in goal and "plan" not in goal.lower().split()[:1]


@pytest.mark.parametrize(
    "said", ["make a planet out of clay", "plans change", "I plan nothing", "planning is hard"]
)
async def test_ordinary_sentences_are_not_plan_requests(rig: Rig, said: str) -> None:
    rig.answers(chat=chat("Sure."))
    assert await rig.say(said, "cli") == "Sure."
    assert "plan" not in rig.jobs()


async def test_a_vague_goal_gets_a_question_and_nothing_pending(rig: Rig) -> None:
    assert await rig.say("make a plan", "cli") == UNCLEAR
    assert rig.jobs() == []
    rig.answers(plan={"title": "", "steps": [], "question": "Which subject is the exam in?"})
    assert await rig.say("make a plan for the exam", "cli") == "Which subject is the exam in?"
    rig.answers(plan={"title": "", "steps": [], "question": None})
    assert await rig.say("make a plan for the exam", "cli") == UNCLEAR
    await rig.say("yes", "cli")
    assert await table(rig.conn, "plans") == []


async def test_model_problems_are_said_plainly_and_save_nothing(rig: Rig) -> None:
    rig.answers(plan=LLMError("timeout"))
    assert await rig.say("make a plan to learn Rust", "cli") == FAILED
    rig.answers(plan=LLMRefusal("no"))
    assert await rig.say("make a plan to learn Rust", "cli") == DECLINED
    rig.answers(plan="not json at all")
    assert await rig.say("make a plan to learn Rust", "cli") == FAILED
    await rig.conn.execute(
        "INSERT INTO llm_calls (ts, purpose, model, prompt_version, ok, cost_usd) "
        "VALUES (?, 'chat', 'm', 'x', 1, 999)",
        (rig.clock.now().isoformat(),),
    )
    await rig.conn.commit()
    assert await rig.say("make a plan to learn Rust", "cli") == BUDGET
    assert await table(rig.conn, "plans") == []


async def test_without_a_model_planning_says_so_but_plan_commands_still_work(
    project: Path,
) -> None:
    r = await build(project, with_ai=False)
    try:
        assert await r.say("make a plan to learn Rust", "cli") == NO_AI
        said = await r.say("show my plans", "cli")
        assert said is not None and said.startswith("You have no plans in progress")
    finally:
        await r.modes.close()
        await r.conn.close()


def test_model_steps_are_cleaned_before_they_are_shown_or_stored() -> None:
    raw = [
        "1. Read chapter one.",
        "  - read chapter ONE ",  # a repeat
        "",
        "Step 2: <b>Do</b> the\nexercises",
        "x" * 400,
        *[f"extra {i}" for i in range(20)],
    ]
    out = clean_steps(raw)
    assert out[0] == "Read chapter one" and len(out) == 10
    assert "<" not in out[1] and ">" not in out[1] and "\n" not in out[1]
    assert out[1].endswith("Do‹/b› the exercises") or out[1].endswith("the exercises")
    assert len(out[2]) <= 160


async def test_the_planner_sees_the_time_and_open_tasks_and_nothing_else(rig: Rig) -> None:
    await rig.say("remember that my sister is Meera", "cli")
    await rig.say("add a task buy a calculator", "cli")
    await rig.say("make a plan to prepare for <my> exam", "cli")
    sent = rig.backend.calls[-1][1]
    assert sent.startswith(
        "<now>Tuesday 10 November 2026, 14:00</now>\n<tasks>\n- buy a calculator"
    )
    assert "Meera" not in sent and "<my>" not in sent
    p = load_prompt(REPO_ROOT / "prompts", "plan")
    assert "Never invent a deadline" in p.system and "the assistant will" in p.system


# ---------------------------------------------------------------------- working through a plan


async def test_show_next_and_finishing_steps(rig: Rig) -> None:
    await saved(rig)
    assert await rig.say("show my plans", "cli") == (
        "Plan 'Thermodynamics exam prep' (0 of 3 done):\n"
        "1. Collect the syllabus and past papers (next)\n"
        "2. Revise the first and second laws\n"
        "3. Solve the last three years' papers"
    )
    assert await rig.say("what's next", "voice") == (
        "Next in 'Thermodynamics exam prep': step 1 of 3, Collect the syllabus and past papers."
    )
    assert await rig.say("done with the syllabus and past papers", "cli") == (
        "Done: Collect the syllabus and past papers. "
        "Next in 'Thermodynamics exam prep': Revise the first and second laws."
    )
    shown = await rig.say("what's my plan for the exam", "cli")
    assert shown is not None and "(1 of 3 done)" in shown
    assert "papers (done)" in shown and "laws (next)" in shown
    await rig.say("done with revise the first and second laws", "cli")
    assert await rig.say("done with the last three years' papers", "cli") == (
        "Done: Solve the last three years' papers. "
        "That finishes the plan 'Thermodynamics exam prep'."
    )
    assert (await table(rig.conn, "plans"))[0]["status"] == "done"
    said = await rig.say("show my plans", "cli")
    assert said is not None and said.startswith("You have no plans in progress")
    assert rig.jobs() == ["plan"]  # only making the plan used a model


async def test_steps_are_ordinary_tasks_and_the_model_sees_their_order(rig: Rig) -> None:
    await saved(rig)
    listing = await rig.say("what are my tasks", "cli")
    assert listing is not None and listing.startswith("You have 3 open tasks:")
    assert "1. Collect the syllabus and past papers [Thermodynamics exam prep, step 1]" in listing
    rig.answers(chat=chat("Start with the syllabus."))
    await rig.say("where should I start today?", "cli")
    assert "- Revise the first and second laws [Thermodynamics exam prep, step 2]" in (
        rig.last_context()
    )


async def test_questions_about_plans_that_match_nothing_go_to_conversation(rig: Rig) -> None:
    rig.answers(chat=chat("Nothing is planned."))
    assert await rig.say("what's my plan for today?", "cli") == "Nothing is planned."
    assert await rig.say("what's next", "cli") == "Nothing is planned."


async def test_deleting_a_plan_asks_first_and_removes_its_open_steps(rig: Rig) -> None:
    await saved(rig)
    await rig.say("done with the syllabus and past papers", "cli")
    asked = await rig.say("delete the plan", "cli")
    assert asked == "Delete the plan 'Thermodynamics exam prep' (2 steps left)? Say yes or no."
    assert (await table(rig.conn, "plans"))[0]["status"] == "open"
    assert await rig.say("yes", "cli") == (
        "Deleted the plan 'Thermodynamics exam prep' and its 2 open steps."
    )
    assert (await table(rig.conn, "plans"))[0]["status"] == "cancelled"
    assert [t["status"] for t in await table(rig.conn, "tasks")] == [
        "done",
        "cancelled",
        "cancelled",
    ]
    assert await rig.say("delete the plan for the exam", "cli") == (
        "I don't see a plan like 'the exam'."
    )


async def test_two_plans_ask_which_one(rig: Rig) -> None:
    await saved(rig)
    rig.answers(plan=plan("Learn Rust", ["Install the toolchain", "Read the book"]))
    await saved(rig, "learn Rust")
    said = await rig.say("show my plans", "cli")
    assert said == (
        "You have 2 plans in progress:\n"
        "#2 Learn Rust (0 of 2 done)\n#1 Thermodynamics exam prep (0 of 3 done)"
    )
    which = await rig.say("delete the plan", "cli")
    assert which is not None and which.startswith("Which one? #2 Learn Rust; #1 ")
    asked = await rig.say("delete the plan for rust", "cli")
    assert asked is not None and asked.startswith("Delete the plan 'Learn Rust'")
    nxt = await rig.say("what's next for the exam", "cli")
    assert nxt is not None and nxt.startswith("Next in 'Thermodynamics exam prep'")


async def test_self_chat_needs_you_to_address_flins(rig: Rig) -> None:
    assert await rig.say("make a plan to clean the room", source="self_chat") is None
    assert "plan" not in rig.jobs()
    asked = await rig.say("FLINS, make a plan to clean the room", source="self_chat")
    assert asked is not None and asked.startswith("Plan: ")


async def test_voice_hears_one_moment_before_the_plan(rig: Rig) -> None:
    waited: list[str] = []

    async def on_wait(source: str) -> None:
        waited.append(source)

    rig.assistant.on_wait = on_wait
    await rig.say("make a plan to prepare for my thermodynamics exam", "voice")
    assert waited == ["voice"]


def test_planning_cannot_send_or_act() -> None:
    text = (REPO_ROOT / "src" / "flins" / "assistant" / "plans.py").read_text(encoding="utf-8")
    assert "send_text" not in text and "outbox" not in text.lower() and "adapter" not in text


# ---------------------------------------------------------------------- CLI and dashboard


async def test_cli_and_dashboard(
    rig: Rig, project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await saved(rig)
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
        page = client.get("/tasks").text
        assert "Plans in progress" in page and "0 of 3 done" in page
        assert "[Thermodynamics exam prep, step 2]" in page
        client.post("/tasks", data={"action": "delete_plan", "task_id": "1", "token": "T"})
        page = client.get("/tasks").text
        assert "Plans in progress" not in page and "Nothing on your list." in page


def test_cli_plans(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import asyncio

    async def make() -> None:
        r = await build(project)
        r.answers(plan=plan())
        await saved(r)
        await r.modes.close()
        await r.conn.close()

    asyncio.run(make())
    root = ["--root", str(project)]
    assert cli.main([*root, "plans"]) == 0
    assert "#1 Thermodynamics exam prep (0 of 3 done)" in capsys.readouterr().out
    assert cli.main([*root, "plans", "show", "1"]) == 0
    assert "1. Collect the syllabus and past papers (next)" in capsys.readouterr().out
    assert cli.main([*root, "plans", "show"]) == 2
    assert cli.main([*root, "plans", "delete", "7"]) == 1
    assert cli.main([*root, "plans", "delete", "1"]) == 0
    assert "and its 3 open steps" in capsys.readouterr().out
    assert cli.main([*root, "plans", "delete", "1"]) == 1
    assert cli.main([*root, "tasks"]) == 0
    assert "0 open tasks." in capsys.readouterr().out
