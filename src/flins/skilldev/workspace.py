"""Everything the skill-dev MCP server can do, as plain methods (SPEC §10).

Hermes can read the installed skills (as examples), and create, edit, check and submit skills
in `_incoming/<name>/`. It can't enable anything, can't touch policy or your data, and never
sees your config: the safety eval runs on the *example* policy.

Path rules for writes: a skill name like `weather_reply`, a relative path of plain parts
(`handler.py`, `fixtures/rain.json`), allowed file types only, size limits, no links, and the
resolved path must stay inside `_incoming/<name>/`.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from flins.core.clock import to_iso
from flins.core.policy_schema import Policy, load_policy
from flins.skills.checks import CheckResult, check_skill
from flins.skills.ctx_api import REQUIRES
from flins.skills.lock import LOCK_FILE, SkillLock, hash_skill, skill_files
from flins.skills.manifest import COMMANDS, MANIFEST_FILE, PERMISSIONS, REFUSED, Manifest
from flins.skills.validator import ALLOWED_IMPORTS, BANNED_CALLS

INCOMING = "_incoming"
SUBMISSIONS = "_submissions"
REQUESTS = "_requests"
NAME = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
SUFFIXES = frozenset({".py", ".md", ".yaml", ".yml", ".json", ".txt"})
MAX_FILE = 100_000
MAX_SKILL = 1_000_000
MAX_FILES = 60
MAX_DEPTH = 3
REQUEST_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-f0-9]{6}$")


class WorkspaceError(ValueError):
    """Refused. The message is shown to Hermes."""


@dataclass
class Workspace:
    skills_dir: Path  # installed skills (read-only here)
    incoming_dir: Path  # the only place Hermes can write
    root: Path  # repo root: prompts, eval cases, example policy, SPEC

    @classmethod
    def for_root(cls, root: Path, incoming: Path | None = None) -> Workspace:
        skills = root / "skills"
        return cls(skills, incoming or skills / INCOMING, root)

    # ------------------------------------------------------------------ paths

    def _name(self, name: str) -> str:
        if not isinstance(name, str) or not NAME.match(name):
            raise WorkspaceError(
                "skill names are lowercase letters, digits and _ (2-40 chars, starting with a "
                "letter)"
            )
        return name

    def _incoming_root(self) -> Path:
        root = self.incoming_dir
        if _is_link(root):
            raise WorkspaceError("the incoming folder is a link; refusing")
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()

    def skill_path(self, name: str) -> Path:
        root = self._incoming_root()
        d = root / self._name(name)
        if _is_link(d):
            raise WorkspaceError(f"{name} is a link; refusing")
        return d

    def file_path(self, name: str, path: str) -> Path:
        """The one place a write location is decided. Everything else goes through here."""
        if not isinstance(path, str) or not path or "\\" in path or ":" in path or "\0" in path:
            raise WorkspaceError("use a relative path with forward slashes, e.g. fixtures/a.json")
        pure = PurePosixPath(path)
        parts = pure.parts
        if pure.is_absolute() or not parts or len(parts) > MAX_DEPTH:
            raise WorkspaceError(f"path must be relative, at most {MAX_DEPTH} levels deep")
        for part in parts:
            if not PART.match(part) or part in (".", ".."):
                raise WorkspaceError(f"bad path part {part!r}: plain names only, no '..' or dots")
        if pure.suffix.lower() not in SUFFIXES:
            raise WorkspaceError(f"file type not allowed; use one of {', '.join(sorted(SUFFIXES))}")
        d = self.skill_path(name)
        target = d.joinpath(*parts)
        # Every existing step must be a real folder/file, not a link to somewhere else.
        step = d
        for part in parts:
            step = step / part
            if _is_link(step):
                raise WorkspaceError(f"{path} goes through a link; refusing")
        resolved = target.resolve()
        if not resolved.is_relative_to(d.resolve()):
            raise WorkspaceError("path escapes the skill folder")
        return target

    # ------------------------------------------------------------------ reading

    def spec(self) -> str:
        """SPEC §9 + the rules the validator enforces + the manifest schema + an example."""
        spec_md = (self.root / "docs" / "SPEC.md").read_text(encoding="utf-8")
        m = re.search(r"## 9\. Skill system.*?(?=\n## 10\.)", spec_md, re.S)
        section = m.group(0) if m else "(SPEC §9 not found)"
        example = self.read_skill("holding_reply", where="installed")
        rules = f"""
