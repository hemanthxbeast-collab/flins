"""App settings: config/flins.yaml (non-secret) + .env (secrets)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from flins.core.policy_schema import Policy, load_policy


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathsConfig(_Strict):
    data_dir: Path = Path("data")
    db_file: str = "flins.db"


class NetworkConfig(_Strict):
    dashboard_port: int = Field(default=8765, ge=1024, le=65535)
    sidecar_port: int = Field(default=8766, ge=1024, le=65535)
    skilldev_port: int = Field(default=8767, ge=1024, le=65535)


class LoggingConfig(_Strict):
    level: str = "INFO"


class ProviderConfig(_Strict):
    """Override a built-in provider, or add one that speaks the OpenAI chat API."""

    base_url: str | None = None
    key_env: str | None = None  # name of the environment variable that holds the key
    json_mode: Literal["json_schema", "json_object", "prompt"] | None = None
    reasoning_effort: bool | None = None
    min_max_tokens: int | None = Field(default=None, ge=256, le=65536)
    timeout_s: float | None = Field(default=None, gt=0, le=300)


class RouteConfig(_Strict):
    provider: str
    model: str
    price_in: float | None = Field(default=None, ge=0)  # USD per million input tokens
    price_out: float | None = Field(default=None, ge=0)  # USD per million output tokens


class LLMConfig(_Strict):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    # Per job, providers in order of preference. Missing jobs use the Anthropic defaults.
    # triage / draft / redflag are needed for WhatsApp. intent, chat, reason and summary are
    # optional: without their own list they borrow another job's (see llm/providers.py).
    roles: dict[
        Literal["triage", "draft", "redflag", "intent", "chat", "reason", "summary"],
        list[RouteConfig],
    ] = Field(default_factory=dict)


class VoiceConfig(_Strict):
    enabled: bool = False  # or start with `flins run --voice`
    hotkey: str = "<ctrl>+<alt>+<space>"
    # fast = base model (~0.7 s); balanced = small + careful search (~2-3 s, much better with
    # Indian English); best = medium (~6 s+, 1.5 GB download). stt_model overrides the model.
    accuracy: Literal["fast", "balanced", "best"] = "balanced"
    stt_model: str | None = None  # faster-whisper: tiny, base, small, medium, large-v3
    stt_device: Literal["cpu", "cuda", "auto"] = "cpu"  # cuda needs NVIDIA's CUDA 12 libraries
    # "en" (default) stops short clips being mistaken for German or Dutch. Use "hi"/"te" if you
    # speak commands in Hindi/Telugu script, or null to detect each time.
    language: str | None = "en"
    tts: Literal["sapi", "piper", "print"] = "sapi"  # sapi = Windows voices; piper = more
    # natural (run `flins voice download` once); print = text only
    piper_voice: str = "en_US-lessac-medium"  # see `flins voice download --list`
    piper_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    tts_voice: str | None = None  # part of a Windows voice name, e.g. "Zira" or "Heera"
    tts_rate: int = Field(default=185, ge=80, le=400)
    speak_alerts: bool = True  # say urgent alerts, expiry warnings and the summary out loud
    max_record_s: float = Field(default=12.0, gt=1, le=60)
    silence_s: float = Field(default=0.9, gt=0.2, le=5)
    # M9: say "FLINS" instead of pressing the hotkey. Listens to the mic all the time (locally,
    # nothing saved) and runs a small speech model on each burst of speech.
    wake_word: bool = False
    wake_model: str = "tiny"


class SkillsConfig(_Strict):
    enabled: bool = True  # master switch; each skill still needs `flins skills enable`
    dir: Path = Path("skills")
    calendar_ics: Path | None = None  # an .ics export of your calendar, for availability_reply
    faq_file: Path = Path("config/faq.yaml")


class BackupConfig(_Strict):
    dir: Path | None = None  # default data/backups; better: a folder on another disk or OneDrive
    keep: int = Field(default=14, ge=1, le=365)
    daily: bool = True  # `flins run` makes one a day when FLINS_BACKUP_PASSPHRASE is set


class ReasoningConfig(_Strict):
    """When a message goes to the `reason` model instead of the faster `chat` model."""

    min_chars: int = Field(default=280, ge=40)
    cues: list[str] = Field(
        default_factory=lambda: [
            "plan",
            "step by step",
            "compare",
            "pros and cons",
            "trade-off",
            "tradeoff",
            "debug",
            "design",
            "strategy",
            "explain why",
            "in detail",
        ]
    )


class MemoryConfig(_Strict):
    """Long-term memory. Nothing is saved unless you say "remember that ..." or answer yes
    when FLINS asks whether it should remember something."""

    enabled: bool = True
    suggest: bool = True  # FLINS may ask "Should I remember that ...?"
    suggest_min_importance: float = Field(default=0.8, ge=0, le=1)
    suggest_cooldown_minutes: int = Field(default=10, ge=0, le=24 * 60)
    max_in_context: int = Field(default=16, ge=0, le=100)  # memories the model sees per turn
    pinned: int = Field(default=6, ge=0, le=50)  # of those, standing ones (preferences etc.)


class AssistantConfig(_Strict):
    """The conversational core (docs/JARVIS_ARCHITECTURE.md). Personality lives here, not in
    code: FLINS's name, how it talks, and when it may speak up in your self-chat."""

    enabled: bool = True
    name: str = Field(default="FLINS", min_length=1, max_length=30)
    style: Literal["concise", "balanced", "detailed"] = "concise"
    humor: Literal["none", "light"] = "light"
    notes: list[str] = Field(default_factory=list, max_length=20)  # extra personality notes
    # Your "Message yourself" chat is also where you keep notes. addressed = FLINS only chats
    # when you start with its name, or while a conversation with it is open; all = it answers
    # everything you type there; off = commands only.
    self_chat: Literal["addressed", "all", "off"] = "addressed"
    follow_window_s: int = Field(default=180, ge=0, le=3600)
    history_turns: int = Field(default=12, ge=2, le=60)  # recent turns the model sees
    session_idle_minutes: int = Field(default=30, ge=1, le=24 * 60)
    keep_days: int = Field(default=30, ge=1, le=3650)  # conversation turns are deleted after
    use_about_me: bool = True  # give the model your about_me.yaml facts
    max_facts: int = Field(default=40, ge=0, le=200)
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)


