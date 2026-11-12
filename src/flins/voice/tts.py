"""Text-to-speech. Default: Windows' built-in voices via pyttsx3 (offline, nothing to
download). Optional: Piper, a more natural neural voice (offline after one download)."""

from __future__ import annotations

import asyncio
import importlib
import math
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

    def stop(self) -> None:
        """Stop what is being said now and drop anything queued before this call."""


def sapi_rate(wpm: int) -> int:
    """Words per minute -> SAPI's -10..10 scale (the same mapping pyttsx3 uses)."""
    return max(-10, min(10, int(math.log(wpm / 156.63, 1.11))))


class SapiTTS:
    """Windows' built-in voices. Talks to SAPI directly so speech can be cut off mid-sentence
    (barge-in). One dedicated worker thread owns the voice object. If SAPI can't be opened
    that way, falls back to pyttsx3, which can't be interrupted."""

    _ASYNC, _PURGE = 1, 2  # SpeechVoiceSpeakFlags

    def __init__(self, *, rate: int = 185, voice: str | None = None) -> None:
        self._rate = rate
        self._voice = voice
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="flins-tts")
        self._sapi: Any = None
        self._engine: Any = None  # pyttsx3 fallback
        self._gen = 0  # bumped by stop(): anything queued under an older number is dropped

    def _open(self) -> Any:
        if self._sapi is None and self._engine is None:
            try:
                comtypes = importlib.import_module("comtypes")
                client = importlib.import_module("comtypes.client")
                comtypes.CoInitialize()
                sapi = client.CreateObject("SAPI.SpVoice")
                sapi.Rate = sapi_rate(self._rate)
                if self._voice:
                    for token in sapi.GetVoices():
                        if self._voice.lower() in str(token.GetDescription()).lower():
                            sapi.Voice = token
                            break
                self._sapi = sapi
            except Exception:
                self._engine = self._open_pyttsx3()
        return self._sapi

    def _open_pyttsx3(self) -> Any:
        import pyttsx3

        engine = pyttsx3.init()
        engine.setProperty("rate", self._rate)
        if self._voice:
            for v in engine.getProperty("voices"):
                if self._voice.lower() in (v.name or "").lower():
                    engine.setProperty("voice", v.id)
                    break
        return engine

    def _speak(self, text: str, gen: int) -> None:
        if gen != self._gen:
            return  # stopped before its turn came
        sapi = self._open()
        if sapi is None:
            self._engine.say(text)
            self._engine.runAndWait()
            return
        sapi.Speak(text, self._ASYNC)
        while not sapi.WaitUntilDone(30):
            if gen != self._gen:
                sapi.Speak("", self._ASYNC | self._PURGE)
                break

    async def say(self, text: str) -> None:
        if text.strip():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._pool, self._speak, text, self._gen)

    def stop(self) -> None:
        self._gen += 1

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
        self._gen = 0

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

    def _speak(self, text: str, gen: int) -> None:
        import sounddevice as sd

        if gen != self._gen:
            return
        audio, rate = self._audio(text)
        if len(audio) and gen == self._gen:
            sd.play(audio, rate)
            sd.wait()

    async def say(self, text: str) -> None:
        if text.strip():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._pool, self._speak, text, self._gen)

    def stop(self) -> None:
        self._gen += 1
        try:
            import sounddevice as sd

            sd.stop()  # sd.wait() in the worker thread returns at once
        except Exception:  # pragma: no cover - depends on the sound system
            pass


class PrintTTS:
    """No speakers: print what would be said (also used in tests)."""

    def __init__(self) -> None:
        self.said: list[str] = []
        self.stopped = 0

    async def say(self, text: str) -> None:
        self.said.append(text)
        print(f"   (FLINS says) {text}")

    def stop(self) -> None:
        self.stopped += 1
