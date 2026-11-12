"""Build the voice pieces from settings, with a clear message if the voice extras are missing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import aiosqlite

from flins.core.intents import contact_names
from flins.settings import Settings
from flins.voice.audio import MicRecorder
from flins.voice.loop import VoiceLoop, _Router
from flins.voice.stt import (
    CPU_FALLBACK_MODEL,
    GPU_ONLY_MODELS,
    PRESETS,
    WhisperSTT,
    gpu_available,
)
from flins.voice.tts import TTS, PiperTTS, PrintTTS, SapiTTS

INSTALL_HINT = "Voice isn't installed. Run: uv sync --extra voice"


class VoiceUnavailable(RuntimeError):
    pass


def check_installed() -> None:
    try:
        import faster_whisper  # noqa: F401
        import pynput  # noqa: F401
        import sounddevice  # noqa: F401
    except ImportError as exc:
        raise VoiceUnavailable(f"{INSTALL_HINT} ({exc.name} missing)") from exc


def piper_model(settings: Settings) -> Path:
    return settings.data_dir / "voices" / f"{settings.app.voice.piper_voice}.onnx"


def make_tts(settings: Settings) -> TTS:
    v = settings.app.voice
    if v.tts == "print":
        return PrintTTS()
    if v.tts == "piper":
        model = piper_model(settings)
        if not model.is_file():
            raise VoiceUnavailable(
                f"Piper voice {v.piper_voice!r} isn't downloaded. Run: uv run flins voice download"
            )
        return PiperTTS(model, speed=v.piper_speed)
    return SapiTTS(rate=v.tts_rate, voice=v.tts_voice)


def make_stt(settings: Settings) -> WhisperSTT:
    v = settings.app.voice
    model, beam = PRESETS[v.accuracy]
    device = v.stt_device
    if device == "auto":
        device = "cuda" if gpu_available() else "cpu"
    if device == "cpu" and v.stt_model is None and model in GPU_ONLY_MODELS:
        model = CPU_FALLBACK_MODEL  # a large model on the CPU takes many seconds per sentence
    return WhisperSTT(
        v.stt_model or model,
        device=device,
        compute_type="float16" if device == "cuda" else "int8",
        language=v.language,
        beam_size=beam,
    )


def make_loop(
    settings: Settings,
    router: _Router,
    announce: Callable[[str], None] = print,
    conn: aiosqlite.Connection | None = None,
) -> VoiceLoop:
    check_installed()
    v = settings.app.voice
    return VoiceLoop(
        router,
        MicRecorder(max_s=v.max_record_s, silence_s=v.silence_s),
        make_stt(settings),
        make_tts(settings),
        signature=settings.policy.user.signature,
        announce=announce,
        hint_source=_hint(conn, v.vocabulary),
        follow_up=v.follow_up,
        follow_up_s=v.follow_up_s,
        awaiting=_awaiting(router),
    )


def _awaiting(router: object) -> Callable[[], bool] | None:
    """True while FLINS waits for your spoken yes/no (the assistant core knows)."""
    check = getattr(router, "awaiting", None)
    return (lambda: bool(check("voice"))) if callable(check) else None


def _hint(
    conn: aiosqlite.Connection | None, vocabulary: list[str]
) -> Callable[[], Awaitable[str]] | None:
    """Spelling hints for the speech model: your contacts' names and your own word list."""
    words = ", ".join(w.strip() for w in vocabulary if w.strip())[:300]
    if conn is None and not words:
        return None

    async def hint() -> str:
        names = await contact_names(conn) if conn is not None else []
        parts = [("People: " + ", ".join(names) + ".") if names else ""]
        parts.append(f"Words: {words}." if words else "")
        return " ".join(p for p in parts if p)

    return hint
