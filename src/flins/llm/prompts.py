"""Load versioned prompts from prompts/*.md (CLAUDE.md: never put a prompt inside code).

File format:
    ---
    version: triage_v1
    model: claude-haiku-5-5
    effort: low
    max_tokens: 1024
    ---
    <system prompt text>
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    model: str
    effort: str
    max_tokens: int
    system: str


class PromptError(ValueError):
    pass


def latest_name(prompts_dir: Path, job: str) -> str:
    """'draft' -> 'draft_v2' (the highest version on disk)."""
    versions = []
    for f in prompts_dir.glob(f"{job}_v*.md"):
        m = re.fullmatch(rf"{re.escape(job)}_v(\d+)", f.stem)
        if m:
            versions.append((int(m.group(1)), f.stem))
    if not versions:
        raise PromptError(f"no prompt files for {job!r} in {prompts_dir}")
    return max(versions)[1]


def load_prompt(prompts_dir: Path, name: str) -> Prompt:
    """Load `name` ('draft_v1'), or the newest version of a job ('draft')."""
    if not re.search(r"_v\d+$", name):
        name = latest_name(prompts_dir, name)
    path = prompts_dir / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise PromptError(f"{path.name}: missing front-matter header")
    try:
        _, header, body = text.split("---", 2)
    except ValueError:
        raise PromptError(f"{path.name}: unterminated front-matter header") from None
    meta = yaml.safe_load(header) or {}
    for key in ("version", "model"):
        if key not in meta:
            raise PromptError(f"{path.name}: header needs '{key}'")
    return Prompt(
        name=name,
        version=str(meta["version"]),
        model=str(meta["model"]),
        effort=str(meta.get("effort", "low")),
        max_tokens=int(meta.get("max_tokens", 1024)),
        system=body.strip(),
    )