class AppConfig(_Strict):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    assistant: AssistantConfig = Field(default_factory=AssistantConfig)


class ToneProfile(_Strict):
    formality: str = "low"
    emoji: str = "sparse"
    max_chars: int = Field(default=160, ge=20, le=1000)
    language: str = "en"


class ToneConfig(BaseModel):
    """tone_profiles.yaml: named profiles plus optional `style_samples`."""

    profiles: dict[str, ToneProfile] = Field(default_factory=dict)
    style_samples: list[str] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, data: dict[str, object] | None) -> ToneConfig:
        data = dict(data or {})
        samples = data.pop("style_samples", None)
        return cls(
            profiles={k: ToneProfile.model_validate(v) for k, v in data.items()},
            style_samples=[str(s) for s in samples] if isinstance(samples, list) else [],
        )

    def get(self, name: str | None) -> ToneProfile:
        if name and name in self.profiles:
            return self.profiles[name]
        return self.profiles.get("casual", ToneProfile())


ShareWith = Literal["everyone", "known", "trusted", "inner"]
_SHARE_RANK = {"everyone": 0, "known": 1, "trusted": 2, "inner": 3}
_TIER_RANK = {"unknown": 0, "known": 1, "trusted": 2, "inner": 3}  # blocked: never


class AboutFact(_Strict):
    fact: str = Field(min_length=1, max_length=300)
    share_with: ShareWith = "trusted"


class AboutAnswer(_Strict):
    question: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=300)
    share_with: ShareWith = "trusted"


class AboutMe(_Strict):
    """about_me.yaml: the only things FLINS may say about the user, and to whom."""

    facts: list[AboutFact] = Field(default_factory=list)
    answers: list[AboutAnswer] = Field(default_factory=list)

    def for_tier(self, tier: str) -> list[str]:
        rank = _TIER_RANK.get(tier, -1)
        if rank < 0:
            return []
        out = [f.fact for f in self.facts if _SHARE_RANK[f.share_with] <= rank]
        out += [
            f"If asked {a.question!r}: {a.answer}"
            for a in self.answers
            if _SHARE_RANK[a.share_with] <= rank
        ]
        return out


