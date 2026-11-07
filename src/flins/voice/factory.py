"""Build the voice pieces from settings, with a clear message if the voice extras are missing."""

from __future__ import annotations

from collections.abc import Callable

from flins.settings import Settings
from flins.voice.audio import MicRecorder
from flins.voice.loop import VoiceLoop, _Router
from flins.voice.stt import WhisperSTT
from flins.voice.tts import TTS, PrintTTS, SapiTTS

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


def make_tts(settings: Settings) -> TTS:
    v = settings.app.voice
    if v.tts == "print":
        return PrintTTS()
    return SapiTTS(rate=v.tts_rate, voice=v.tts_voice)


def make_stt(settings: Settings) -> WhisperSTT:
    v = settings.app.voice
    return WhisperSTT(v.stt_model, device=v.stt_device, language=v.language)


def make_loop(
    settings: Settings, router: _Router, announce: Callable[[str], None] = print
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
    )
