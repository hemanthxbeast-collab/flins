"""`flins.skill.yaml`: what a skill is allowed to do (SPEC §9.1). Anything not listed is denied."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from flins.core.levels import Level, Mode

MANIFEST_FILE = "flins.skill.yaml"

# permission -> what it gives. The only permissions that exist (CLAUDE.md invariant 8: no send).
PERMISSIONS: dict[str, str] = {
    "wa:read_thread": "read the last messages of the chat being answered",
    "wa:propose_reply": "suggest a reply (it still goes through approval and the guard)",
    "contacts:read_card": "see the sender's first name, tier and language",
    "calendar:read": "read busy times from your calendar file",
    "faq:read": "read your answers in config/faq.yaml",
    "messages:read_recent": "read recent incoming messages and what FLINS did with them",
    "http:get": "fetch web pages, only from the hosts listed under network",
}
# Known names that are refused on purpose, with the reason shown to the author.
REFUSED: dict[str, str] = {
    "wa:send": "skills can't send. Return a 'reply' proposal; FLINS decides whether it goes out",
    "llm:complete": "AI calls from skills are not available yet",
}
COMMANDS = ("summary",)  # things a skill can answer besides incoming messages

_NAME = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
_ENTRY = re.compile(r"^[a-z_][a-z0-9_]*:[a-z_][a-z0-9_]*$")
_HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9-]+(?:\.[a-z0-9-]+)+$")


class ManifestError(ValueError):
    pass


class Triggers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[str] = Field(default_factory=list)
    modes: list[Mode] = Field(default_factory=list)  # empty = any mode except PAUSED
    commands: list[Literal["summary"]] = Field(default_factory=list)


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    author: Literal["hermes", "claude", "user"]
    description: str = ""
    entrypoint: str  # "module:function"
    triggers: Triggers = Field(default_factory=Triggers)
    permissions: list[str] = Field(default_factory=list)
    max_level: Level = Level.NOTIFY
    timeout_s: float = Field(default=8.0, gt=0, le=15)
    network: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _NAME.match(v):
            raise ValueError("lowercase letters, digits and _ only, 2-40 characters")
        return v

    @field_validator("entrypoint")
    @classmethod
    def _entry(cls, v: str) -> str:
        if v == "prompt-only":
            raise ValueError("prompt-only skills are not supported yet; give module:function")
        if not _ENTRY.match(v):
            raise ValueError("expected 'module:function', e.g. 'handler:run'")
        return v

    @field_validator("permissions")
    @classmethod
    def _perms(cls, v: list[str]) -> list[str]:
        for p in v:
            if p in REFUSED:
                raise ValueError(f"permission {p!r} refused: {REFUSED[p]}")
            if p not in PERMISSIONS:
                raise ValueError(f"unknown permission {p!r}; known: {', '.join(PERMISSIONS)}")
        return sorted(set(v))

    @field_validator("max_level", mode="before")
    @classmethod
    def _level(cls, v: object) -> Level:
        level = Level.parse(v)  # type: ignore[arg-type]
        if level > Level.AUTO:
            raise ValueError("max_level can't be above AUTO")
        return level

    @field_validator("network")
    @classmethod
    def _network(cls, v: list[str]) -> list[str]:
        hosts = [h.strip().lower() for h in v]
        for h in hosts:
            if not _HOST.match(h):
                raise ValueError(f"network entries are plain host names, got {h!r}")
        return sorted(set(hosts))

    @property
    def module(self) -> str:
        return self.entrypoint.split(":")[0]

    @property
    def function(self) -> str:
        return self.entrypoint.split(":")[1]

    def allows(self, permission: str) -> bool:
        return permission in self.permissions

    def summary_lines(self) -> list[str]:
        """What you are approving, in plain words."""
        lines = [f"{p}: {PERMISSIONS[p]}" for p in self.permissions] or ["(no permissions)"]
        if self.network:
            lines.append(f"network: {', '.join(self.network)}")
        lines.append(f"highest level it can contribute: {self.max_level.name}")
        return lines


def load_manifest(skill_dir: Path) -> Manifest:
    path = skill_dir / MANIFEST_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManifestError(f"{MANIFEST_FILE} is missing") from None
    except yaml.YAMLError as exc:
        raise ManifestError(f"{MANIFEST_FILE} is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ManifestError(f"{MANIFEST_FILE} must be a mapping")
    try:
        m = Manifest.model_validate(data)
    except ValueError as exc:
        raise ManifestError(str(exc)) from None
    if m.name != skill_dir.name:
        raise ManifestError(f"name {m.name!r} must match the folder name {skill_dir.name!r}")
    if "http:get" in m.permissions and not m.network:
        raise ManifestError("http:get needs at least one host under network")
    if m.network and "http:get" not in m.permissions:
        raise ManifestError("network hosts are listed but http:get isn't a permission")
    return m