class Secrets(BaseSettings):
    """Read from environment variables and the project's .env. Values are never logged."""

    model_config = SettingsConfigDict(extra="ignore")

    anthropic_api_key: SecretStr | None = None
    flins_wa_secret: SecretStr | None = None


@dataclass
class Settings:
    root: Path
    app: AppConfig
    policy: Policy
    secrets: Secrets
    tones: ToneConfig = field(default_factory=ToneConfig)
    about: AboutMe = field(default_factory=AboutMe)
    notes: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)  # .env values, then real env vars

    def env_value(self, name: str | None) -> str | None:
        """A secret by variable name (e.g. GROQ_API_KEY). Never log the result."""
        if not name:
            return None
        value = os.environ.get(name) or self.env.get(name)
        return value or None

    @property
    def prompts_dir(self) -> Path:
        return self.root / "prompts"

    @property
    def data_dir(self) -> Path:
        d = self.app.paths.data_dir
        return d if d.is_absolute() else self.root / d

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.app.paths.db_file

    def path(self, p: Path) -> Path:
        return p if p.is_absolute() else self.root / p

    @property
    def skills_dir(self) -> Path:
        return self.path(self.app.skills.dir)

    @property
    def backup_dir(self) -> Path:
        d = self.app.backup.dir
        return self.path(d) if d is not None else self.data_dir / "backups"

    @property
    def skill_runs_dir(self) -> Path:
        return self.data_dir / "skill_runs"


def find_root(start: Path | None = None) -> Path:
    """Walk up from `start` to the folder holding pyproject.toml and config/."""
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        if (d / "pyproject.toml").is_file() and (d / "config").is_dir():
            return d
    raise FileNotFoundError("could not find the FLINS project root (pyproject.toml + config/)")


def _pick(config_dir: Path, name: str, notes: list[str]) -> Path:
    real = config_dir / f"{name}.yaml"
    if real.is_file():
        return real
    example = config_dir / f"{name}.example.yaml"
    if example.is_file():
        notes.append(f"config/{name}.yaml not found; using {name}.example.yaml")
        return example
    raise FileNotFoundError(f"neither {real} nor {example} exists")


def load_settings(root: Path | None = None) -> Settings:
    root = root or find_root()
    notes: list[str] = []
    config_dir = root / "config"

    with _pick(config_dir, "flins", notes).open(encoding="utf-8") as f:
        app = AppConfig.model_validate(yaml.safe_load(f) or {})
    policy = load_policy(_pick(config_dir, "policy", notes))
    tones = ToneConfig()
    try:
        with _pick(config_dir, "tone_profiles", notes).open(encoding="utf-8") as f:
            tones = ToneConfig.from_yaml(yaml.safe_load(f))
    except FileNotFoundError:
        notes.append("no tone_profiles.yaml; using built-in defaults")
    for tier_name, tier in policy.tiers.items():
        if tier.tone and tier.tone not in tones.profiles:
            notes.append(f"tier {tier_name} uses unknown tone profile {tier.tone!r}")
    about = AboutMe()
    try:
        with _pick(config_dir, "about_me", notes).open(encoding="utf-8") as f:
            about = AboutMe.model_validate(yaml.safe_load(f) or {})
    except FileNotFoundError:
        pass
    env_file = root / ".env"
    secrets = Secrets(_env_file=env_file if env_file.is_file() else None)  # type: ignore[call-arg]
    env = read_env_file(env_file) if env_file.is_file() else {}
    return Settings(
        root=root,
        app=app,
        policy=policy,
        secrets=secrets,
        tones=tones,
        about=about,
        notes=notes,
        env=env,
    )


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal .env reader: KEY=VALUE lines, # comments (also after a value), optional quotes."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#|^#", value, maxsplit=1)[0].strip()
        if value:
            out[key.strip()] = value
    return out
