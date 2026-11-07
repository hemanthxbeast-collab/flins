"""Speech-to-text with faster-whisper (local, offline after the first model download)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol

# Words FLINS should recognise well; Whisper uses this as a spelling hint. Contact names are
# added per call (see VoiceLoop), which is what makes "tell Rahul" come out as "Rahul".
HINT = (
    "Commands for FLINS, Hemanth's assistant: working mood, focus mode, I'm done, resume, stop, "
    "status, pending, approve number three, reject, extend, what did I miss, "
    "tell Rahul I'll call him at 8."
)
PRESETS = {
    "fast": ("base", 1),
    "balanced": ("small", 5),
    "best": ("medium", 5),
}


@dataclass
class Transcript:
    text: str
    language: str | None
    seconds: float


class STT(Protocol):
    async def transcribe(self, audio: Any, hint: str | None = None) -> Transcript: ...


class WhisperSTT:
    def __init__(
        self,
        model: str = "small",
        *,
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,
        beam_size: int = 1,
    ) -> None:
        self._beam_size = beam_size
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._model: Any = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def load(self) -> None:
        """Load (and on first use, download) the model. Takes a while the first time."""
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self._model_name, device=self._device, compute_type=self._compute_type
            )

    def _run(self, audio: Any, hint: str | None = None) -> Transcript:
        try:
            return self._transcribe(audio, hint)
        except RuntimeError as exc:
            # A GPU without the CUDA libraries (cublas/cudnn) fails only at inference time.
            if self._device == "cpu" or not any(
                k in str(exc).lower() for k in ("cuda", "cublas", "cudnn")
            ):
                raise
            self._device, self._model = "cpu", None
            return self._transcribe(audio, hint)

    def _transcribe(self, audio: Any, hint: str | None = None) -> Transcript:
        self.load()
        started = time.monotonic()
        segments, info = self._model.transcribe(
            audio,
            language=self._language,
            vad_filter=True,
            beam_size=self._beam_size,
            initial_prompt=f"{HINT} {hint}".strip() if hint else HINT,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return Transcript(text, getattr(info, "language", None), time.monotonic() - started)

    async def transcribe(self, audio: Any, hint: str | None = None) -> Transcript:
        return await asyncio.get_running_loop().run_in_executor(None, self._run, audio, hint)
