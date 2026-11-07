"""`skills/skills.lock`: the exact versions you approved (SPEC §9.4).

A skill runs only if its files hash to what you approved. Change one byte and it stops running
until you approve it again. The lock file is yours (git-ignored), like policy.yaml.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from flins.core.levels import Level
from flins.skills.manifest import Manifest

LOCK_FILE = "skills.lock"
_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}


class LockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    sha256: str
    permissions: list[str] = Field(default_factory=list)
    network: list[str] = Field(default_factory=list)
    max_level: str
    approved_at: str
    enabled: bool = True


def skill_files(skill_dir: Path) -> list[Path]:
    return sorted(
        p
        for p in skill_dir.rglob("*")
        if p.is_file()
        and not (set(p.relative_to(skill_dir).parts[:-1]) & _SKIP_DIRS)
        and p.suffix not in (".pyc", ".pyo")
    )


def hash_skill(skill_dir: Path) -> str:
    """sha256 over every file's relative path and bytes."""
    h = hashlib.sha256()
    for p in skill_files(skill_dir):
        rel = p.relative_to(skill_dir).as_posix().encode("utf-8")
        data = p.read_bytes()
        h.update(len(rel).to_bytes(4, "big") + rel + len(data).to_bytes(8, "big") + data)
    return h.hexdigest()


class SkillLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, LockEntry] = {}
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
            self.entries = {
                k: LockEntry.model_validate(v) for k, v in raw.get("skills", {}).items()
            }

    def save(self) -> None:
        data = {
            "note": "Approved skills. Written by `flins skills enable`; don't edit by hand.",
            "skills": {k: v.model_dump() for k, v in sorted(self.entries.items())},
        }
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def approve(self, manifest: Manifest, sha256: str, now_iso: str) -> LockEntry:
        entry = LockEntry(
            version=manifest.version,
            sha256=sha256,
            permissions=list(manifest.permissions),
            network=list(manifest.network),
            max_level=manifest.max_level.name,
            approved_at=now_iso,
        )
        self.entries[manifest.name] = entry
        return entry

    def disable(self, name: str) -> bool:
        entry = self.entries.get(name)
        if entry is None or not entry.enabled:
            return False
        entry.enabled = False
        return True


def escalation(manifest: Manifest, previous: LockEntry | None) -> list[str]:
    """What this version may do that the approved one couldn't. Empty = no increase."""
    if previous is None:
        return [f"new skill: {p}" for p in manifest.permissions] + [
            f"new skill: network {h}" for h in manifest.network
        ]
    out = [f"new permission {p}" for p in manifest.permissions if p not in previous.permissions]
    out += [f"new network host {h}" for h in manifest.network if h not in previous.network]
    if manifest.max_level > Level.parse(previous.max_level):
        out.append(f"max_level raised {previous.max_level} -> {manifest.max_level.name}")
    return out
