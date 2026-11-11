"""JARVIS phase 2: memory (docs/milestones/J2.md).

Nothing is saved silently, secrets are refused, every memory has a real source, and you can
see, correct, delete and switch off what FLINS remembers.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from flins import cli
from flins.assistant.core import CANT
from flins.assistant.memory import SqlMemory, guess_kind, is_secret, score_importance
from flins.assistant.memory_commands import MEMORY_OFF, REMINDER
from flins.core.clock import FrozenClock
from flins.core.db import connect, migrate
from flins.core.levels import Mode
from flins.llm.gateway import LLMError
from flins.llm.prompts import load_prompt
from flins.settings import load_settings
from flins.web.app import create_app
from tests.conftest import REPO_ROOT
from tests.unit.test_j1_assistant import Rig, build, chat
from tests.unit.test_m6_voice_and_intents import intent

TZ = ZoneInfo("Asia/Kolkata")
T0 = datetime(2026, 11, 10, 8, 30, tzinfo=UTC)


@pytest.fixture
async def rig(project: Path) -> AsyncIterator[Rig]:
    r = await build(project)
    yield r
    await r.modes.close()
    await r.conn.close()


@pytest.fixture
async def mem(tmp_path: Path) -> AsyncIterator[tuple[SqlMemory, FrozenClock, aiosqlite.Connection]]:
    conn = await connect(tmp_path / "m.db")
    await migrate(conn)
    clock = FrozenClock(T0)
    yield SqlMemory(conn, TZ, clock), clock, conn
    await conn.close()


async def rows(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    async with conn.execute("SELECT * FROM memories ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def audit(conn: aiosqlite.Connection, kind: str) -> list[dict[str, Any]]:
    async with conn.execute("SELECT payload FROM audit_log WHERE kind = ?", (kind,)) as cur:
        return [json.loads(r[0]) for r in await cur.fetchall()]


def suggest(reply: str, fact: str, importance: float = 0.9) -> dict[str, Any]:
    return {
        **chat(reply),
        "remember": fact,
        "remember_kind": "semantic",
        "remember_importance": importance,
    }


# ---------------------------------------------------------------------- the store


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("my ATM PIN is 4321", True),
        ("the wifi password is hunter2", True),
        ("OTP for the bank was 889123", True),
        ("card number 4111 1111 1111 1111", True),
        ("my API key starts with sk", True),
        ("my pin code is 560001", False),  # a postal code
        ("I pinned the notes to the board", False),
        ("my exam is on Friday", False),
    ],
)
def test_secrets_are_recognised(text: str, secret: bool) -> None:
    assert is_secret(text) is secret


def test_kind_and_importance() -> None:
    assert guess_kind("I prefer short explanations") == "semantic"
    assert guess_kind("I decided to use NVIDIA NIM for the project") == "episodic"
    assert guess_kind("Rahul is my best friend") == "relationship"
    assert score_importance("I always take notes in markdown") > score_importance("the sky was red")
    assert 0.1 <= score_importance("x" * 400, 0.2) <= 0.95


async def test_save_dedup_refuse_and_sensitivity(
    mem: tuple[SqlMemory, FrozenClock, aiosqlite.Connection],
) -> None:
    m, clock, conn = mem
    now = clock.now()
    first = await m.save("I prefer short explanations.", source="told", channel="voice", now=now)
    assert first.status == "saved" and first.record is not None
    assert first.record.where(TZ) == "you told me on 10 Nov 2026 by voice"
    again = await m.save("i prefer short explanations", source="told", channel="cli", now=now)
    assert again.status == "duplicate" and len(await rows(conn)) == 1
    refused = await m.save("my card PIN is 9911", source="told", channel="voice", now=now)
    assert refused.status == "refused" and "password manager" in (refused.reason or "")
    health = await m.save("My doctor said to avoid peanuts", source="told", channel="cli", now=now)
    assert health.record is not None and health.record.privacy == "sensitive"
    assert (await m.save("ok", source="told", channel="cli", now=now)).status == "refused"
    saved = await audit(conn, "memory.saved")
    assert len(saved) == 2 and "peanuts" not in json.dumps(saved)  # no memory text in the audit


async def test_relevant_matches_pins_and_hides_sensitive(
    mem: tuple[SqlMemory, FrozenClock, aiosqlite.Connection],
) -> None:
    m, clock, conn = mem
    now = clock.now()
    await m.save("The thermodynamics exam is on Friday", source="told", channel="cli", now=now)
    await m.save(
        "I always prefer explanations with code examples", source="told", channel="cli", now=now
    )
    await m.save("My doctor said to avoid peanuts", source="told", channel="cli", now=now)
    await m.save("The hackathon was fun", source="told", channel="cli", now=now, importance=0.3)
    await m.save(
        "The library closes early this week",
        source="told",
        channel="cli",
        now=now,
        expires_in=timedelta(days=2),
    )
    got = await m.relevant("when is my thermodynamics exam?", 10)
    assert got[0].content == "The thermodynamics exam is on Friday" and got[0].matched
    contents = [g.content for g in got]
    assert "I always prefer explanations with code examples" in contents  # standing preference
    assert "My doctor said to avoid peanuts" not in contents  # sensitive: only when relevant
    assert "The hackathon was fun" not in contents  # unimportant and unrelated
    assert any("peanuts" in g.content for g in await m.relevant("what did the doctor say?", 10))
    assert (await rows(conn))[0]["uses"] == 1
    clock.advance(timedelta(days=3))
    assert not any("library" in g.content for g in await m.relevant("library hours", 10))
    await m.set_enabled(False)
    assert await m.relevant("thermodynamics exam", 10) == []


async def test_edit_follows_the_same_rules(
    mem: tuple[SqlMemory, FrozenClock, aiosqlite.Connection],
) -> None:
    m, clock, _ = mem
    r = await m.save("The exam is on Friday", source="told", channel="cli", now=clock.now())
    assert r.record is not None
    fixed = await m.edit(r.record.id, "The exam is on Monday", clock.now())
    assert fixed.record is not None and fixed.record.content == "The exam is on Monday"
    assert (await m.edit(r.record.id, "password is abc", clock.now())).status == "refused"
    assert (await m.edit(999, "nothing here", clock.now())).status == "refused"


# ---------------------------------------------------------------------- remember


async def test_remember_saves_without_a_model_and_shows_up_with_its_source(rig: Rig) -> None:
    rig.answers(intent=LLMError("down"), chat=LLMError("down"))
    said = await rig.say("FLINS, remember that my thermodynamics exam is on Friday")
    assert said == 'I\'ll remember that: "my thermodynamics exam is on Friday".'
    assert rig.backend.calls == []
    rig.answers(intent=intent("unknown"), chat=chat("Friday."))
    await rig.say("when is my thermodynamics exam?")
    ctx = rig.last_context()
    assert (
        "<memories>\n- my thermodynamics exam is on Friday (you told me on 10 Nov 2026 by voice)"
        in ctx
    )


async def test_secrets_and_reminders_are_not_stored(rig: Rig) -> None:
    refused = await rig.say("remember that my ATM PIN is 4321", source="cli")
    assert refused is not None and refused.startswith("I won't save that")
    assert await rig.say("remember to call mom at 7", source="cli") == REMINDER
    assert await rows(rig.conn) == []
    assert "pin code" in (await rig.say("remember that my pin code is 560001", "cli") or "")
    assert len(await rows(rig.conn)) == 1


async def test_remember_this_saves_what_you_just_said_after_yes(rig: Rig) -> None:
    rig.answers(chat=chat("Sounds like a solid choice."))
    await rig.say("I decided to use NVIDIA NIM for the drone project", source="cli")
    question = await rig.say("remember this", source="cli")
    assert (
        question == 'Remember "I decided to use NVIDIA NIM for the drone project"? Say yes or no.'
    )
    assert await rows(rig.conn) == []  # not yet
    await rig.say("yes", source="cli")
    saved = await rows(rig.conn)
    assert saved[0]["content"] == "I decided to use NVIDIA NIM for the drone project"
    assert saved[0]["kind"] == "episodic" and saved[0]["source"] == "told"


async def test_remember_this_with_nothing_before_it(rig: Rig) -> None:
    assert (await rig.say("remember this", source="cli") or "").startswith("Remember what?")


# ---------------------------------------------------------------------- forget


async def test_forget_that_right_after_saving(rig: Rig) -> None:
    await rig.say("remember that I like filter coffee", source="cli")
    gone = await rig.say("forget that", source="cli") or ""
    assert gone.startswith('Forgotten: "I like filter coffee".')
    # It was said moments ago, so FLINS is upfront that the conversation still has it.
    assert "still in our recent conversation" in gone and "forget this conversation" in gone
    assert await rows(rig.conn) == []
    assert (await audit(rig.conn, "memory.forgotten"))[0]["via"] == "cli"


async def test_forget_about_a_topic_asks_first(rig: Rig) -> None:
    await rig.say("remember that Rahul is my lab partner", source="cli")
    await rig.say("remember that Rahul prefers calls over texts", source="cli")
    await rig.say("remember that I like filter coffee", source="cli")
    rig.clock.advance(timedelta(hours=1))
    question = await rig.say("forget everything about Rahul", source="cli")
    assert question is not None and question.startswith("Forget these 2 memories about Rahul")
    assert await rig.say("no", source="cli") == "Okay, I won't."
    assert len(await rows(rig.conn)) == 3
    await rig.say("forget about Rahul", source="cli")
    assert (await rig.say("yes", "cli") or "").startswith("Forgotten 2 memories about Rahul.")
    assert [r["content"] for r in await rows(rig.conn)] == ["I like filter coffee"]
    assert await rig.say("forget about zebras", "cli") == "I don't have anything about zebras."


async def test_forgetting_an_old_memory_has_no_conversation_note(rig: Rig) -> None:
    await rig.say("remember that I like filter coffee", source="cli")
    await rig.assistant.store.clear()  # nothing about it is left in the conversation
    assert await rig.say("forget that", "cli") == 'Forgotten: "I like filter coffee".'


async def test_forget_everything_and_forget_the_conversation(rig: Rig) -> None:
    await rig.say("remember that I like filter coffee", source="cli")
    question = await rig.say("forget everything", source="cli")
    assert question == "Delete all 1 memories? This can't be undone. Say yes or no."
    await rig.say("yes", source="cli")
    assert await rows(rig.conn) == []
    assert "Delete our whole conversation" in (
        await rig.say("forget this conversation", "cli") or ""
    )
    await rig.say("yes", source="cli")
    assert len(await rig.turns()) == 2  # only the "yes" and its answer remain


async def test_forget_that_when_nothing_is_recent(rig: Rig) -> None:
    await rig.say("remember that I like filter coffee", source="cli")
    await rig.say("remember that my exam is on Friday", source="cli")
    rig.clock.advance(timedelta(hours=2))
    assert (await rig.say("forget that", source="cli") or "").startswith("Which one?")
    rig.answers(chat=chat("Friday."))
    await rig.say("when is my exam?", source="cli")  # this answer used exactly one memory
    assert await rig.say("forget that", "cli") == 'Forget "my exam is on Friday"? Say yes or no.'
    await rig.say("yes", source="cli")
    assert [r["content"] for r in await rows(rig.conn)] == ["I like filter coffee"]


# ---------------------------------------------------------------------- recall and why


async def test_what_do_you_remember(rig: Rig) -> None:
    assert (await rig.say("what do you remember?", "cli") or "").startswith("I don't remember")
    await rig.say("remember that my thermodynamics exam is on Friday", source="voice")
    await rig.say("remember that I like filter coffee", source="cli")
    listing = await rig.say("what do you remember", source="cli")
    assert listing is not None and listing.startswith("I remember 2 things:")
    assert "• my thermodynamics exam is on Friday (you told me on 10 Nov 2026 by voice)" in listing
    about = await rig.say("what do you remember about the exam?", source="cli")
    assert about is not None and "thermodynamics" in about and "coffee" not in about
    assert await rig.say("what do you remember about zebras", "cli") == (
        "I don't remember anything about zebras."
    )
    assert "chat" not in rig.jobs()  # all of it without a model


async def test_general_knowledge_questions_are_not_memory_commands(rig: Rig) -> None:
    rig.answers(chat=chat("Quite a lot, in broad strokes."))
    assert await rig.say("what do you know about quantum physics?", "cli") == (
        "Quite a lot, in broad strokes."
    )


async def test_why_do_you_know_that_names_real_sources(rig: Rig) -> None:
    rig.answers(chat=chat("Paris."))
    await rig.say("what's the capital of France?", source="cli")
    assert (await rig.say("why do you know that?", "cli") or "").startswith(
        "Nothing from my memory went into that answer"
    )
    await rig.say("remember that my thermodynamics exam is on Friday", source="cli")
    rig.answers(chat=chat("Friday."))
    await rig.say("when is the thermodynamics exam?", source="cli")
    why = await rig.say("how do you know that", source="cli")
    assert why is not None
    assert (
        "• my thermodynamics exam is on Friday (you told me on 10 Nov 2026 in the terminal)" in why
    )


# ---------------------------------------------------------------------- switch


async def test_memory_off_saves_and_uses_nothing(rig: Rig) -> None:
    await rig.say("remember that my exam is on Friday", source="cli")
    off = await rig.say("memory off", source="cli")
    assert off is not None and "Memory is off" in off and "The 1 I already have are kept" in off
    assert await rig.say("remember that I like tea", source="cli") == MEMORY_OFF
    rig.answers(chat=suggest("Noted the preference exists.", "Likes tea."))
    reply = await rig.say("when is my exam? also I love tea", source="cli")
    assert "<memories>" not in rig.last_context()
    assert reply is not None and "Should I remember" not in reply
    await rig.say("turn memory on", source="cli")
    await rig.say("when is my exam?", source="cli")
    assert "<memories>" in rig.last_context()


async def test_memory_can_be_off_in_config(rig: Rig) -> None:
    rig.settings.app.assistant.memory.enabled = False
    rig.answers(chat=chat("I can't keep that yet.", "cannot"))
    assert await rig.say("remember that I like tea", "cli") == "I can't keep that yet."
    assert await rows(rig.conn) == []


# ---------------------------------------------------------------------- suggestions


async def test_a_suggested_memory_needs_your_yes(rig: Rig) -> None:
    rig.answers(chat=suggest("Markdown is a good fit for that.", "Takes notes in Markdown."))
    reply = await rig.assistant.handle("I always take my notes in markdown", source="cli")
    assert reply.text == (
        "Markdown is a good fit for that.\n"
        'Should I remember this: "Takes notes in Markdown."? Say yes or no.'
    )
    assert reply.stages[-1] == "memory suggested" and await rows(rig.conn) == []
    assert await rig.say("yes", source="cli") == "Okay, I'll remember that."
    saved = await rows(rig.conn)
    assert saved[0]["content"] == "Takes notes in Markdown." and saved[0]["source"] == "suggested"
    listing = await rig.say("what do you remember", source="cli") or ""
    assert "(I asked and you agreed on 10 Nov 2026 in the terminal)" in listing


async def test_saying_no_or_moving_on_saves_nothing(rig: Rig) -> None:
    rig.answers(chat=suggest("Good to know.", "Is vegetarian."))
    await rig.say("by the way I'm vegetarian", source="cli")
    await rig.say("no", source="cli")
    rig.clock.advance(timedelta(minutes=20))
    rig.answers(chat=suggest("Okay.", "Lives in a hostel."))
    await rig.say("I live in a hostel", source="cli")
    rig.answers(chat=chat("About 384,000 km."))
    await rig.say("how far is the moon?", source="cli")  # the question lapses
    assert await rows(rig.conn) == []


async def test_suggestions_are_limited(rig: Rig) -> None:
    rig.answers(chat=suggest("Okay.", "Mentioned the weather.", importance=0.4))
    assert "Should I remember" not in (await rig.say("it's hot today", "cli") or "")  # unimportant
    rig.answers(chat=suggest("Okay.", "The locker password is tiger."))
    assert "Should I remember" not in (await rig.say("my locker code thing", "cli") or "")  # secret
    rig.answers(chat=suggest("Okay.", "Is vegetarian."))
    assert "Should I remember" in (await rig.say("I'm vegetarian", "cli") or "")
    await rig.say("yes", source="cli")
    rig.answers(chat=suggest("Okay.", "Plays badminton on Sundays."))
    assert "Should I remember" not in (await rig.say("I play badminton on Sundays", "cli") or "")
    rig.clock.advance(timedelta(minutes=11))  # past the cooldown
    rig.answers(chat=suggest("Okay.", "Is a vegetarian"))  # already stored
    assert "Should I remember" not in (await rig.say("still vegetarian", "cli") or "")
    rig.answers(chat=suggest("Okay.", "Plays badminton on Sundays."))
    assert "Should I remember" in (await rig.say("badminton again this Sunday", "cli") or "")


async def test_the_model_still_cannot_claim_it_remembered(rig: Rig) -> None:
    rig.answers(chat=suggest("Got it, I'll remember that.", "Is vegetarian."))
    reply = await rig.assistant.handle("I'm vegetarian", source="cli")
    # Its false promise is dropped; the honest version (a question) takes its place.
    assert reply.text == 'Should I remember this: "Is vegetarian."? Say yes or no.'
    assert reply.flagged and await rows(rig.conn) == []
    rig.answers(chat={**chat("Sure, I'll remember that.")})  # no fact offered: plain refusal
    rig.clock.advance(timedelta(minutes=20))
    assert await rig.say("keep that in your head", source="cli") == CANT


# ---------------------------------------------------------------------- where it does not apply


async def test_self_chat_notes_do_not_become_memories(rig: Rig) -> None:
    assert await rig.say("remember milk and eggs", source="self_chat") is None  # a note to self
    assert await rows(rig.conn) == []
    saved = await rig.say("FLINS, remember that I like filter coffee", source="self_chat")
    assert saved is not None and saved.startswith("I'll remember that")
    gone = await rig.say("forget that", "self_chat") or ""
    assert gone.startswith('Forgotten: "I like filter coffee".')


async def test_an_open_question_wins_over_memory_commands(rig: Rig) -> None:
    rig.answers(intent=intent("extend"))
    assert (await rig.say("keep the mode going", "cli") or "").startswith("Until when?")
    assert rig.router.awaiting_answer("cli")
    await rig.say("remember that I like tea", source="cli")  # taken as the answer, not a memory
    assert await rows(rig.conn) == []


async def test_stop_still_wins(rig: Rig) -> None:
    await rig.say("remember that I like tea", source="cli")
    await rig.say("FLINS, stop")
    assert rig.modes.state.mode == Mode.PAUSED and len(await rows(rig.conn)) == 1


def test_whatsapp_replies_do_not_get_your_memories() -> None:
    """Memories are for your own conversation. The contact pipeline doesn't read them."""
    for name in ("pipeline.py", "policy.py", "outbox.py"):
        text = (REPO_ROOT / "src" / "flins" / "core" / name).read_text(encoding="utf-8")
        assert "flins.assistant" not in text and "memories" not in text, name


