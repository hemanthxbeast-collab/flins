"""One way to check a skill, used by `flins skills test`, the skill-dev MCP server and the
dashboard: validate -> fixtures in the sandbox -> the safety eval with the skill switched on."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from flins.core.clock import to_iso
from flins.core.policy_schema import Policy
from flins.evaluation import load_cases, run_eval
from flins.skills.engine import FixtureResult, SkillEngine, run_fixtures
from flins.skills.lock import LOCK_FILE, SkillLock
from flins.skills.validator import Report, validate


@dataclass
class CheckResult:
    report: Report
    fixtures: list[FixtureResult] = field(default_factory=list)
    eval_cases: int = 0
    eval_ran: int = 0  # cases where the skill actually ran
    eval_unsafe: list[str] = field(default_factory=list)
    eval_skipped: str | None = None

    @property
    def ok(self) -> bool:
        return (
            self.report.ok
            and bool(self.fixtures)
            and all(f.ok for f in self.fixtures)
            and not self.eval_unsafe
        )

    def lines(self) -> list[str]:
        r = self.report
        out = [f"{r.name}: {'valid' if r.ok else 'NOT valid'}"]
        out += [f"  error: {e}" for e in r.errors]
        out += [f"  warning: {w}" for w in r.warnings]
        out += [f"  needs your approval: {n}" for n in r.needs_approval]
        for f in self.fixtures:
            out.append(f"  fixture {f.name}: {'pass' if f.ok else 'FAIL'} ({f.ms} ms)")
            out += [f"    {p}" for p in f.problems]
        if self.eval_skipped:
            out.append(f"  eval: skipped ({self.eval_skipped})")
        elif self.eval_cases:
            out.append(
                f"  eval: {self.eval_cases} cases, skill ran in {self.eval_ran}, "
                f"{len(self.eval_unsafe)} unsafe"
            )
            out += [f"    unsafe: {u}" for u in self.eval_unsafe[:5]]
        return out

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "valid": self.report.ok,
            "errors": self.report.errors,
            "warnings": self.report.warnings,
            "needs_approval": self.report.needs_approval,
            "fixtures": [
                {"name": f.name, "ok": f.ok, "problems": f.problems} for f in self.fixtures
            ],
            "eval": {
                "cases": self.eval_cases,
                "skill_ran": self.eval_ran,
                "unsafe": self.eval_unsafe,
                "skipped": self.eval_skipped,
            },
            "sha256": self.report.sha256,
        }


async def check_skill(
    skill_dir: Path,
    *,
    lock: SkillLock | None,
    policy: Policy,
    prompts_dir: Path,
    cases_path: Path,
    runs_dir: Path,
    with_eval: bool = True,
    lint: bool = True,
) -> CheckResult:
    """Validation must pass before anything runs. The eval uses a throwaway lock in which only
    this skill is approved, so nothing is ever written to your real skills.lock."""
    report = validate(skill_dir, lock, lint=lint)
    result = CheckResult(report)
    if not report.ok or report.manifest is None:
        return result
    manifest = report.manifest
    result.fixtures = await run_fixtures(skill_dir, manifest, runs_dir)
    if not with_eval:
        result.eval_skipped = "not requested"
        return result
    if not manifest.triggers.categories:
        result.eval_skipped = "this skill doesn't answer messages"
        return result
    if not all(f.ok for f in result.fixtures):
        result.eval_skipped = "fix the fixtures first"
        return result

    assert report.sha256 is not None
    with tempfile.TemporaryDirectory() as tmp:
        trial = SkillLock(Path(tmp) / LOCK_FILE)
        trial.approve(manifest, report.sha256, to_iso(datetime.now(UTC)))
        engines: list[SkillEngine] = []

        def factory(conn: aiosqlite.Connection) -> SkillEngine:
            e = SkillEngine(
                skill_dir.parent,
                runs_dir,
                tz=ZoneInfo(policy.user.timezone),
                conn=conn,
                lock=trial,
            )
            engines.append(e)
            return e

        cases = load_cases(cases_path)
        evaluated = await run_eval(cases, policy, Path(tmp) / "eval", prompts_dir, skills=factory)
    result.eval_cases = len(cases)
    result.eval_ran = sum(e.runs_started for e in engines)
    result.eval_unsafe = [f"{r.id}: {'; '.join(r.unsafe)}" for r in evaluated.unsafe]
    return result
