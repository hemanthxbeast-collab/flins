"""Voice conversation loop (M6, extended in JARVIS phase 5, docs/milestones/J5.md).

hotkey or wake word -> beep -> record -> speech-to-text -> assistant -> spoken answer, then:
- follow-up: if FLINS asked you something, it listens again by itself (no hotkey needed);
- barge-in: press the hotkey while FLINS is talking and it stops and listens.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from flins.voice.audio import Recorder, beep
from flins.voice.stt import STT
from flins.voice.tts import TTS, speakable

NOT_HEARD = "I didn't hear anything."
NOT_UNDERSTOOD = "Sorry, I didn't get that."
MAX_CHAIN = 6  # follow-ups after one hotkey press / wake word, so it can't listen forever

FollowUp = Literal["off", "question", "always"]

# Said in a follow-up, these just end the conversation (unless FLINS is waiting for a yes/no).
_BYE = re.compile(
    r"^\W*(?:(?:no|ok|okay)[,.\s]+)?"
    r"(?:that'?s (?:all|it)|that is (?:all|it)|thanks?(?: you)?(?: flins)?|thank you(?: flins)?|"
    r"never ?mind|nothing(?: else)?|bye|goodbye|i'?m good|all good|ok|okay|leave it)\W*$",
    re.IGNORECASE,
)
_ASKS = re.compile(r"\?\s*$|\bsay yes or no\b", re.IGNORECASE)


def soft_beep() -> None:
    """A lower, shorter tone: FLINS is listening for your answer."""
    beep(660, 70)


class _Router:
    async def handle_text(self, text: str, *, source: str) -> str | None: ...


@dataclass
class Turn:
    heard: str
    reply: str
    stt_s: float
    think_s: float
    latency_s: float  # from the end of your speech to the start of the spoken answer


class VoiceLoop:
    def __init__(
        self,
        router: _Router,
        recorder: Recorder,
        stt: STT,
        tts: TTS,
        *,
        signature: str | None = None,
        announce: Callable[[str], None] = print,
        beep_fn: Callable[[], None] = beep,
        soft_beep_fn: Callable[[], None] = soft_beep,
        hint_source: Callable[[], Awaitable[str]] | None = None,
        follow_up: FollowUp = "off",
        follow_up_s: float = 6.0,
        awaiting: Callable[[], bool] | None = None,
    ) -> None:
        self._hint_source = hint_source
        self._router = router
        self._recorder = recorder
        self._stt = stt
        self._tts = tts
        self._signature = signature
        self._announce = announce
        self._beep = beep_fn
        self._soft_beep = soft_beep_fn
        self._follow_up = follow_up
        self._follow_up_s = follow_up_s
        self._awaiting = awaiting  # True while FLINS waits for your yes/no or an answer
        self._in_turn = False
        self._speaking_n = 0
        self._barged = False
        self.quiet_since = 0.0  # time.monotonic() when FLINS last stopped talking
        self._cancel = asyncio.Event()
        self.turns: list[Turn] = []

    @property
    def busy(self) -> bool:
        """Listening for a command or talking: the wake word ignores the mic meanwhile."""
        return self._in_turn or self.speaking

    @property
    def speaking(self) -> bool:
        return self._speaking_n > 0

    async def say(self, text: str) -> None:
        """Speak anything (alerts, summaries). Never raises: speech must not break FLINS."""
        self._speaking_n += 1
        try:
            await self._tts.say(speakable(text, signature=self._signature))
        except Exception as exc:  # pragma: no cover - depends on the sound system
            self._announce(f"-- voice output failed: {type(exc).__name__}: {exc}")
        finally:
            self._speaking_n -= 1
            self.quiet_since = time.monotonic()

    def interrupt(self) -> bool:
        """Barge-in: stop talking now. Inside a conversation FLINS then listens to you."""
        if not self.speaking:
            return False
        self._barged = self._in_turn
        try:
            self._tts.stop()
        except Exception as exc:  # pragma: no cover - depends on the sound system
            self._announce(f"-- couldn't stop speaking: {type(exc).__name__}: {exc}")
        return True

    async def turn(self) -> Turn | None:
        """One conversation started by the hotkey or the bare wake word. Returns its first
        exchange (follow-ups are in `self.turns`)."""
        return await self._conversation(None)

    async def command(self, heard: str, speech_end: float) -> Turn:
        """A command already heard (the wake word with words after it: "FLINS, status")."""
        first = await self._conversation((heard, speech_end))
        assert first is not None
        return first

    async def _conversation(self, already: tuple[str, float] | None) -> Turn | None:
        self._in_turn = True
        self._barged = False
        self._cancel.clear()
        try:
            if already is None:
                first = await self._listen(follow_up=False)
            else:
                self._announce(f"   YOU (voice): {already[0]}")
                first = await self._respond(already[0], 0.0, already[1])
            last, extra = first, 0
            while extra < MAX_CHAIN and not self._cancel.is_set():
                barged, self._barged = self._barged, False
                if not barged and not (last is not None and self._listen_again(last.reply)):
                    break
                extra += 1
                last = await self._listen(follow_up=True, loud=barged)
                if last is None and not self._barged:
                    break
            return first
        finally:
            self._in_turn = False
            self._barged = False

    def _waiting_for_answer(self) -> bool:
        try:
            return bool(self._awaiting and self._awaiting())
        except Exception:
            return False

    def _listen_again(self, reply: str) -> bool:
        if self._follow_up == "off":
            return False
        if self._follow_up == "always":
            return True
        return self._waiting_for_answer() or bool(_ASKS.search(reply))

    async def _listen(self, *, follow_up: bool, loud: bool = True) -> Turn | None:
        """Beep, record, transcribe, answer. In a follow-up, silence or "that's all" just ends
        the conversation quietly."""
        tone = self._beep if loud or not follow_up else self._soft_beep
        await asyncio.get_running_loop().run_in_executor(None, tone)
        if follow_up:
            rec = await self._recorder.record(self._cancel, start_timeout_s=self._follow_up_s)
        else:
            rec = await self._recorder.record(self._cancel)
        if self._cancel.is_set():
            return None
        if not rec.heard_speech:
            if not follow_up:
                await self.say(NOT_HEARD)
            return None
        hint = None
        if self._hint_source is not None:
            try:
                hint = await self._hint_source()
            except Exception:  # hints are a bonus; never fail a turn over them
                hint = None
        transcript = await self._stt.transcribe(rec.audio, hint)
        heard = transcript.text.strip()
        if not heard:
            if not follow_up:
                await self.say(NOT_UNDERSTOOD)
            return None
        self._announce(f"   YOU (voice): {heard}")
        if follow_up and _BYE.match(heard) and not self._waiting_for_answer():
            return None
        return await self._respond(heard, transcript.seconds, rec.speech_end)

    async def _respond(self, heard: str, stt_s: float, speech_end: float) -> Turn:
        think_started = time.monotonic()
        reply = await self._router.handle_text(heard, source="voice") or NOT_UNDERSTOOD
        if reply.startswith(("Sorry", "I couldn't find")):
            reply = f'I heard "{heard}". {reply}'  # so you can tell what went wrong
        think = time.monotonic() - think_started
        self._announce(f"   FLINS: {reply}")
        latency = time.monotonic() - speech_end
        result = Turn(heard, reply, stt_s, think, latency)
        self.turns.append(result)
        await self.say(reply)
        return result

    async def run(self, presses: asyncio.Queue[None]) -> None:
        """Handle hotkey presses forever. A press while FLINS is talking stops it and listens;
        a press while it is listening cancels that recording."""
        current: asyncio.Task[Turn | None] | None = None
        while True:
            await presses.get()
            running = current is not None and not current.done()
            if self.interrupt():
                if not running:  # it was speaking an alert, not answering you
                    current = asyncio.create_task(self._safe_turn())
                continue
            if running:
                self._cancel.set()
                continue
            current = asyncio.create_task(self._safe_turn())

    async def _safe_turn(self) -> Turn | None:
        try:
            return await self.turn()
        except Exception as exc:  # a broken mic or model must not stop FLINS
            self._announce(f"-- voice error: {type(exc).__name__}: {exc}")
            return None