def test_chat_prompt_v2_explains_memory_without_personal_details() -> None:
    p = load_prompt(REPO_ROOT / "prompts", "chat")
    assert p.version == "chat_v2" and "<memories>" in p.system and "remember_importance" in p.system
    assert "Never guess a source or a date" in p.system


# ---------------------------------------------------------------------- CLI and dashboard


def test_cli_memory(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = load_settings(project)

    def run(action: str, *rest: str) -> tuple[int, str]:
        code = asyncio.run(cli._memory(settings, action, list(rest)))
        return code, capsys.readouterr().out

    assert run("add", "I", "prefer", "short", "answers")[1].startswith("Saved #1")
    assert "Not saved" in run("add", "the", "password", "is", "x")[1]
    assert "#1 [semantic" in run("list")[1]
    assert "Saved #1: I prefer detailed answers" in run("edit", "1", "I prefer detailed answers")[1]
    assert run("clear")[0] == 1  # not an interactive terminal
    assert run("off")[1] == "Memory is off.\n" and "OFF" in run("list")[1]
    assert run("forget", "#1") == (0, "Forgotten #1.\n")
    assert run("forget", "1")[0] == 1


def test_dashboard_memory_page(project: Path) -> None:
    settings = load_settings(project)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(settings.db_path, settings.policy, port=8765, token="T", settings=settings)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        assert "Nothing remembered yet" in c.get("/memory").text
        bad = c.post("/memory", data={"action": "add", "text": "x y z", "token": "no"})
        assert bad.status_code == 403
        c.post("/memory", data={"action": "add", "text": "I like <b>tea</b>", "token": "T"})
        r = c.post(
            "/memory",
            data={"action": "add", "text": "my bank PIN is 1234", "token": "T"},
            follow_redirects=False,
        )
        assert "Not%20saved" in r.headers["location"]
        html = c.get("/memory").text
        assert "I like &lt;b&gt;tea&lt;/b&gt;" in html and "you added it on" in html
        c.post(
            "/memory", data={"action": "edit", "memory_id": 1, "text": "I like chai", "token": "T"}
        )
        assert "I like chai" in c.get("/memory").text
        c.post("/memory", data={"action": "off", "token": "T"})
        assert "nothing is saved or used" in c.get("/memory").text
        c.post("/memory", data={"action": "delete", "memory_id": 1, "token": "T"})
        assert "Nothing remembered yet" in c.get("/memory").text
    with sqlite3.connect(settings.db_path) as db:
        kinds = [r[0] for r in db.execute("SELECT kind FROM audit_log ORDER BY id")]
    assert kinds == ["memory.saved", "memory.edited", "memory.switch", "memory.forgotten"]
