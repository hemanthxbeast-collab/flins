"""Text-to-speech. Default: Windows' built-in voices via pyttsx3 (offline, nothing to
download). Optional: Piper, a more natural neural voice (offline after one download)."""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

MAX_SPOKEN = 450
_EMOJI = re.compile("[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff️‍]+")


def speakable(text: str, *, signature: str | None = None) -> str:
    """Turn a chat-style note into something pleasant to hear."""
    t = text
    if signature:
        t = t.replace(signature, "")
    t = _EMOJI.sub("", t)
    t = t.replace("•", ". ").replace("→", ": ").replace("⏎", ". ")
    t = re.sub(r"#(\d+)", r"number \1", t)
    t = re.sub(r"\s*\n\s*", ". ", t)
    t = re.sub(r"\.(\s*\.)+", ".", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" .")
    if len(t) > MAX_SPOKEN:
        cut = t[:MAX_SPOKEN].rsplit(". ", 1)[0]
        t = cut + ". The rest is on the dashboard"
    return t + "." if t and t[-1] not in ".?!" else t


class TTS(Protocol):
    async def say(self, text: str) -> None: ...


class SapiTTS:
    """pyttsx3 isn't thread-safe, so one dedicated worker thread owns the engine."""

    def __init__(self, *, rate: int = 185, voice: str | None = None) -> None:
        self._rate = rate
        self._voice = voice
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="flins-tts")
        self._engine: Any = None

    def _speak(self, text: str) -> None:
        if self._engine is None:
            import pyttsx3

            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", self._rate)
            if self._voice:
                for v in self._engine.getProperty("voices"):
                    if self._voice.lower() in (v.name or "").lower():
                        self._engine.setProperty("voice", v.id)
                        break
        self._engine.say(text)
        self._engine.runAndWait()

    async def say(self, text: str) -> None:
        if text.strip():
            await asyncio.get_running_loop().run_in_executor(self._pool, self._speak, text)

    def voices(self) -> list[str]:
        import pyttsx3

        engine = pyttsx3.init()
        return [v.name for v in engine.getProperty("voices")]


class PiperTTS:
    """Piper: a natural-sounding neural voice, fully offline. Needs a downloaded voice model
    (`flins voice download`). Speaks through the default speakers with sounddevice."""

    def __init__(self, model: Path, *, speed: float = 1.0) -> None:
        self._model = model
        self._speed = speed
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="flins-piper")
        self._voice: Any = None

    def load(self) -> None:
        if self._voice is None:
            from piper import PiperVoice

            self._voice = PiperVoice.load(self._model)

    def _audio(self, text: str) -> tuple[Any, int]:
        import numpy as np
        from piper import SynthesisConfig

        self.load()
        config = SynthesisConfig(length_scale=1.0 / self._speed)
        chunks = list(self._voice.synthesize(text, syn_config=config))
        if not chunks:
            return np.zeros(0, dtype="float32"), 22050
        audio = np.concatenate([c.audio_float_array for c in chunks]).astype("float32")
        return audio, int(chunks[0].sample_rate)

    def _speak(self, text: str) -> None:
        import sounddevice as sd

        audio, rate = self._audio(text)
        if len(audio):
            sd.play(audio, rate)
            sd.wait()

    async def say(self, text: str) -> None:
        if text.strip():
            await asyncio.get_running_loop().run_in_executor(self._pool, self._speak, text)


class PrintTTS:
    """No speakers: print what would be said (also used in tests)."""

    def __init__(self) -> None:
        self.said: list[str] = []

    async def say(self, text: str) -> None:
        self.said.append(text)
        print(f"   (FLINS says) {text}")
