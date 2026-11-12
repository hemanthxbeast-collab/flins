"""Wake word: say "FLINS" instead of pressing the hotkey (SPEC §14 M9).

`openwakeword` can't be installed on Python 3.13 and has no "FLINS" model, so this uses what
FLINS already has. A mic listener wakes only on speech (loudness), cuts short bursts (up to 4
s), and Whisper `tiny` transcribes each one locally. If it starts with "FLINS" (or a sound-alike),
FLINS listens for your command; "FLINS, status" works in one breath. To cut false wakes, the
main speech model then checks the same audio and must hear "FLINS" too (phase 5). Audio is
never saved, and nothing leaves the PC. While FLINS is talking or listening, the mic is
ignored, so it can't wake itself.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from flins.voice.audio import BLOCK_MS, SAMPLE_RATE
from flins.voice.loop import VoiceLoop
from flins.voice.stt import STT

WAKE_HINT = "FLINS. FLINS, status. FLINS, I'm in working mood."
_WAKE = re.compile(
    r"^\W*(?:(?:hey|hi|ok|okay|oh)[\s,]+)?"
    r"(?:flins|flints|flinz|flin'?s|flinns|flynns|fleens|plins|blins|flins\w?)\b"
    r"[\s,.!?:;-]*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def match_wake(text: str) -> str | None:
    """None if the words don't start with the wake word, else whatever followed it."""
    m = _WAKE.match(text.strip())
    if m is None:
        return None
    return m.group("rest").strip().strip(".!?,").strip()


@dataclass
class Burst:
    audio: Any
    start: float  # time.monotonic()
    end: float


class BurstSource(Protocol):
    async def next(self) -> Burst: ...


class MicBursts:
    """Short bursts of speech from the microphone, cut by loudness. Runs in a thread."""

    def __init__(
        self,
        *,
        device: int | str | None = None,
        min_s: float = 0.25,
        max_s: float = 4.0,
        silence_s: float = 0.45,
        min_level: float = 0.015,
    ) -> None:
        self._device = device
        self._min_s = min_s
        self._max_s = max_s
        self._silence_s = silence_s
        self._min_level = min_level
        self._queue: asyncio.Queue[Burst] = asyncio.Queue(maxsize=4)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._listen, args=(loop,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    async def next(self) -> Burst:
        return await self._queue.get()

    def _put(self, burst: Burst) -> None:
        if not self._queue.full():  # if FLINS is behind, drop old speech rather than queue it
            self._queue.put_nowait(burst)

    def _listen(self, loop: asyncio.AbstractEventLoop) -> None:
        import numpy as np
        import sounddevice as sd

        block = int(SAMPLE_RATE * BLOCK_MS / 1000)
        noise = self._min_level / 3
        chunks: list[Any] = []
        start = last_voice = 0.0
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=block,
            device=self._device,
        ) as stream:
            while not self._stop.is_set():
                data, _ = stream.read(block)
                chunk = data[:, 0].copy()
                now = time.monotonic()
                level = float(np.sqrt(np.mean(chunk**2)))
                loud = level > max(self._min_level, 3.0 * noise)
                if not chunks:
                    if loud:
                        chunks, start, last_voice = [chunk], now, now
                    else:
                        noise = 0.95 * noise + 0.05 * level  # follow the room's background
                    continue
                chunks.append(chunk)
                if loud:
                    last_voice = now
                ended = now - last_voice >= self._silence_s
                if ended or now - start >= self._max_s:
                    if last_voice - start >= self._min_s:
                        audio = np.concatenate(chunks)
                        burst = Burst(audio, start, last_voice)
                        loop.call_soon_threadsafe(self._put, burst)
                    chunks = []


class WakeWord:
    def __init__(
        self,
        source: BurstSource,
        stt: STT,
        voice: VoiceLoop,
        *,
        announce: Callable[[str], None] = print,
        cooldown_s: float = 1.0,
        confirm: STT | None = None,
    ) -> None:
        self._source = source
        self._stt = stt
        self._voice = voice
        self._announce = announce
        self._cooldown_s = cooldown_s
        # Phase 5: the small model only *spots* a possible wake word. When a better model is
        # given, it must hear the wake word too, and its words are the command.
        self._confirm = confirm
        self.wakes = 0
        self.rejected = 0  # the small model heard "FLINS", the better one didn't

    async def run(self) -> None:
        while True:
            burst = await self._source.next()
            try:
                await self.handle(burst)
            except Exception as exc:  # the wake word must never take FLINS down
                self._announce(f"-- wake word error: {type(exc).__name__}: {exc}")

    async def handle(self, burst: Burst) -> bool:
        """Returns True if the burst woke FLINS."""
        v = self._voice
        if v.busy or burst.start < v.quiet_since + self._cooldown_s:
            return False  # FLINS was talking or already listening: ignore (no self-wake)
        transcript = await self._stt.transcribe(burst.audio, WAKE_HINT)
        rest = match_wake(transcript.text)
        if rest is None or v.busy:
            return False
        if self._confirm is not None:
            second = await self._confirm.transcribe(burst.audio, WAKE_HINT)
            rest = match_wake(second.text)
            if rest is None or v.busy:
                self.rejected += 1
                return False
        self.wakes += 1
        if rest:
            await v.command(rest, burst.end)  # "FLINS, stop" / "FLINS, working mood till 6"
        else:
            await v.turn()  # just "FLINS": beep, then listen for the command
        return True
