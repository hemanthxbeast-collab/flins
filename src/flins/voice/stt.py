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
    # Whisper's large model. Needs the GPU to be usable (well under a second per sentence on
    # an RTX 4060; far too slow on a CPU). The clear choice for accented English.
    "max": ("large-v3-turbo", 5),
}
GPU_ONLY_MODELS = ("large-v3-turbo", "large-v3", "large-v2")
CPU_FALLBACK_MODEL = "small"  # if the GPU can't be used, don't run a large model on the CPU


def register_cuda_dlls() -> list[str]:
    """Windows: make the NVIDIA runtime libraries from the `nvidia-*-cu12` pip packages
    findable (they live in site-packages/nvidia/*/bin, which isn't on the DLL search path).
    Returns the folders added. Harmless when the packages aren't installed."""
    import os
    import site
    from pathlib import Path

    added: list[str] = []
    if os.name != "nt":
        return added
    roots = [*site.getsitepackages(), site.getusersitepackages()]
    for sp in roots:
        for d in Path(sp).glob("nvidia/*/bin"):
            if d.is_dir() and str(d) not in added:
                os.add_dll_directory(str(d))
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
                added.append(str(d))
    return added


def gpu_available() -> bool:
    """True if the speech library can see a CUDA GPU."""
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count()) > 0
    except Exception:
        return False


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
            if self._device != "cpu":
                register_cuda_dlls()
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self._model_name, device=self._device, compute_type=self._compute_type
            )

    @property
    def device(self) -> str:
        return self._device

    def warm_up(self) -> None:
        """One throwaway run on a second of silence, so your first sentence isn't the slow
        one (a GPU model takes about a second extra the first time). Never raises."""
        try:
            import numpy as np

            self.load()
            segments, _ = self._model.transcribe(
                np.zeros(16_000, dtype="float32"), language="en", vad_filter=False, beam_size=1
            )
            list(segments)
        except Exception:  # the real call reports (or works around) the problem
            return

    def _run(self, audio: Any, hint: str | None = None) -> Transcript:
        try:
            return self._transcribe(audio, hint)
        except RuntimeError as exc:
            # A GPU without the CUDA libraries (cublas/cudnn) fails only at inference time.
            if self._device == "cpu" or not any(
                k in str(exc).lower() for k in ("cuda", "cublas", "cudnn")
            ):
                raise
            # Fall back to the CPU, with a model the CPU can run at a usable speed.
            self._device, self._model, self._compute_type = "cpu", None, "int8"
            if self._model_name in ("large-v3-turbo", "large-v3", "large-v2", "medium"):
                self._model_name = CPU_FALLBACK_MODEL
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
