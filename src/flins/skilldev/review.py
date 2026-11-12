"""Reviewing Hermes' skills on Windows (dashboard). Nothing Hermes wrote is trusted: every check
runs again here, and approval is tied to the exact files you looked at (their hash)."""

from __future__ import annotations

import difflib
import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from flins.core.audit import AuditLog
from flins.core.clock import to_iso
from flins.settings import Settings
from flins.skilldev.workspace import (
    INCOMING,
    NAME,
    SUBMISSIONS,
    Workspace,
    WorkspaceError,
    install,
)
from flins.skills.analysis import Analysis, analyse
from flins.skills.checks import check_skill
from flins.skills.engine import FixtureResult, SkillEngine, run_fixtures
from flins.skills.lock import LOCK_FILE, SkillLock, hash_skill, skill_files
from flins.skills.validator import Report, validate

MAX_DIFF = 200_000


@dataclass
class Waiting:
    name: str
    summary: str
    submitted_at: str
    review: dict[str, Any] | None
    current: bool  # files still match what Hermes submitted


@dataclass
class ReviewView:
    name: str
    sha256: str
    report: Report
    fixtures: list[FixtureResult]
    permission_lines: list[str]
    diff: str
    files: list[str]
    installed: bool
    submission: dict[str, Any] | None
    submission_current: bool
    analysis: Analysis | None = None  # what it could do (phase 8)
    problems: list[str] = field(default_factory=list)

    @property
    def can_approve(self) -> bool:
        return self.report.ok and bool(self.fixtures) and all(f.ok for f in self.fixtures)


