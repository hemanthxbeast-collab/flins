"""Microphone recording: start on the hotkey, stop when you stop talking."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

SAMPLE_RATE = 16_000
BLOCK_MS = 30


@dataclass
class Recording:
    audio: Any  # numpy float32 array, mono, 16 kHz (empty if nothing was said)
    speech_end: float  # time.monotonic() when you stopped talking
    heard_speech: bool


class Recorder(Protocol):
    async def record(
        self, cancel: asyncio.Event | None = None, *, start_timeout_s: float | None = None
    ) -> Recording: ...


class MicRecorder:
    """Records until ~`silence_s` of quiet after speech, `max_s` total, or nothing for
    `start_timeout_s`. Uses a simple loudness check; Whisper's own VAD trims the rest."""

    def __init__(
        self,
        *,
        device: int | str | None = None,
        max_s: float = 12.0,
        silence_s: float = 0.9,
        start_timeout_s: float = 5.0,
        min_level: float = 0.012,
    ) -> None:
        self._device = device
        self._max_s = max_s
        self._silence_s = silence_s
        self._start_timeout_s = start_timeout_s
        self._min_level = min_level

    async def record(
        self, cancel: asyncio.Event | None = None, *, start_timeout_s: float | None = None
    ) -> Recording:
        """`start_timeout_s`: how long to wait for you to start speaking (default: the
        recorder's own setting)."""
        stop = threading.Event()
        wait = self._start_timeout_s if start_timeout_s is None else start_timeout_s
        loop = asyncio.get_running_loop()
        if cancel is not None:
            watcher = asyncio.create_task(cancel.wait())
            watcher.add_done_callback(lambda _: stop.set())
        try:
            return await loop.run_in_executor(None, self._record_blocking, stop, wait)
        finally:
            if cancel is not None:
                watcher.cancel()

    def _record_blocking(self, stop: threading.Event, start_timeout_s: float) -> Recording:
        import numpy as np
        import sounddevice as sd

        block = int(SAMPLE_RATE * BLOCK_MS / 1000)
        chunks: list[Any] = []
        started_at = time.monotonic()
        noise: list[float] = []
        speaking = False
        last_voice = started_at
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=block,
            device=self._device,
        ) as stream:
            while not stop.is_set():
                data, _ = stream.read(block)
                chunk = data[:, 0].copy()
                chunks.append(chunk)
                now = time.monotonic()
                level = float(np.sqrt(np.mean(chunk**2)))
                if now - started_at < 0.3:
                    noise.append(level)  # first 300 ms: learn the room's background noise
                    continue
                threshold = max(self._min_level, 3.0 * (sum(noise) / max(len(noise), 1)))
                if level > threshold:
                    speaking = True
                    last_voice = now
                if speaking and now - last_voice >= self._silence_s:
                    break
                if not speaking and now - started_at >= start_timeout_s:
                    break
                if now - started_at >= self._max_s:
                    break
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype="float32")
        return Recording(audio if speaking else audio[:0], last_voice, speaking)


def beep(freq: int = 880, ms: int = 120) -> None:
    """A short tone so you know FLINS is listening (Windows only; silent elsewhere)."""
    try:
        import winsound

        winsound.Beep(freq, ms)
    except (ImportError, RuntimeError):
        pass
