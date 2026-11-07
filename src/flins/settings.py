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
    roles: dict[Literal["triage", "draft", "redflag"], list[RouteConfig]] = Field(
        default_factory=dict
    )


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
    tts: Literal["sapi", "print"] = "sapi"  # sapi = Windows voices; print = text only
    tts_voice: str | None = None  # part of a Windows voice name, e.g. "Zira" or "Heera"
    tts_rate: int = Field(default=185, ge=80, le=400)
    speak_alerts: bool = True  # say urgent alerts, expiry warnings and the summary out loud
    max_record_s: float = Field(default=12.0, gt=1, le=60)
    silence_s: float = Field(default=0.9, gt=0.2, le=5)


class SkillsConfig(_Strict):
    enabled: bool = True  # master switch; each skill still needs `flins skills enable`
    dir: Path = Path("skills")
    calendar_ics: Path | None = None  # an .ics export of your calendar, for availability_reply
    faq_file: Path = Path("config/faq.yaml")


class AppConfig(_Strict):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)


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
