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
    notes: list[str] = field(default_factory=list)

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
    env_file = root / ".env"
    secrets = Secrets(_env_file=env_file if env_file.is_file() else None)  # type: ignore[call-arg]
    return Settings(root=root, app=app, policy=policy, secrets=secrets, notes=notes)