class Reviewer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.skills_dir = settings.skills_dir
        self.incoming = self.skills_dir / INCOMING
        self.workspace = Workspace(self.skills_dir, self.incoming, settings.root)
        self.workspace_tz = ZoneInfo(settings.policy.user.timezone)

    def _lock(self) -> SkillLock:
        return SkillLock(self.skills_dir / LOCK_FILE)

    def _dir(self, name: str) -> Path:
        if not NAME.match(name):
            raise WorkspaceError("bad skill name")
        return self.workspace.skill_path(name)

    def _submission(self, name: str) -> dict[str, Any] | None:
        p = self.incoming / SUBMISSIONS / f"{name}.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write_submission(self, name: str, data: dict[str, Any]) -> None:
        p = self.incoming / SUBMISSIONS / f"{name}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------ lists

    def waiting(self) -> list[Waiting]:
        """Submitted skills whose folder is still in _incoming."""
        out: list[Waiting] = []
        if not self.incoming.is_dir():
            return out
        for d in sorted(self.incoming.iterdir()):
            if not d.is_dir() or not NAME.match(d.name):
                continue
            sub = self._submission(d.name)
            if sub is None:
                continue
            review = sub.get("review") if isinstance(sub.get("review"), dict) else None
            if review and review.get("status") == "approved":
                continue
            out.append(
                Waiting(
                    d.name,
                    str(sub.get("summary", ""))[:300],
                    str(sub.get("submitted_at", "")),
                    review,
                    sub.get("sha256") == hash_skill(d),
                )
            )
        return out

    def pending_count(self) -> int:
        return sum(1 for w in self.waiting() if w.review is None and w.current)

    # ------------------------------------------------------------------ one skill

    async def view(self, name: str) -> ReviewView:
        d = self._dir(name)
        if not d.is_dir():
            raise WorkspaceError(f"no skill {name!r} waiting in _incoming")
        lock = self._lock()
        report = validate(d, lock)
        fixtures: list[FixtureResult] = []
        if report.ok and report.manifest is not None:
            fixtures = await run_fixtures(d, report.manifest, self.settings.skill_runs_dir)
        m = report.manifest
        perm_lines = m.summary_lines() if m is not None else []
        if m is not None:
            t = m.triggers
            when = [*t.categories, *(f"command: {c}" for c in t.commands)]
            perm_lines.insert(0, f"runs for: {', '.join(when) or 'nothing'}")
            if t.modes:
                perm_lines.insert(1, f"only in: {', '.join(x.value for x in t.modes)}")
        installed_dir = self.skills_dir / name
        sha = hash_skill(d)
        sub = self._submission(name)
        return ReviewView(
            name=name,
            sha256=sha,
            report=report,
            fixtures=fixtures,
            permission_lines=perm_lines,
            diff=_diff(installed_dir if installed_dir.is_dir() else None, d),
            files=[p.relative_to(d).as_posix() for p in skill_files(d)],
            installed=installed_dir.is_dir(),
            submission=sub,
            submission_current=bool(sub and sub.get("sha256") == sha),
            analysis=analyse(m, lock.entries.get(name)) if m is not None else None,
        )

    async def approve(
        self, conn: aiosqlite.Connection, name: str, sha: str, *, acknowledged: bool = False
    ) -> tuple[bool, str]:
        """`acknowledged`: you ticked "I understand" for a high-risk skill."""
        d = self._dir(name)
        if not d.is_dir():
            return False, f"{name} isn't waiting for review."
        if hash_skill(d) != sha:
            return False, f"{name} changed after you opened it. Look at it again."
        lock = self._lock()
        s = self.settings
        result = await check_skill(
            d,
            lock=lock,
            policy=s.policy,
            prompts_dir=s.prompts_dir,
            cases_path=s.root / "tests" / "eval" / "whatsapp_cases.jsonl",
            runs_dir=s.skill_runs_dir,
        )
        if not result.ok:
            first = (result.report.errors + result.eval_unsafe + ["fixtures failed"])[0]
            return False, f"Not approved: checks failed ({first})."
        if hash_skill(d) != sha or result.report.sha256 != sha:
            return False, f"{name} changed during the checks. Look at it again."
        manifest = result.report.manifest
        assert manifest is not None
        analysis = analyse(manifest, lock.entries.get(name))
        if analysis.needs_acknowledgement and not acknowledged:
            return False, (
                f"{name} is high risk. Read the points marked HIGH, tick the box, then approve."
            )
        dest = install(d, self.skills_dir)
        if hash_skill(dest) != sha:
            shutil.rmtree(dest, ignore_errors=True)
            return False, "The copy didn't match what you reviewed; nothing was enabled."
        now = to_iso(datetime.now(UTC))
        lock.approve(manifest, sha, now)
        lock.save()
        await AuditLog(conn).record(
            "user",
            "skill.enabled",
            {
                "skill": name,
                "version": manifest.version,
                "author": manifest.author,
                "sha256": sha,
                "permissions": manifest.permissions,
                "network": manifest.network,
                "max_level": manifest.max_level.name,
                "via": "dashboard review",
                "risk": analysis.risk,
                "acknowledged": bool(acknowledged),
                "eval": {"cases": result.eval_cases, "skill_ran": result.eval_ran},
            },
        )
        sub = self._submission(name) or {"name": name}
        sub["review"] = {"status": "approved", "at": now}
        self._write_submission(name, sub)
        shutil.rmtree(d, ignore_errors=True)
        await SkillEngine(
            self.skills_dir, s.skill_runs_dir, tz=ZoneInfo(s.policy.user.timezone), conn=conn
        ).sync_db()
        return True, f"Enabled {name} {manifest.version}."

    async def reject(self, conn: aiosqlite.Connection, name: str, note: str) -> str:
        d = self._dir(name)
        if not d.is_dir():
            return f"{name} isn't waiting for review."
        note = " ".join(note.split())[:1000]
        sub = self._submission(name) or {"name": name}
        sub["review"] = {"status": "rejected", "note": note, "at": to_iso(datetime.now(UTC))}
        self._write_submission(name, sub)
        await AuditLog(conn).record("user", "skill.rejected", {"skill": name, "note": note})
        return f"Rejected {name}. Hermes can read your note."


def _diff(old: Path | None, new: Path) -> str:
    def texts(d: Path | None) -> dict[str, list[str]]:
        if d is None:
            return {}
        return {
            p.relative_to(d).as_posix(): p.read_text(encoding="utf-8", errors="replace").splitlines(
                keepends=True
            )
            for p in skill_files(d)
            if p.stat().st_size <= 200_000
        }

    a, b = texts(old), texts(new)
    chunks: list[str] = []
    for name in sorted(set(a) | set(b)):
        lines = difflib.unified_diff(
            a.get(name, []),
            b.get(name, []),
            fromfile=f"installed/{name}" if name in a else "/dev/null",
            tofile=f"new/{name}" if name in b else "/dev/null",
        )
        chunks.append("".join(lines))
    out = "\n".join(c for c in chunks if c)
    return out if len(out) <= MAX_DIFF else out[:MAX_DIFF] + "\n… (diff cut short)"
