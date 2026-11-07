"""Speech-to-text with faster-whisper (local, offline after the first model download)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol

# Words FLINS should recognise well; Whisper uses this as a spelling hint.
HINT = (
    "FLINS, working mood, focus mode, do not disturb, heading out, I'm done, resume, stop, "
    "status, pending, approve, reject, extend, what did I miss"
)


@dataclass
class Transcript:
    text: str
    language: str | None
    seconds: float


class STT(Protocol):
    async def transcribe(self, audio: Any) -> Transcript: ...


class WhisperSTT:
    def __init__(
        self,
        model: str = "small",
        *,
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = None,
    ) -> None:
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._model: Any = None

    def load(self) -> None:
        """Load (and on first use, download) the model. Takes a while the first time."""
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self._model_name, device=self._device, compute_type=self._compute_type
            )

    def _run(self, audio: Any) -> Transcript:
        try:
            return self._transcribe(audio)
        except RuntimeError as exc:
            # A GPU without the CUDA libraries (cublas/cudnn) fails only at inference time.
            if self._device == "cpu" or not any(
                k in str(exc).lower() for k in ("cuda", "cublas", "cudnn")
            ):
                raise
            self._device, self._model = "cpu", None
            return self._transcribe(audio)

    def _transcribe(self, audio: Any) -> Transcript:
        self.load()
        started = time.monotonic()
        segments, info = self._model.transcribe(
            audio,
            language=self._language,
            vad_filter=True,
            beam_size=1,
            initial_prompt=HINT,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        return Transcript(text, getattr(info, "language", None), time.monotonic() - started)

    async def transcribe(self, audio: Any) -> Transcript:
        return await asyncio.get_running_loop().run_in_executor(None, self._run, audio)
