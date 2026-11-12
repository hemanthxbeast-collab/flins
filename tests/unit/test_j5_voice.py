"""JARVIS phase 5: conversational voice (docs/milestones/J5.md).

Follow-up listening (FLINS asked, so it listens again by itself), barge-in (the hotkey cuts
FLINS off and it listens), a wake word the main speech model must confirm, and speech
recognition that picks a model the hardware can run. No microphone, speakers or model here.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from flins.settings import load_settings
from flins.voice import factory
from flins.voice.audio import Recording
from flins.voice.loop import MAX_CHAIN, NOT_HEARD, VoiceLoop
from flins.voice.stt import Transcript
from flins.voice.tts import PrintTTS, SapiTTS, sapi_rate
from flins.voice.wake import Burst, WakeWord
from tests.unit.test_j1_assistant import Rig, build
from tests.unit.test_j4_tools import ask
from tests.unit.test_m9 import FakeSTT, FakeVoice, NoSource


class Mic:
    """Each recording: you spoke (True) or stayed silent (False). Repeats the last value."""

    def __init__(self, *spoke: bool) -> None:
        self.script = list(spoke) or [True]
        self.timeouts: list[float | None] = []

    async def record(
        self, cancel: asyncio.Event | None = None, *, start_timeout_s: float | None = None
    ) -> Recording:
        self.timeouts.append(start_timeout_s)
        heard = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return Recording(audio=[0.0], speech_end=time.monotonic(), heard_speech=heard)


class Ears:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)

    async def transcribe(self, audio: Any, hint: str | None = None) -> Transcript:
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return Transcript(text, "en", 0.01)


class Brain:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.heard: list[str] = []
        self.waiting = False

    async def handle_text(self, text: str, *, source: str) -> str | None:
        self.heard.append(text)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    def awaiting(self, source: str) -> bool:
        return self.waiting


class SlowVoice:
    """Speech that takes until it is stopped (or `release()`d), like a long answer."""

    def __init__(self) -> None:
        self.said: list[str] = []
        self.stopped = 0
        self.started = asyncio.Event()
        self._done = asyncio.Event()

    async def say(self, text: str) -> None:
        self.said.append(text)
        self._done.clear()
        self.started.set()
        await self._done.wait()

    def stop(self) -> None:
        self.stopped += 1
        self._done.set()

    def release(self) -> None:
        self._done.set()


def voice(
    brain: Brain, mic: Mic, ears: Ears, tts: Any = None, **kw: Any
) -> tuple[VoiceLoop, list[str]]:
    beeps: list[str] = []
    loop = VoiceLoop(
        brain,
        mic,
        ears,
        tts or PrintTTS(),
        announce=lambda _: None,
        beep_fn=lambda: beeps.append("beep"),
        soft_beep_fn=lambda: beeps.append("soft"),
        awaiting=lambda: brain.awaiting("voice"),
        **kw,
    )
    return loop, beeps


# ---------------------------------------------------------------------- follow-up


async def test_flins_listens_again_when_it_asked_a_question() -> None:
    brain = Brain("Set a reminder to call the bank at 7? Say yes or no.", "Done, 7 pm.")
    mic = Mic(True)
    tts = PrintTTS()
    ears = Ears("remind me to call the bank at 7", "yes")
    loop, beeps = voice(brain, mic, ears, tts, follow_up="question", follow_up_s=5.0)
    first = await loop.turn()  # one hotkey press
    assert first is not None and first.heard == "remind me to call the bank at 7"
    assert brain.heard == ["remind me to call the bank at 7", "yes"]
    assert [t.reply for t in loop.turns] == [
        "Set a reminder to call the bank at 7? Say yes or no.",
        "Done, 7 pm.",
    ]
    assert mic.timeouts == [None, 5.0]  # the follow-up waits a limited time for you to start
    assert beeps == ["beep", "soft"] and not loop.busy


@pytest.mark.parametrize(
    ("mode", "reply", "exchanges"),
    [
        ("question", "It's 27 degrees in Pune.", 1),  # a plain answer: conversation over
        ("question", "Which city do you mean?", 2),
        ("off", "Which city do you mean?", 1),
        ("always", "It's 27 degrees in Pune.", 2),
    ],
)
async def test_follow_up_modes(mode: str, reply: str, exchanges: int) -> None:
    brain = Brain(reply, "Okay.")
    loop, _ = voice(brain, Mic(True, True, False), Ears("first", "second"), follow_up=mode)
    await loop.turn()
    assert len(loop.turns) == exchanges


async def test_default_voice_loop_has_no_follow_up() -> None:
    brain = Brain("Shall I? Say yes or no.")
    loop = VoiceLoop(brain, Mic(True), Ears("x"), PrintTTS(), beep_fn=lambda: None)
    await loop.turn()
    assert len(loop.turns) == 1


async def test_a_pending_yes_or_no_keeps_it_listening_even_without_a_question_mark() -> None:
    brain = Brain("I'll tell Rahul you're busy", "Sent.")
    brain.waiting = True
    loop, _ = voice(brain, Mic(True), Ears("tell rahul i'm busy", "okay"), follow_up="question")

    async def answered(text: str, *, source: str) -> str | None:
        brain.heard.append(text)
        if text == "okay":
            brain.waiting = False
            return "Sent."
        return "I'll tell Rahul you're busy"

    brain.handle_text = answered  # type: ignore[method-assign]
    await loop.turn()
    # "okay" would normally end a follow-up; while a yes/no is pending it is your answer
    assert brain.heard == ["tell rahul i'm busy", "okay"] and len(loop.turns) == 2


async def test_silence_in_a_follow_up_ends_quietly() -> None:
    brain = Brain("Which city do you mean?")
    tts = PrintTTS()
    loop, _ = voice(brain, Mic(True, False), Ears("weather"), tts, follow_up="question")
    await loop.turn()
    assert tts.said == ["Which city do you mean?"]  # no "I didn't hear anything"
    assert len(loop.turns) == 1

    quiet = PrintTTS()
    loop, _ = voice(Brain("x"), Mic(False), Ears("x"), quiet, follow_up="question")
    assert await loop.turn() is None and quiet.said == [NOT_HEARD]  # the first listen still says so


@pytest.mark.parametrize(
    "bye", ["that's all", "Thanks.", "thank you FLINS", "never mind", "No, that's it.", "Okay."]
)
async def test_saying_thanks_ends_the_conversation_without_asking_the_model(bye: str) -> None:
    brain = Brain("Anything else?")
    tts = PrintTTS()
    loop, _ = voice(brain, Mic(True), Ears("status", bye), tts, follow_up="question")
    await loop.turn()
    assert brain.heard == ["status"] and tts.said == ["Anything else?"]


async def test_follow_ups_are_limited() -> None:
    brain = Brain("And then?")
    loop, _ = voice(brain, Mic(True), Ears("go on"), follow_up="always")
    await loop.turn()
    assert len(loop.turns) == 1 + MAX_CHAIN  # it can't keep the microphone open forever


async def test_inline_wake_command_gets_follow_ups_too() -> None:
    brain = Brain("Pause everything? Say yes or no.", "Paused.")
    loop, beeps = voice(brain, Mic(True), Ears("yes"), follow_up="question")
    first = await loop.command("stop everything", time.monotonic())
    assert first.heard == "stop everything" and brain.heard == ["stop everything", "yes"]
    assert beeps == ["soft"]


# ---------------------------------------------------------------------- barge-in


async def test_hotkey_while_flins_is_talking_stops_it_and_listens() -> None:
    brain = Brain("A very long answer about thermodynamics.", "Short answer.")
    tts = SlowVoice()
    loop, beeps = voice(brain, Mic(True), Ears("explain entropy", "shorter please"), tts)
    presses: asyncio.Queue[None] = asyncio.Queue()
    runner = asyncio.create_task(loop.run(presses))
    try:
        presses.put_nowait(None)  # press: listen
        await asyncio.wait_for(tts.started.wait(), 2)
        assert loop.speaking and tts.stopped == 0
        tts.started.clear()
        presses.put_nowait(None)  # press again while it talks: barge in
        await asyncio.wait_for(tts.started.wait(), 2)  # it listened and is answering again
        assert tts.stopped == 1
        assert brain.heard == ["explain entropy", "shorter please"]
        assert beeps == ["beep", "beep"]  # a barge-in gets the normal listening tone
        tts.release()
        await asyncio.sleep(0.05)
        assert not loop.busy and len(loop.turns) == 2  # follow_up is off: it ends there
    finally:
        runner.cancel()


async def test_hotkey_stops_a_spoken_alert_and_starts_listening() -> None:
    brain = Brain("Mode is normal.")
    tts = SlowVoice()
    loop, _ = voice(brain, Mic(True), Ears("status"), tts)
    presses: asyncio.Queue[None] = asyncio.Queue()
    runner = asyncio.create_task(loop.run(presses))
    alert = asyncio.create_task(loop.say("Urgent message from someone, and it goes on and on."))
    try:
        await asyncio.wait_for(tts.started.wait(), 2)
        tts.started.clear()
        presses.put_nowait(None)
        await asyncio.wait_for(alert, 2)
        await asyncio.wait_for(tts.started.wait(), 2)
        assert tts.stopped == 1 and brain.heard == ["status"]
    finally:
        tts.release()
        runner.cancel()


async def test_interrupt_does_nothing_when_flins_is_silent() -> None:
    tts = PrintTTS()
    loop, _ = voice(Brain("x"), Mic(True), Ears("x"), tts)
    assert loop.interrupt() is False and tts.stopped == 0


class FakeSapi:
    """Stands in for the Windows SpVoice object: speech lasts until purged or `polls` run out."""

    def __init__(self, polls: int = 400) -> None:
        self.calls: list[tuple[str, int]] = []
        self.polls = polls
        self._left = 0

    def Speak(self, text: str, flags: int) -> None:
        self.calls.append((text, flags))
        self._left = 0 if flags & 2 else self.polls

    def WaitUntilDone(self, ms: int) -> bool:
        time.sleep(0.002)
        self._left -= 1
        return self._left <= 0


async def test_windows_voice_can_be_cut_off_and_speaks_again_afterwards() -> None:
    tts = SapiTTS()
    sapi = FakeSapi()
    tts._sapi = sapi
    first = asyncio.create_task(tts.say("A long sentence."))
    queued = asyncio.create_task(tts.say("Queued before the stop."))
    await asyncio.sleep(0.05)
    assert sapi.calls == [("A long sentence.", 1)]
    started = time.monotonic()
    tts.stop()
    await asyncio.wait_for(asyncio.gather(first, queued), 2)
    assert time.monotonic() - started < 0.5
    assert sapi.calls == [("A long sentence.", 1), ("", 3)]  # purged; the queued one dropped
    sapi.polls = 3
    await tts.say("After the stop.")
    assert sapi.calls[-1] == ("After the stop.", 1)
    await tts.say("   ")
    assert len(sapi.calls) == 3


def test_sapi_rate_matches_the_old_mapping() -> None:
    assert sapi_rate(185) == 1 and sapi_rate(157) == 0 and sapi_rate(130) == -1
    assert sapi_rate(400) <= 10 and sapi_rate(80) >= -10


# ---------------------------------------------------------------------- wake word


async def test_the_main_model_must_also_hear_the_wake_word() -> None:
    v = FakeVoice()
    spotter, main = FakeSTT("FLINS, stop."), FakeSTT("Flins, status.")
    wake = WakeWord(NoSource(), spotter, v, confirm=main)  # type: ignore[arg-type]
    assert await wake.handle(Burst(None, 100.0, 101.0))
    assert v.commands == ["status"]  # the better model's words are the command

    v2 = FakeVoice()
    doubt = FakeSTT("Friends, stop.")
    wake = WakeWord(NoSource(), FakeSTT("FLINS, stop."), v2, confirm=doubt)  # type: ignore[arg-type]
    assert not await wake.handle(Burst(None, 100.0, 101.0))
    assert v2.commands == [] and v2.turns == 0 and wake.rejected == 1 and wake.wakes == 0

    v3 = FakeVoice()
    never = FakeSTT("FLINS")
    wake = WakeWord(NoSource(), FakeSTT("turn it up"), v3, confirm=never)  # type: ignore[arg-type]
    assert not await wake.handle(Burst(None, 100.0, 101.0))
    assert never.calls == 0  # the main model only runs when the small one heard something


# ---------------------------------------------------------------------- model choice, wiring


def test_speech_model_fits_the_hardware(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings(project)
    v = settings.app.voice
    assert v.follow_up == "question" and v.wake_confirm is True
    v.accuracy, v.stt_device = "max", "cpu"
    stt = factory.make_stt(settings)
    assert (stt.model_name, stt.device) == ("small", "cpu")  # never a large model on the CPU
    v.stt_device = "cuda"
    stt = factory.make_stt(settings)
    assert (stt.model_name, stt.device, stt._compute_type) == ("large-v3-turbo", "cuda", "float16")
    v.stt_device = "auto"
    monkeypatch.setattr(factory, "gpu_available", lambda: False)
    assert factory.make_stt(settings).model_name == "small"
    monkeypatch.setattr(factory, "gpu_available", lambda: True)
    assert factory.make_stt(settings).device == "cuda"
    v.stt_model, v.stt_device = "large-v3", "cpu"  # an explicit model is your decision
    assert factory.make_stt(settings).model_name == "large-v3"


@pytest.fixture
async def rig(project: Path) -> AsyncIterator[Rig]:
    r = await build(project)
    yield r
    await r.modes.close()
    await r.conn.close()


async def test_a_spoken_yes_completes_a_confirmation_from_one_hotkey_press(rig: Rig) -> None:
    """End to end with the real assistant: ask for a note, FLINS asks, you say yes."""
    check = factory._awaiting(rig.assistant)
    assert check is not None and check() is False
    assert factory._awaiting(object()) is None
    rig.answers(chat=ask("note_add", "buy milk"))
    tts = PrintTTS()
    loop = VoiceLoop(
        rig.assistant,
        Mic(True),
        Ears("note down buy milk", "yes"),
        tts,
        announce=lambda _: None,
        beep_fn=lambda: None,
        soft_beep_fn=lambda: None,
        follow_up="question",
        awaiting=check,
    )
    await loop.turn()
    assert tts.said == ['Add this to your notes: "buy milk"? Say yes or no.', 'Noted: "buy milk".']
    assert "buy milk" in (rig.settings.data_dir / "notes.md").read_text(encoding="utf-8")
    assert check() is False and not loop.busy
