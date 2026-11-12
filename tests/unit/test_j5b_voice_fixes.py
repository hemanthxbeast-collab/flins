"""Fixes after the first real voice session of phase 5 (0.17.1).

A spoken "Yes, add it to the notes" is a yes; a model can't send "list my notes" to the
approvals list or "summarise X's chat" to the debrief; "Hey friends" after the hotkey is
"Hey FLINS"; the local model server is started when a job needs it; stopping FLINS ends a
voice conversation in progress.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from flins.core.commands import said
from flins.llm.local import ensure_running
from flins.llm.prompts import load_prompt
from flins.settings import load_settings
from flins.voice import factory
from flins.voice.loop import fix_name
from flins.voice.tts import PrintTTS
from flins.wiring import ensure_local
from tests.conftest import REPO_ROOT
from tests.unit.test_j1_assistant import Rig, build, chat
from tests.unit.test_j4_tools import ask
from tests.unit.test_j5_voice import Brain, Ears, Mic, SlowVoice, voice
from tests.unit.test_m6_voice_and_intents import intent


@pytest.fixture
async def rig(project: Path) -> AsyncIterator[Rig]:
    r = await build(project)
    yield r
    await r.modes.close()
    await r.conn.close()


# ---------------------------------------------------------------------- yes / no


@pytest.mark.parametrize(
    ("text", "answer"),
    [
        ("yes", "yes"),
        ("Yes.", "yes"),
        ("Yes, add it to the notes.", "yes"),
        ("yeah go ahead", "yes"),
        ("Okay, do that", "yes"),
        ("sure, send it", "yes"),
        ("haan", "yes"),
        ("no", "no"),
        ("No, leave it.", "no"),
        ("nope not now", "no"),
        ("don't send that", "no"),
        ("cancel it", "no"),
        ("yes but make it 8", None),  # a change, not a plain yes: nothing runs
        ("yes, actually wait", None),
        ("okay, not now", None),
        ("yes change the time", None),
        ("yesterday I went out", None),
        ("what's the weather", None),
        ("", None),
        ("yes " + "and then " * 12, None),  # a long sentence isn't an answer
    ],
)
def test_said(text: str, answer: str | None) -> None:
    assert said(text) == answer


async def test_a_spoken_yes_with_extra_words_confirms(rig: Rig) -> None:
    rig.answers(chat=ask("note_add", "buy milk"))
    asked = await rig.say("note down buy milk", "voice")
    assert asked == 'Add this to your notes: "buy milk"? Say yes or no.'
    assert await rig.say("Yes, add it to the notes.", "voice") == 'Noted: "buy milk".'
    assert "buy milk" in (rig.settings.data_dir / "notes.md").read_text(encoding="utf-8")


async def test_yes_with_a_change_does_not_run_the_action(rig: Rig) -> None:
    rig.answers(chat=ask("note_add", "buy milk"))
    await rig.say("note down buy milk", "voice")
    rig.answers(chat=chat("Tell me the new text."))
    await rig.say("yes but make it buy bread", "voice")
    assert not (rig.settings.data_dir / "notes.md").exists()
    await rig.say("yes", "voice")  # the question has lapsed: a later yes confirms nothing
    assert not (rig.settings.data_dir / "notes.md").exists()


# ---------------------------------------------------------------------- misrouted read-outs


@pytest.mark.parametrize(
    ("action", "text"),
    [
        ("pending", "can you list the things whichever we noted in the note?"),
        ("pending", "show me everything"),
        ("summary", "what's in my notes"),
        ("summary", "tell me about thermodynamics"),
    ],
)
async def test_a_model_cannot_send_other_questions_to_approvals_or_the_debrief(
    rig: Rig, action: str, text: str
) -> None:
    rig.answers(intent=intent(action), chat=chat("From the conversation model."))
    assert await rig.say(text, "voice") == "From the conversation model."


@pytest.mark.parametrize(
    ("action", "text", "starts"),
    [
        ("pending", "what's pending?", "Nothing is waiting"),
        ("pending", "any drafts waiting for me", "Nothing is waiting"),
        ("pending", "show me what needs my approval", "Nothing is waiting"),
        ("summary", "what did I miss", ""),
        ("summary", "any new messages?", ""),
        ("summary", "catch me up", ""),
    ],
)
async def test_real_requests_for_approvals_and_the_debrief_still_work(
    rig: Rig, action: str, text: str, starts: str
) -> None:
    rig.answers(intent=intent(action), chat=chat("From the conversation model."))
    said_back = await rig.say(text, "voice")
    assert said_back and said_back != "From the conversation model."
    assert said_back.startswith(starts)


def test_intent_prompt_v2_sends_everything_else_to_unknown() -> None:
    p = load_prompt(REPO_ROOT / "prompts", "intent")
    assert p.version == "intent_v2"
    assert "When in doubt, choose unknown" in p.system and "NOT this" in p.system


# ---------------------------------------------------------------------- the name


@pytest.mark.parametrize(
    ("heard", "fixed"),
    [
        ("Hey friends.", "Hey FLINS."),
        ("Hey friends, what's the weather?", "Hey FLINS, what's the weather?"),
        ("Hi Flints, status", "Hi FLINS, status"),
        ("Big Fins.", "FLINS."),
        ("Flens, note down buy milk", "FLINS, note down buy milk"),
        ("Hey Flins, what's up", "Hey FLINS, what's up"),
        # not the name: left exactly as heard
        ("My friends are coming over", "My friends are coming over"),
        ("Hey friends are coming over", "Hey friends are coming over"),
        ("tell my friends I'm late", "tell my friends I'm late"),
        ("fins of a shark", "fins of a shark"),
        ("What's the weather?", "What's the weather?"),
    ],
)
def test_fix_name(heard: str, fixed: str) -> None:
    assert fix_name(heard) == fixed


async def test_the_voice_loop_passes_on_the_fixed_name() -> None:
    brain = Brain("Hey, what's up?")
    loop, _ = voice(brain, Mic(True, False), Ears("Hey friends."), follow_up="question")
    await loop.turn()
    assert brain.heard == ["Hey FLINS."]


# ---------------------------------------------------------------------- speech hints


async def test_your_vocabulary_is_given_to_the_speech_model(project: Path) -> None:
    settings = load_settings(project)
    assert settings.app.voice.vocabulary == []
    assert factory._hint(None, []) is None
    hint = factory._hint(None, ["NPTEL", " thermo ", ""])
    assert hint is not None and await hint() == "Words: NPTEL, thermo."


# ---------------------------------------------------------------------- stopping


async def test_stopping_flins_ends_a_voice_conversation_in_progress() -> None:
    tts = SlowVoice()
    loop, _ = voice(Brain("A long answer."), Mic(True), Ears("explain"), tts)
    presses: asyncio.Queue[None] = asyncio.Queue()
    runner = asyncio.create_task(loop.run(presses))
    presses.put_nowait(None)
    await asyncio.wait_for(tts.started.wait(), 2)
    runner.cancel()
    await asyncio.sleep(0.05)
    assert not loop.busy  # the turn was cancelled with the loop, not left talking


# ---------------------------------------------------------------------- local model server


def server(up_after: int) -> tuple[httpx.MockTransport, dict[str, int]]:
    """A model server that starts answering after `up_after` probes (0 = already running)."""
    seen = {"probes": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["probes"] += 1
        if seen["probes"] <= up_after:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"version": "0.9.9"})

    return httpx.MockTransport(handler), seen


async def test_local_models_are_started_only_when_needed() -> None:
    started: list[Path] = []
    exe = Path("C:/x/ollama.exe")
    url = "http://127.0.0.1:11434/v1"
    kw = {"spawn": started.append, "poll_s": 0.01, "wait_s": 0.1}

    transport, _ = server(0)
    assert await ensure_running(url, find=lambda: exe, transport=transport, **kw) == "running"
    assert started == []  # already up: nothing is started

    transport, _ = server(2)
    assert await ensure_running(url, find=lambda: exe, transport=transport, **kw) == "started"
    assert started == [exe]

    transport, _ = server(10**6)
    assert await ensure_running(url, find=lambda: None, transport=transport, **kw) == (
        "not installed"
    )
    assert await ensure_running(url, find=lambda: exe, transport=transport, **kw) == (
        "did not start"
    )
    assert len(started) == 2

    # A model server on another machine is never started from here.
    remote = "http://192.168.1.20:11434/v1"
    assert await ensure_running(remote, find=lambda: exe, transport=transport, **kw) == "not local"
    assert len(started) == 2

    def broken(path: Path) -> None:
        raise OSError("blocked")

    state = await ensure_running(url, find=lambda: exe, transport=transport, spawn=broken)
    assert state == "did not start"


async def test_nothing_is_started_when_no_job_uses_a_local_model(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flins import wiring

    async def never(base_url: str) -> str:
        raise AssertionError("should not be probed")

    monkeypatch.setattr(wiring, "ensure_running", never)
    settings = load_settings(project)  # the example config has no local jobs
    assert settings.app.llm.start_local is True
    assert await ensure_local(settings) == []


async def test_a_spoken_reply_is_never_lost_to_tts_stop_state() -> None:
    tts = PrintTTS()
    loop, _ = voice(Brain("One.", "Two."), Mic(True, True, False), Ears("a", "b"), tts)
    await loop.turn()
    await loop.turn()
    assert tts.said == ["One.", "Two."] and tts.stopped == 0


async def test_preload_loads_the_model_and_never_raises() -> None:
    from flins.llm.local import LocalModels

    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json={"done": True})

    models = LocalModels("http://127.0.0.1:11434/v1", transport=httpx.MockTransport(handler))
    assert await models.preload("qwen3.5:4b") is True
    assert b'"model":"qwen3.5:4b"' in bodies[0].replace(b" ", b"") and b"prompt" not in bodies[0]

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    gone = LocalModels("http://127.0.0.1:11434/v1", transport=httpx.MockTransport(down))
    assert await gone.preload("qwen3.5:4b") is False


async def test_no_preload_when_chat_is_not_local(project: Path) -> None:
    from flins.wiring import preload_local

    assert await preload_local(load_settings(project)) is None
