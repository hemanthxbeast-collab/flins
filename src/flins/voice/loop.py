"""Push-to-talk loop: hotkey -> beep -> record -> speech-to-text -> command -> spoken answer."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
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
    ) -> None:
        self._router = router
        self._recorder = recorder
        self._stt = stt
        self._tts = tts
        self._signature = signature
        self._announce = announce
        self._beep = beep_fn
        self._busy = False
        self._cancel = asyncio.Event()
        self.turns: list[Turn] = []

    async def say(self, text: str) -> None:
        """Speak anything (alerts, summaries). Never raises: speech must not break FLINS."""
        try:
            await self._tts.say(speakable(text, signature=self._signature))
        except Exception as exc:  # pragma: no cover - depends on the sound system
            self._announce(f"-- voice output failed: {type(exc).__name__}: {exc}")

    async def turn(self) -> Turn | None:
        self._cancel.clear()
        await asyncio.get_running_loop().run_in_executor(None, self._beep)
        rec = await self._recorder.record(self._cancel)
        if self._cancel.is_set():
            return None
        if not rec.heard_speech:
            await self.say(NOT_HEARD)
            return None
        transcript = await self._stt.transcribe(rec.audio)
        heard = transcript.text.strip()
        if not heard:
            await self.say(NOT_UNDERSTOOD)
            return None
        self._announce(f"   YOU (voice): {heard}")
        think_started = time.monotonic()
        reply = await self._router.handle_text(heard, source="voice") or NOT_UNDERSTOOD
        think = time.monotonic() - think_started
        self._announce(f"   FLINS: {reply}")
        latency = time.monotonic() - rec.speech_end
        result = Turn(heard, reply, transcript.seconds, think, latency)
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
