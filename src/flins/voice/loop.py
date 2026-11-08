"""Push-to-talk loop: hotkey -> beep -> record -> speech-to-text -> command -> spoken answer."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from flins.voice.audio import Recorder, beep
from flins.voice.stt import STT
from flins.voice.tts import TTS, speakable

NOT_HEARD = "I didn't hear anything."
NOT_UNDERSTOOD = "Sorry, I didn't get that."


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
        hint_source: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        self._hint_source = hint_source
        self._router = router
        self._recorder = recorder
        self._stt = stt
        self._tts = tts
        self._signature = signature
        self._announce = announce
        self._beep = beep_fn
        self._in_turn = False
        self._speaking = False
        self.quiet_since = 0.0  # time.monotonic() when FLINS last stopped talking
        self._cancel = asyncio.Event()
        self.turns: list[Turn] = []

    @property
    def busy(self) -> bool:
        """Listening for a command or talking: the wake word ignores the mic meanwhile."""
        return self._in_turn or self._speaking

    async def say(self, text: str) -> None:
        """Speak anything (alerts, summaries). Never raises: speech must not break FLINS."""
        self._speaking = True
        try:
            await self._tts.say(speakable(text, signature=self._signature))
        except Exception as exc:  # pragma: no cover - depends on the sound system
            self._announce(f"-- voice output failed: {type(exc).__name__}: {exc}")
        finally:
            self._speaking = False
            self.quiet_since = time.monotonic()

    async def turn(self) -> Turn | None:
        self._in_turn = True
        try:
            return await self._turn()
        finally:
            self._in_turn = False

    async def command(self, heard: str, speech_end: float) -> Turn:
        """A command already heard (the wake word with words after it: "FLINS, status")."""
        self._in_turn = True
        try:
            self._announce(f"   YOU (voice): {heard}")
            return await self._respond(heard, 0.0, speech_end)
        finally:
            self._in_turn = False

    async def _turn(self) -> Turn | None:
        self._cancel.clear()
        await asyncio.get_running_loop().run_in_executor(None, self._beep)
        rec = await self._recorder.record(self._cancel)
        if self._cancel.is_set():
            return None
        if not rec.heard_speech:
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
            await self.say(NOT_UNDERSTOOD)
            return None
        self._announce(f"   YOU (voice): {heard}")
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
        """Handle hotkey presses forever. A press while listening cancels that recording."""
        current: asyncio.Task[Turn | None] | None = None
        while True:
            await presses.get()
            if current is not None and not current.done():
                self._cancel.set()
                continue
            current = asyncio.create_task(self._safe_turn())

    async def _safe_turn(self) -> Turn | None:
        try:
            return await self.turn()
        except Exception as exc:  # a broken mic or model must not stop FLINS
            self._announce(f"-- voice error: {type(exc).__name__}: {exc}")
            return None
