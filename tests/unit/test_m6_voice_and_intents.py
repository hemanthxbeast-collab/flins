"""M6: voice loop (with fake mic / speech model / speaker) and Tier-B commands with confirmation."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

from flins.adapters.base import ContactInfo, ContactsUpsert, InboundMessage, MeInfo, SidecarHello
from flins.core.approvals import ApprovalService
from flins.core.audit import AuditLog
from flins.core.clock import FrozenClock
from flins.core.commands import HELP, CommandRouter
from flins.core.db import connect, migrate
from flins.core.events import EventBus
from flins.core.ingest import Ingestor
from flins.core.intents import IntentParser, find_contacts
from flins.core.levels import Mode
from flins.core.modes import ModeManager
from flins.core.policy_schema import Policy
from flins.llm.gateway import FakeBackend, LLMGateway
from flins.llm.prompts import load_prompt
from flins.llm.providers import ConfiguredRouter, Route
from flins.voice.audio import Recording
from flins.voice.loop import NOT_HEARD, VoiceLoop
from flins.voice.stt import Transcript
from flins.voice.tts import PrintTTS, speakable
from tests.conftest import REPO_ROOT

T0 = datetime(2026, 11, 9, 4, 30, tzinfo=UTC)  # 10:00 IST
ME = "919000000000@s.whatsapp.net"
RADHIKA = "919666666666@s.whatsapp.net"
MOM = "919222222222@s.whatsapp.net"


def intent(action: str, **kw: Any) -> dict[str, Any]:
    base = {
        "action": action,
        "mode": None,
        "duration_or_time": None,
        "number": None,
        "contact": None,
        "text": None,
        "confidence": 0.9,
    }
    return {**base, **kw}


@dataclass
class Rig:
    conn: aiosqlite.Connection
    clock: FrozenClock
    modes: ModeManager
    router: CommandRouter
    backend: FakeBackend
    ingest: Ingestor

    def answer(self, value: Any) -> None:
        self.backend.answers["intent"] = value

    async def say(self, text: str, source: str = "voice") -> str | None:
        return await self.router.handle_text(text, source=source)

    async def outbox(self) -> list[aiosqlite.Row]:
        async with self.conn.execute("SELECT * FROM outbox ORDER BY id") as cur:
            return list(await cur.fetchall())


@pytest.fixture
async def rig(tmp_path: Path, example_policy_dict: dict[str, Any]) -> AsyncIterator[Rig]:
    policy = Policy.model_validate(example_policy_dict)
    db = tmp_path / "t.db"
    conn = await connect(db)
    await migrate(conn)
    clock = FrozenClock(T0)
    bus = EventBus()
    backend = FakeBackend()
    gw = LLMGateway(backend, conn, clock, daily_budget_usd=1.0, tz=ZoneInfo("Asia/Kolkata"))
    modes = await ModeManager.open(db, bus, policy, clock)
    router = CommandRouter(
        modes,
        policy,
        clock,
        ApprovalService(conn, policy),
        intents=IntentParser(gw, load_prompt(REPO_ROOT / "prompts", "intent")),
        conn=conn,
    )
    ingest = Ingestor(conn, bus, AuditLog(conn))
    await ingest.handle(SidecarHello(sidecar_version="t", state="open", me=MeInfo(id=ME)))
    await ingest.handle(
        ContactsUpsert(
            contacts=[
                ContactInfo(pn=RADHIKA, name="Radhika K"),
                ContactInfo(pn=MOM, name="Mom"),
            ]
        )
    )
    # Radhika wrote recently, so FLINS may message her.
    await ingest.handle(
        InboundMessage(
            wa_msg_id="y1",
            chat_jid=RADHIKA,
            sender_jid=RADHIKA,
            text="Hi",
            ts_ms=int(clock.now().timestamp() * 1000),
            type="text",
        )
    )
    yield Rig(conn, clock, modes, router, backend, ingest)
    await modes.close()
    await conn.close()


# ------------------------------------------------------------------ Tier B + confirmation


async def test_fixed_phrases_never_need_the_ai(rig: Rig) -> None:
    reply = await rig.say("working mood for 1h")
    assert reply is not None and "Working mode on" in reply
    assert rig.backend.calls == []


async def test_send_is_read_back_and_waits_for_yes(rig: Rig) -> None:
    rig.answer(intent("send", contact="radhika", text="I'll call you at 8"))
    question = await rig.say("reply to radhika that I'll call her at 8")
    assert question == (
        'Send to Radhika K: "Hemanth asked me to tell you: I\'ll call you at 8"? Say yes or no.'
    )
    assert await rig.outbox() == []  # nothing yet
    done = await rig.say("yes")
    assert done is not None and done.startswith("Sending to Radhika K")
    row = (await rig.outbox())[0]
    assert row["chat_jid"] == RADHIKA
    assert row["text"].startswith("Hemanth asked me to tell you: I'll call you at 8")
    assert row["text"].endswith("— FLINS, Hemanth's assistant")  # FLINS != Hemanth


async def test_no_cancels(rig: Rig) -> None:
    rig.answer(intent("send", contact="radhika", text="ok"))
    await rig.say("tell radhika ok")
    assert await rig.say("no") == "Okay, I won't."
    assert await rig.outbox() == []


async def test_confirmation_expires(rig: Rig) -> None:
    rig.answer(intent("send", contact="radhika", text="ok"))
    await rig.say("tell radhika ok")
    rig.clock.advance(timedelta(seconds=61))
    rig.answer(intent("unknown", confidence=0.9))
    reply = await rig.say("yes")
    assert reply is None or not reply.startswith("Sending")
    assert await rig.outbox() == []


async def test_another_command_drops_the_question(rig: Rig) -> None:
    rig.answer(intent("send", contact="radhika", text="ok"))
    await rig.say("tell radhika ok")
    reply = await rig.say("status")  # Tier A, handled normally
    assert reply is not None and "Normal mode" in reply
    rig.answer(intent("unknown", confidence=0.9))
    await rig.say("yes")
    assert await rig.outbox() == []


async def test_send_to_someone_who_hasnt_written_is_refused(rig: Rig) -> None:
    rig.answer(intent("send", contact="mom", text="I'll be late"))
    await rig.say("tell mom I'll be late")
    reply = await rig.say("yes")
    assert reply is not None and "24 hours" in reply


async def test_unknown_contact_and_missing_message(rig: Rig) -> None:
    rig.answer(intent("send", contact="zorro", text="hi"))
    assert await rig.say("tell zorro hi") == (
        "I couldn't find a contact called zorro. Who should I send it to?"
    )
    await rig.say("never mind")
    rig.answer(intent("send", contact="radhika", text=None))
    assert await rig.say("reply to radhika") == "What should I tell Radhika K?"


async def test_mode_change_is_confirmed(rig: Rig) -> None:
    rig.answer(intent("mode", mode="focus", duration_or_time="for 2 hours"))
    question = await rig.say("I need to concentrate for a couple of hours")
    assert question == "Switch to Focus mode until 12:00? Say yes or no."
    assert rig.modes.state.mode == Mode.NORMAL
    reply = await rig.say("haan")
    assert reply is not None and "Focus mode on until 12:00" in reply


async def test_stop_is_immediate(rig: Rig) -> None:
    rig.answer(intent("stop"))
    reply = await rig.say("shut up for now")
    assert reply is not None and "Paused" in reply
    assert rig.modes.state.mode == Mode.PAUSED


async def test_unknown_is_silent_in_self_chat_but_answered_by_voice(rig: Rig) -> None:
    rig.answer(intent("unknown", confidence=0.9))
    assert await rig.say("buy milk", source="self_chat") is None  # a note to yourself
    assert await rig.say("buy milk", source="voice") == HELP


async def test_low_confidence_counts_as_unknown(rig: Rig) -> None:
    rig.answer(intent("send", contact="radhika", text="?", confidence=0.2))
    assert await rig.say("hmm radhika maybe", source="self_chat") is None


async def test_cli_never_uses_tier_b(rig: Rig) -> None:
    rig.answer(intent("stop"))
    assert await rig.say("please be quiet", source="cli") is None
    assert rig.backend.calls == []


async def test_ai_outage_is_handled(rig: Rig) -> None:
    from flins.llm.gateway import LLMError

    rig.answer(LLMError("down"))
    assert await rig.say("tell radhika hi", source="self_chat") is None
    assert await rig.say("tell radhika hi") == "Sorry, I can't understand that right now."


async def test_find_contacts(rig: Rig) -> None:
    assert [m.name for m in await find_contacts(rig.conn, "radhika")] == ["Radhika K"]
    assert [m.name for m in await find_contacts(rig.conn, "Radika")] == ["Radhika K"]
    assert await find_contacts(rig.conn, "") == []


def test_intent_job_uses_triage_providers() -> None:
    fake = FakeBackend()
    router = ConfiguredRouter({"triage": [Route("groq", "openai/gpt-oss-20b")]}, {"groq": fake})
    chain = router.chain(load_prompt(REPO_ROOT / "prompts", "intent"))
    assert [(a.provider, a.prompt.model) for a in chain] == [("groq", "openai/gpt-oss-20b")]


# ------------------------------------------------------------------ voice loop


class FakeRecorder:
    def __init__(self, heard: bool = True) -> None:
        self.heard = heard

    async def record(self, cancel: asyncio.Event | None = None) -> Recording:
        return Recording(audio=[0.0], speech_end=time.monotonic(), heard_speech=self.heard)


class FakeSTT:
    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe(self, audio: Any, hint: str | None = None) -> Transcript:
        self.hint = hint
        return Transcript(self.text, "en", 0.01)


async def test_voice_turn_end_to_end(rig: Rig) -> None:
    tts = PrintTTS()
    loop = VoiceLoop(
        rig.router,
        FakeRecorder(),
        FakeSTT("I'm in working mood till 6pm"),
        tts,
        signature="— FLINS, Hemanth's assistant",
        announce=lambda _: None,
        beep_fn=lambda: None,
    )
    turn = await loop.turn()
    assert turn is not None and turn.heard == "I'm in working mood till 6pm"
    assert rig.modes.state.mode == Mode.WORKING
    assert tts.said and tts.said[0].startswith("Working mode on until 18:00")
    assert turn.latency_s < 2.5


async def test_voice_nothing_heard(rig: Rig) -> None:
    tts = PrintTTS()
    loop = VoiceLoop(
        rig.router,
        FakeRecorder(heard=False),
        FakeSTT(""),
        tts,
        announce=lambda _: None,
        beep_fn=lambda: None,
    )
    assert await loop.turn() is None
    assert tts.said == [NOT_HEARD]


@pytest.mark.parametrize(
    ("note", "spoken"),
    [
        ('🤖 #3 Kavya: "hi"\nDraft: "Hey!"', 'number 3 Kavya: "hi". Draft: "Hey!".'),
        (
            'Back to normal.\n• ⚠ Rahul: "call" → told you',
            'Back to normal. Rahul: "call" : told you.',
        ),
        ("Sent. — FLINS, Hemanth's assistant", "Sent."),
    ],
)
def test_speakable(note: str, spoken: str) -> None:
    assert speakable(note, signature="— FLINS, Hemanth's assistant") == spoken


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Approved number 7", ("approve", 7)),
        ("approve number seven", ("approve", 7)),
        ("reject no. 3", ("reject", 3)),
        ("y 4", ("approve", 4)),
    ],
)
def test_spoken_approvals(text: str, expected: tuple[str, int]) -> None:
    from zoneinfo import ZoneInfo as Z

    from flins.core.grammar import ApprovalCommand, parse

    cmd = parse(text, now=T0, tz=Z("Asia/Kolkata"))
    assert isinstance(cmd, ApprovalCommand) and (cmd.op, cmd.number) == expected


async def test_contact_names_as_misheard_by_speech_to_text(rig: Rig) -> None:
    for heard in ("Radhiga", "Radhi Ga", "radhika k"):
        assert [m.name for m in await find_contacts(rig.conn, heard)] == ["Radhika K"], heard


# ------------------------------------------------------------------ accent improvements


@pytest.mark.parametrize(
    ("a", "b"),
    [("Rahul", "roul"), ("Rahul", "raul"), ("Radhika", "radika"), ("Rohan", "rohaan")],
)
def test_sound_alike_names(a: str, b: str) -> None:
    from flins.core.intents import sound_key

    assert sound_key(a) == sound_key(b)


async def test_contact_by_sound_and_by_number(rig: Rig) -> None:
    await rig.ingest.handle(
        ContactsUpsert(contacts=[ContactInfo(pn="919111111111@s.whatsapp.net", name="Rahul")])
    )
    assert [m.name for m in await find_contacts(rig.conn, "roul")] == ["Rahul"]
    assert [m.name for m in await find_contacts(rig.conn, "+91 96666 66666")] == ["Radhika K"]
    assert [m.name for m in await find_contacts(rig.conn, "9666666666")] == ["Radhika K"]


async def test_intent_gets_contact_names_but_no_numbers(rig: Rig) -> None:
    rig.answer(intent("send", contact="Radhika K", text="hi"))
    await rig.say("tell radhi ga hi")
    sent = rig.backend.calls[-1][1]
    assert "<contacts>" in sent and "Radhika K" in sent
    assert "966666" not in sent and "@s.whatsapp.net" not in sent


async def test_names_are_passed_to_speech_model_and_misheard_text_is_echoed(rig: Rig) -> None:
    from flins.core.intents import contact_names

    async def hint() -> str:
        return "People: " + ", ".join(await contact_names(rig.conn)) + "."

    stt = FakeSTT("blah blah")
    tts = PrintTTS()
    rig.answer(intent("unknown", confidence=0.9))
    loop = VoiceLoop(
        rig.router,
        FakeRecorder(),
        stt,
        tts,
        announce=lambda _: None,
        beep_fn=lambda: None,
        hint_source=hint,
    )
    turn = await loop.turn()
    assert "Radhika K" in (stt.hint or "")
    assert turn is not None and turn.reply.startswith('I heard "blah blah". Sorry')


def test_accuracy_presets(example_policy_dict: dict[str, Any], project: Path) -> None:
    from flins.settings import load_settings
    from flins.voice.factory import make_stt

    settings = load_settings(project)
    stt = make_stt(settings)
    assert stt.model_name == "small" and stt._beam_size == 5 and stt._language == "en"
    settings.app.voice.accuracy = "fast"
    assert make_stt(settings).model_name == "base"
    settings.app.voice.stt_model = "medium"
    assert make_stt(settings).model_name == "medium"


# ------------------------------------------------------------------ follow-up questions


async def test_your_next_words_answer_what_should_i_tell(rig: Rig) -> None:
    """The exact conversation from the first voice session."""
    rig.answer(intent("send", contact="Radhika K", text=None))
    assert await rig.say("Send them a message to Radika HD.") == "What should I tell Radhika K?"
    # The model readdresses the message to her ("him" -> "you").
    rig.answer(intent("send", contact="Radhika K", text="I'll call you back at 8 o'clock"))
    question = await rig.say("I'll call him back at 8 o'clock.")
    assert question == (
        "Send to Radhika K: \"Hemanth asked me to tell you: I'll call you back at 8 o'clock\"? "
        "Say yes or no."
    )
    done = await rig.say("yes")
    assert done is not None and done.startswith("Sending to Radhika K")
    assert "call you back at 8" in (await rig.outbox())[0]["text"]


async def test_who_should_i_send_it_to(rig: Rig) -> None:
    rig.answer(intent("send", contact=None, text="I'll be late"))
    assert await rig.say("send a message saying I'll be late") == "Who should I send it to?"
    question = await rig.say("Radhika")  # found directly, no AI needed for the name
    assert question is not None and question.startswith('Send to Radhika K: "Hemanth asked')


async def test_follow_up_by_phone_number(rig: Rig) -> None:
    rig.answer(intent("send", contact="9666666666", text=None))
    assert await rig.say("send a message to the number 9666666666") == (
        "What should I tell Radhika K?"
    )


async def test_a_clear_command_overrides_the_open_question(rig: Rig) -> None:
    rig.answer(intent("send", contact="Radhika K", text=None))
    await rig.say("message radhika")
    reply = await rig.say("stop")
    assert reply is not None and "Paused" in reply
    assert await rig.outbox() == []


async def test_never_mind_and_expiry(rig: Rig) -> None:
    rig.answer(intent("send", contact="Radhika K", text=None))
    await rig.say("message radhika")
    assert await rig.say("never mind") == "Okay, never mind."
    await rig.say("message radhika")
    rig.clock.advance(timedelta(seconds=61))
    rig.answer(intent("unknown", confidence=0.9))
    assert await rig.say("I'll call at 8") == HELP  # the question had lapsed


async def test_until_when(rig: Rig) -> None:
    await rig.say("working mood for 1h")
    rig.answer(intent("extend", duration_or_time=None))
    assert await rig.say("can you leave working mode on a bit more") == (
        "Until when? For example 'till 7pm' or 'one more hour'."
    )
    question = await rig.say("till 2pm")
    assert question == "Keep the mode on until 14:00? Say yes or no."
    reply = await rig.say("yes")
    assert reply is not None and "extended until 14:00" in reply
