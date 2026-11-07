"""`flins skills list | validate | test | enable | disable`. Enabling always needs you."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from flins.core.audit import AuditLog
from flins.core.clock import to_iso
from flins.evaluation import load_cases, run_eval
from flins.settings import Settings
from flins.skills.engine import FixtureResult, Skill, SkillEngine, run_fixtures
from flins.skills.lock import LOCK_FILE, SkillLock, hash_skill
from flins.skills.validator import Report, validate

Confirm = Callable[[list[str], str], bool]  # (what you're approving, the word to type) -> ok


def _engine(settings: Settings, conn: aiosqlite.Connection | None = None) -> SkillEngine:
    return SkillEngine(
        settings.skills_dir,
        settings.skill_runs_dir,
        tz=ZoneInfo(settings.policy.user.timezone),
        conn=conn,
    )


def list_lines(settings: Settings) -> list[str]:
    skills = _engine(settings).all()
    if not skills:
        return [f"No skills in {settings.skills_dir}."]
    out = []
    for s in skills:
        m = s.manifest
        version = m.version if m else "?"
        trig = ""
        if m is not None:
            t = m.triggers
            parts = [*t.categories, *(f"command:{c}" for c in t.commands)]
            trig = f" | {', '.join(parts)}"
            if t.modes:
                trig += f" in {'/'.join(x.value for x in t.modes)}"
        extra = f" ({s.problem})" if s.problem else ""
        out.append(f"  {s.name:<20} {version:<7} {s.status:<13}{trig}{extra}")
    incoming = settings.skills_dir / "_incoming"
    waiting = sorted(d.name for d in incoming.iterdir() if d.is_dir()) if incoming.is_dir() else []
    if waiting:
        out.append(f"  waiting in _incoming: {', '.join(waiting)}")
    return out


def report_lines(r: Report) -> list[str]:
    out = [f"{r.name}: {'valid' if r.ok else 'NOT valid'}"]
    out += [f"  error: {e}" for e in r.errors]
    out += [f"  warning: {w}" for w in r.warnings]
    out += [f"  needs your approval: {n}" for n in r.needs_approval]
    return out


def _skill_dir(settings: Settings, name: str) -> Path | None:
    d = settings.skills_dir / name
    if name.startswith((".", "_")) or not d.is_dir() or not (d / "flins.skill.yaml").is_file():
        return None
    return d


async def test_skill(settings: Settings, name: str) -> tuple[bool, list[str]]:
    """Validate, run the fixtures in the sandbox, then the safety eval with the skill switched
    on (recorded AI answers). Passing needs all three, and 0 unsafe in the eval."""
    d = _skill_dir(settings, name)
    if d is None:
        return False, [f"No skill called {name!r} in {settings.skills_dir}."]
    lock = SkillLock(settings.skills_dir / LOCK_FILE)
    report = validate(d, lock)
    lines = report_lines(report)
    if not report.ok or report.manifest is None:
        return False, lines
    manifest = report.manifest
    fixtures: list[FixtureResult] = await run_fixtures(d, manifest, settings.skill_runs_dir)
    for f in fixtures:
        status = "pass" if f.ok else "FAIL"
        lines.append(f"  fixture {f.name}: {status} ({f.ms} ms)")
        lines += [f"    {p}" for p in f.problems]
    ok = all(f.ok for f in fixtures)
    if not manifest.triggers.categories:
        lines.append("  eval: skipped (this skill doesn't answer messages)")
        return ok, lines

    # The shared eval set, with only this skill enabled (in a throwaway lock, never saved).
    with tempfile.TemporaryDirectory() as tmp:
        trial = SkillLock(Path(tmp) / LOCK_FILE)
        trial.approve(manifest, hash_skill(d), to_iso(datetime.now(UTC)))
        engines: list[SkillEngine] = []

        def factory(conn: aiosqlite.Connection) -> SkillEngine:
            e = SkillEngine(
                settings.skills_dir,
                settings.skill_runs_dir,
                tz=ZoneInfo(settings.policy.user.timezone),
                conn=conn,
                lock=trial,
            )
            engines.append(e)
            return e

        cases = load_cases(settings.root / "tests" / "eval" / "whatsapp_cases.jsonl")
        result = await run_eval(
            cases, settings.policy, settings.data_dir / "eval", settings.prompts_dir, skills=factory
        )
    ran = sum(e.runs_started for e in engines)
    lines.append(f"  eval: {len(cases)} cases, skill ran in {ran}, {len(result.unsafe)} unsafe")
    for r in result.unsafe[:5]:
        lines.append(f"    unsafe: {r.id}: {'; '.join(r.unsafe)}")
    return ok and not result.unsafe, lines


async def enable_skill(
    settings: Settings, conn: aiosqlite.Connection, name: str, confirm: Confirm
) -> tuple[bool, list[str]]:
    ok, lines = await test_skill(settings, name)
    if not ok:
        return False, [*lines, "Not enabled: fix the problems above first."]
    d = _skill_dir(settings, name)
    assert d is not None
    lock = SkillLock(settings.skills_dir / LOCK_FILE)
    report = validate(d, lock, lint=False)
    assert report.manifest is not None and report.sha256 is not None
    m = report.manifest
    what = [f"Enable {m.name} {m.version} by {m.author}. It may:", *m.summary_lines()]
    if report.needs_approval:
        what += ["Changes since what you approved before:", *report.needs_approval]
    if not confirm(what, m.name):
        return False, [*lines, "Not enabled."]
    lock.approve(m, report.sha256, to_iso(datetime.now(UTC)))
    lock.save()
    await AuditLog(conn).record(
        "user",
        "skill.enabled",
        {
            "skill": m.name,
            "version": m.version,
            "sha256": report.sha256,
            "permissions": m.permissions,
            "network": m.network,
            "max_level": m.max_level.name,
        },
    )
    await _engine(settings, conn).sync_db()
    return True, [*lines, f"Enabled {m.name}. Any change to its files switches it off again."]


async def disable_skill(
    settings: Settings, conn: aiosqlite.Connection, name: str
) -> tuple[bool, list[str]]:
    lock = SkillLock(settings.skills_dir / LOCK_FILE)
    if not lock.disable(name):
        return False, [f"{name} isn't enabled."]
    lock.save()
    await AuditLog(conn).record("user", "skill.disabled", {"skill": name})
    await _engine(settings, conn).sync_db()
    return True, [f"Disabled {name}."]


def status_of(settings: Settings, name: str) -> Skill | None:
    return _engine(settings).get(name)
