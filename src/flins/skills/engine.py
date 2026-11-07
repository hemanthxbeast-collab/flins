"""Find skills, check they are approved and unchanged, run them, record every run."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import aiosqlite

from flins.core.audit import AuditLog
from flins.core.clock import to_iso
from flins.core.guard import GuardInput, check
from flins.core.levels import Level, Mode
from flins.skills.ctx_api import Capabilities, DbProvider, FixtureProvider, Provider
from flins.skills.lock import LOCK_FILE, SkillLock, hash_skill
from flins.skills.manifest import Manifest, ManifestError, load_manifest
from flins.skills.models import ContactCard, SkillInput
from flins.skills.runtime import RunResult, run_skill

Status = Literal["enabled", "disabled", "not approved", "changed", "invalid"]
SPECIAL_DIRS = ("_incoming", "_requests")


@dataclass
class Skill:
    name: str
    dir: Path
    manifest: Manifest | None
    status: Status
    problem: str | None = None

    @property
    def runnable(self) -> bool:
        return self.status == "enabled" and self.manifest is not None


class SkillEngine:
    def __init__(
        self,
        skills_dir: Path,
        runs_dir: Path,
        *,
        tz: ZoneInfo,
        conn: aiosqlite.Connection | None = None,
        lock: SkillLock | None = None,
        faq_path: Path | None = None,
        calendar_path: Path | None = None,
    ) -> None:
        self.skills_dir = skills_dir
        self.faq_path = faq_path
        self.calendar_path = calendar_path
        self.runs_dir = runs_dir
        self.tz = tz
        self._conn = conn
        self._audit = AuditLog(conn) if conn is not None else None
        self.lock = lock or SkillLock(skills_dir / LOCK_FILE)
        self._reported: set[str] = set()
        self.runs_started = 0

    # ------------------------------------------------------------------ discovery

    def skill_dirs(self) -> list[Path]:
        if not self.skills_dir.is_dir():
            return []
        return sorted(
            d for d in self.skills_dir.iterdir() if d.is_dir() and not d.name.startswith(("_", "."))
        )

    def load(self, skill_dir: Path) -> Skill:
        name = skill_dir.name
        try:
            manifest = load_manifest(skill_dir)
        except ManifestError as exc:
            return Skill(name, skill_dir, None, "invalid", str(exc))
        entry = self.lock.entries.get(name)
        if entry is None:
            return Skill(name, skill_dir, manifest, "not approved")
        if not entry.enabled:
            return Skill(name, skill_dir, manifest, "disabled")
        if entry.sha256 != hash_skill(skill_dir) or entry.version != manifest.version:
            return Skill(
                name, skill_dir, manifest, "changed", "files changed since you approved it"
            )
        return Skill(name, skill_dir, manifest, "enabled")

    def all(self) -> list[Skill]:
        return [self.load(d) for d in self.skill_dirs()]

    def get(self, name: str) -> Skill | None:
        d = self.skills_dir / name
        return self.load(d) if d.is_dir() and not name.startswith(("_", ".")) else None

    async def _report_changed(self, skill: Skill) -> None:
        if skill.status == "changed" and skill.name not in self._reported:
            self._reported.add(skill.name)
            if self._audit is not None:
                await self._audit.record(
                    "skills", "skill.changed", {"skill": skill.name, "status": "not running"}
                )

    async def for_message(self, category: str, mode: Mode) -> list[Skill]:
        out = []
        for s in self.all():
            await self._report_changed(s)
            if not s.runnable or s.manifest is None:
                continue
            t = s.manifest.triggers
            if category not in t.categories or mode == Mode.PAUSED:
                continue
            if t.modes and mode not in t.modes:
                continue
            out.append(s)
        return out[:3]

    async def for_command(self, command: str) -> Skill | None:
        for s in self.all():
            await self._report_changed(s)
            if s.runnable and s.manifest is not None and command in s.manifest.triggers.commands:
                return s
        return None

    # ------------------------------------------------------------------ running

    def db_provider(self, chat_jid: str | None) -> DbProvider:
        assert self._conn is not None
        return DbProvider(
            self._conn,
            self.tz,
            chat_jid=chat_jid,
            faq_path=self.faq_path,
            calendar_path=self.calendar_path,
        )

    async def run(
        self,
        skill: Skill,
        base: SkillInput,
        provider: Provider,
        *,
        now: datetime,
        card: ContactCard | None = None,
    ) -> tuple[RunResult, int | None]:
        """Run once. Returns the result and the skill_runs row id (None without a database)."""
        m = skill.manifest
        assert m is not None and skill.runnable
        self.runs_started += 1
        inp = base.model_copy(update={"contact": card if m.allows("contacts:read_card") else None})
        caps = Capabilities(m, provider, self.tz, now, audit=self._audit)
        result = await run_skill(skill.dir, m, inp, caps, self.runs_dir)
        await self._drop_unpermitted(skill, result, caps)
        return result, await self._record(skill.name, inp, result, now)

    async def _drop_unpermitted(self, skill: Skill, r: RunResult, caps: Capabilities) -> None:
        m = skill.manifest
        assert m is not None
        if r.output is None or m.allows("wa:propose_reply"):
            return
        replies = [p for p in r.output.proposals if p.kind == "reply"]
        if replies:
            r.output.proposals = [p for p in r.output.proposals if p.kind != "reply"]
            await caps.deny("wa:propose_reply", "returned a reply without wa:propose_reply")
            r.denied = list(caps.denied)

    async def _record(self, name: str, inp: SkillInput, r: RunResult, now: datetime) -> int | None:
        if self._conn is None:
            return None
        digest = hashlib.sha256(inp.model_dump_json().encode("utf-8")).hexdigest()[:16]
        cur = await self._conn.execute(
            "INSERT INTO skill_runs (skill, input_hash, output, ms, error, denied, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                digest,
                r.output.model_dump_json() if r.output else None,
                r.ms,
                r.error,
                json.dumps(r.denied) if r.denied else None,
                to_iso(now),
            ),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def sync_db(self) -> None:
        """Mirror statuses into the `skills` table (for the dashboard)."""
        if self._conn is None:
            return
        db_status = {
            "enabled": "enabled",
            "disabled": "disabled",
            "not approved": "incoming",
            "changed": "incoming",
            "invalid": "incoming",
        }
        for s in self.all():
            entry = self.lock.entries.get(s.name)
            await self._conn.execute(
                "INSERT INTO skills (name, version, status, sha256, permissions, approved_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET version = excluded."
                "version, status = excluded.status, sha256 = excluded.sha256, permissions = "
                "excluded.permissions, approved_at = excluded.approved_at",
                (
                    s.name,
                    s.manifest.version if s.manifest else "?",
                    db_status[s.status],
                    entry.sha256 if entry else None,
                    json.dumps(s.manifest.permissions if s.manifest else []),
                    entry.approved_at if entry else None,
                ),
            )
        await self._conn.commit()


# ---------------------------------------------------------------------- fixture tests


@dataclass
class FixtureResult:
    name: str
    ok: bool
    problems: list[str] = field(default_factory=list)
    reply: str | None = None
    ms: int = 0


DEFAULT_INPUT: dict[str, Any] = {
    "trigger": "message",
    "mode": "WORKING",
    "until_local": "18:00",
    "now_local": "2026-11-08 14:00",
    "timezone": "Asia/Kolkata",
    "user_name": "Hemanth",
    "signature": "— FLINS, Hemanth's assistant",
    "facts": ["I am FLINS, Hemanth's assistant.", "Hemanth is working until 18:00."],
}


async def run_fixtures(skill_dir: Path, manifest: Manifest, runs_dir: Path) -> list[FixtureResult]:
    """`flins skills test`: every fixtures/*.json through the real sandbox, synthetic data only.

    Fixture: {"input": {...}, "contact": {...}, "ctx": {...}, "expect": {...}}. expect keys:
    reply_contains [..], reply_excludes [..], no_reply, note_contains [..], facts {..},
    denied [..], error.
    Every reply must also pass the output guard at the skill's max_level.
    """
    tz = ZoneInfo(DEFAULT_INPUT["timezone"])
    results = []
    for fx in sorted((skill_dir / "fixtures").glob("*.json")):
        spec = json.loads(fx.read_text(encoding="utf-8"))
        raw = {**DEFAULT_INPUT, **spec.get("input", {})}
        inp = SkillInput.model_validate(raw)
        card = ContactCard.model_validate(
            spec.get("contact", {"first_name": "Rahul", "tier": "trusted"})
        )
        if manifest.allows("contacts:read_card"):
            inp = inp.model_copy(update={"contact": card})
        now = datetime.fromisoformat(inp.now_local).replace(tzinfo=tz)
        caps = Capabilities(manifest, FixtureProvider(spec.get("ctx", {}), tz), tz, now)
        r = await run_skill(skill_dir, manifest, inp, caps, runs_dir)
        exp = spec.get("expect", {})
        problems: list[str] = []
        reply = r.output.first("reply") if r.output else None
        if reply is not None and not manifest.allows("wa:propose_reply"):
            problems.append("returned a reply without wa:propose_reply")
        if exp.get("error"):
            if r.error is None:
                problems.append("expected an error")
        elif r.error is not None:
            problems.append(f"error: {r.error}")
        text = reply.text if reply else None
        for s in exp.get("reply_contains", []):
            if text is None or s.lower() not in text.lower():
                problems.append(f"reply should contain {s!r}, got {text!r}")
        for s in exp.get("reply_excludes", []):
            if text is not None and s.lower() in text.lower():
                problems.append(f"reply should not contain {s!r}")
        notes = " ".join(
            p.text for p in (r.output.proposals if r.output else []) if p.kind == "note"
        )
        for want in exp.get("note_contains", []):
            if want.lower() not in notes.lower():
                problems.append(f"note should contain {want!r}, got {notes[:200]!r}")
        if exp.get("no_reply") and text is not None:
            problems.append(f"expected no reply, got {text!r}")
        for k, v in exp.get("facts", {}).items():
            got = r.output.facts.get(k) if r.output else None
            if got != v:
                problems.append(f"fact {k}: expected {v!r}, got {got!r}")
        if sorted(exp.get("denied", [])) != sorted(r.denied):
            problems.append(f"denied: expected {exp.get('denied', [])}, got {r.denied}")
        if reply is not None and manifest.max_level >= Level.DRAFT:
            facts = [
                *inp.facts,
                inp.signature,
                card.first_name,
                *(r.output.facts.values() if r.output else []),
            ]
            g = check(
                GuardInput(
                    reply=reply.text,
                    level=min(manifest.max_level, Level.AUTO),
                    inbound_texts=inp.messages,
                    facts=facts,
                    max_chars=max(320, len(reply.text)),
                    confidence=reply.confidence,
                    busy=inp.mode in ("WORKING", "FOCUS", "AWAY"),
                )
            )
            if not g.ok:
                problems.append(f"guard: {'; '.join(g.failures)}")
        results.append(FixtureResult(fx.stem, not problems, problems, text, r.ms))
    return results
