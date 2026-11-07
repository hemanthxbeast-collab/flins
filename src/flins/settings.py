"""App settings: config/flins.yaml (non-secret) + .env (secrets)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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


class AppConfig(_Strict):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


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
    notes: list[str] = field(default_factory=list)

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
    env_file = root / ".env"
    secrets = Secrets(_env_file=env_file if env_file.is_file() else None)  # type: ignore[call-arg]
    return Settings(root=root, app=app, policy=policy, secrets=secrets, tones=tones, notes=notes)