## Rules FLINS enforces (read before writing code)
- Files: {", ".join(sorted(SUFFIXES))}; each under {MAX_FILE // 1000} KB; no links.
- Required: flins.skill.yaml, SKILL.md (frontmatter name + description), the entry module,
  and at least one test in fixtures/*.json. tests/*.py are never run by FLINS.
- Imports allowed: {", ".join(sorted(ALLOWED_IMPORTS))}, and your own .py files.
  Not allowed: {", ".join(sorted(BANNED_CALLS))}, any __dunder__ attribute, everything else.
- No network except ctx.http.get(url=...) to https hosts listed under `network` (needs http:get).
- The skill runs in a sandbox: no files outside its run folder, no processes, no sockets,
  no API keys. Times are local text "YYYY-MM-DD HH:MM" (no time zones inside skills).
- Entry: def run(inp: dict, ctx) -> dict with "proposals", "facts", "logs".
- A reply proposal can only stand in for a fixed holding template (AUTO_SAFE). For drafts,
  FLINS's own AI writes the reply; return verified facts (e.g. {{"free_at": "18:30"}}) for it.
- There is no send permission. Only the user can enable a skill, in the FLINS dashboard.

## Fixture format (fixtures/<case>.json)
{{"input": {{...SkillInput fields...}}, "contact": {{"first_name": "Rahul", "tier": "trusted"}},
 "ctx": {{"thread": [...], "calendar": [{{"start": "...", "end": "..."}}], "faq": [...],
         "recent": [...], "http": {{"https://host/x": "body"}}}},
 "expect": {{"reply_contains": [...], "reply_excludes": [...], "no_reply": true,
            "note_contains": [...], "facts": {{...}}, "denied": [...], "error": false}}}}
Fixture data must be made up. Never put real names, numbers or messages in a skill.

## flins.skill.yaml schema
{json.dumps(Manifest.model_json_schema(), indent=1)}
"""
        return (
            f"{section}\n{rules}\n## Example skill: holding_reply\n{json.dumps(example, indent=1)}"
        )

    def capabilities(self) -> dict[str, Any]:
        calls = {
            "log": "ctx.log(message) - always allowed",
            "thread": "ctx.thread(n=10) -> [{from: me|them, text, time}]",
            "faq": "ctx.faq() -> [{q: [...], a, tiers: [...]}]",
            "calendar.free_busy": (
                "ctx.calendar.free_busy(start='YYYY-MM-DD HH:MM', end=...) -> [{start, end}]"
            ),
            "messages.recent": (
                "ctx.messages.recent(hours=8) -> [{who, time, text, action, urgent, flags}]"
            ),
            "http.get": "ctx.http.get(url='https://...') -> {status, text}",
        }
        return {
            "permissions": PERMISSIONS,
            "refused": REFUSED,
            "ctx_calls": {k: {"call": calls[k], "needs": v} for k, v in REQUIRES.items()},
            "commands": list(COMMANDS),
            "levels": ["IGNORE", "NOTIFY", "DRAFT", "AUTO_SAFE", "AUTO"],
        }

    def list_skills(self) -> dict[str, Any]:
        installed = []
        if self.skills_dir.is_dir():
            for d in sorted(self.skills_dir.iterdir()):
                if d.is_dir() and NAME.match(d.name) and (d / MANIFEST_FILE).is_file():
                    installed.append(d.name)
        incoming = []
        root = self.incoming_dir
        if root.is_dir():
            for d in sorted(root.iterdir()):
                if d.is_dir() and NAME.match(d.name):
                    sub = self._submission(d.name)
                    incoming.append(
                        {
                            "name": d.name,
                            "submitted": sub is not None,
                            "review": (sub or {}).get("review"),
                        }
                    )
        return {"installed": installed, "incoming": incoming}

    def read_skill(self, name: str, where: str = "incoming") -> dict[str, str]:
        self._name(name)
        if where == "installed":
            d = self.skills_dir / name
        elif where == "incoming":
            d = self.incoming_dir / name
        else:
            raise WorkspaceError("where must be 'installed' or 'incoming'")
        if not d.is_dir() or _is_link(d):
            raise WorkspaceError(f"no {where} skill called {name!r}")
        out: dict[str, str] = {}
        for p in skill_files(d):
            if _is_link(p) or p.suffix.lower() not in SUFFIXES or p.stat().st_size > MAX_FILE:
                continue
            out[p.relative_to(d).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
        return out

    # ------------------------------------------------------------------ writing

    def scaffold(self, name: str, description: str) -> list[str]:
        d = self.skill_path(name)
        if d.exists():
            raise WorkspaceError(f"{name} already exists in _incoming; edit it instead")
        desc = " ".join(str(description).split())[:200] or "describe what it does"
        files = {
            MANIFEST_FILE: (
                f"name: {name}\nversion: 0.1.0\nauthor: hermes\ndescription: {json.dumps(desc)}\n"
                "entrypoint: handler:run\ntriggers:\n  categories: []\n  modes: []\n"
                "permissions:\n  - contacts:read_card\nmax_level: NOTIFY\ntimeout_s: 5\n"
                "network: []\n"
            ),
            "SKILL.md": f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\n{desc}\n",
            "handler.py": (
                '"""' + desc + '"""\n\n\ndef run(inp, ctx):\n'
                '    return {"proposals": [], "facts": {}}\n'
            ),
            "fixtures/basic.json": json.dumps(
                {"input": {"messages": ["hello"]}, "expect": {"no_reply": True}}, indent=2
            )
            + "\n",
        }
        for path, content in files.items():
            self.write_file(name, path, content, create=True)
        return sorted(files)

    def write_file(self, name: str, path: str, content: str, *, create: bool = False) -> str:
        target = self.file_path(name, path)
        d = self.skill_path(name)
        if not d.exists() and not create:
            raise WorkspaceError(f"no skill {name!r} in _incoming; scaffold_skill first")
        data = str(content).encode("utf-8")
        if len(data) > MAX_FILE:
            raise WorkspaceError(f"file too big ({len(data)} bytes, limit {MAX_FILE})")
        existing = [p for p in skill_files(d) if p != target] if d.exists() else []
        if len(existing) + 1 > MAX_FILES:
            raise WorkspaceError(f"too many files (limit {MAX_FILES})")
        if sum(p.stat().st_size for p in existing) + len(data) > MAX_SKILL:
            raise WorkspaceError(f"skill too big (limit {MAX_SKILL} bytes)")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self._unsubmit(name)  # any edit withdraws a submission
        return f"wrote {name}/{path} ({len(data)} bytes)"

    def delete_file(self, name: str, path: str) -> str:
        target = self.file_path(name, path)
        if not target.is_file():
            raise WorkspaceError(f"{name}/{path} doesn't exist")
        target.unlink()
        self._unsubmit(name)
        return f"deleted {name}/{path}"

    # ------------------------------------------------------------------ checking

    def _policy(self) -> Policy:
        return load_policy(self.root / "config" / "policy.example.yaml")

    async def check(self, name: str, *, with_eval: bool) -> CheckResult:
        d = self.skill_path(name)
        if not d.is_dir():
            raise WorkspaceError(f"no skill {name!r} in _incoming")
        with tempfile.TemporaryDirectory() as tmp:
            return await check_skill(
                d,
                lock=SkillLock(self.skills_dir / LOCK_FILE),
                policy=self._policy(),
                prompts_dir=self.root / "prompts",
                cases_path=self.root / "tests" / "eval" / "whatsapp_cases.jsonl",
                runs_dir=Path(tmp) / "runs",
                with_eval=with_eval,
            )

    # ------------------------------------------------------------------ submitting

    def _submission_path(self, name: str) -> Path:
        return self._incoming_root() / SUBMISSIONS / f"{self._name(name)}.json"

    def _submission(self, name: str) -> dict[str, Any] | None:
        p = self._submission_path(name)
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _unsubmit(self, name: str) -> None:
        p = self._submission_path(name)
        sub = self._submission(name)
        if sub is not None and not sub.get("review"):
            p.unlink(missing_ok=True)

    async def submit(
        self, name: str, summary: str, request_id: str | None = None
    ) -> dict[str, Any]:
        result = await self.check(name, with_eval=True)
        if not result.ok:
            return {"submitted": False, "why": "checks failed", "report": result.as_dict()}
        if request_id is not None and not REQUEST_ID.match(request_id):
            raise WorkspaceError("request_id looks wrong; use the id from list_requests")
        d = self.skill_path(name)
        if hash_skill(d) != result.report.sha256:
            return {"submitted": False, "why": "files changed during the checks; submit again"}
        p = self._submission_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "name": name,
            "summary": " ".join(str(summary).split())[:1000],
            "sha256": result.report.sha256,
            "request_id": request_id,
            "submitted_at": to_iso(datetime.now(UTC)),
            "report": result.as_dict(),
            "review": None,
        }
        p.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return {
            "submitted": True,
            "note": "Waiting for Hemanth to review it in the FLINS dashboard. Editing any file "
            "withdraws the submission.",
        }

    # ------------------------------------------------------------------ requests

    def requests(self) -> list[dict[str, str]]:
        d = self.incoming_dir / REQUESTS
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.glob("*.md")):
            if REQUEST_ID.match(p.stem) and not _is_link(p):
                first = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                out.append({"id": p.stem, "title": (first[0] if first else "")[:120]})
        return out

    def read_request(self, request_id: str) -> str:
        if not REQUEST_ID.match(str(request_id)):
            raise WorkspaceError("unknown request id")
        p = self.incoming_dir / REQUESTS / f"{request_id}.md"
        if not p.is_file() or _is_link(p):
            raise WorkspaceError("unknown request id")
        return p.read_text(encoding="utf-8", errors="replace")


def write_request(incoming_dir: Path, text: str, source: str, now: datetime) -> str:
    """'Make a skill that …' from voice or self-chat -> a file Hermes can pick up."""
    d = incoming_dir / REQUESTS
    d.mkdir(parents=True, exist_ok=True)
    rid = f"{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    body = " ".join(text.split())[:2000]
    (d / f"{rid}.md").write_text(
        f"{body}\n\nAsked by the user ({source}) at {to_iso(now)}.\n"
        "Build it as a FLINS skill with made-up test data, then submit_skill with this "
        "request_id.\n",
        encoding="utf-8",
    )
    return rid


def install(src: Path, skills_dir: Path) -> Path:
    """Copy a reviewed skill into skills/<name>, replacing the old version."""
    dest = skills_dir / src.name
    tmp = skills_dir / f".{src.name}.installing"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    return dest


def _is_link(p: Path) -> bool:
    return p.is_symlink() or bool(getattr(p, "is_junction", lambda: False)())
