"""`flins skills validate <name>` (SPEC §9.4): checks a skill before it can be tested or enabled.

Static checks only. The sandbox (worker_main.py) is the second wall at run time.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from flins.skills.lock import SkillLock, escalation, hash_skill, skill_files
from flins.skills.manifest import Manifest, ManifestError, load_manifest

# Skills may import only these standard-library modules (and their own files). An allowlist is
# stricter than SPEC §9.4's banned list: anything new has to be added here on purpose.
ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "abc",
        "base64",
        "bisect",
        "calendar",
        "collections",
        "copy",
        "dataclasses",
        "datetime",
        "decimal",
        "difflib",
        "enum",
        "fractions",
        "functools",
        "hashlib",
        "heapq",
        "html",
        "itertools",
        "json",
        "math",
        "operator",
        "random",
        "re",
        "statistics",
        "string",
        "textwrap",
        "time",
        "typing",
        "unicodedata",
    }
)
BANNED_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "open",
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "input",
        "getattr",
        "setattr",
        "delattr",
    }
)
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


@dataclass
class Report:
    name: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    needs_approval: list[str] = field(default_factory=list)  # permission increases
    manifest: Manifest | None = None
    sha256: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_link(p: Path) -> bool:
    return p.is_symlink() or bool(getattr(p, "is_junction", lambda: False)())


def _local_modules(skill_dir: Path) -> set[str]:
    return {p.stem for p in skill_dir.glob("*.py")}


def check_source(source: str, filename: str, local: set[str]) -> list[str]:
    """AST checks on one file. Returns problems as 'file:line: message'."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [f"{filename}:{exc.lineno}: syntax error: {exc.msg}"]
    out: list[str] = []

    def bad(node: ast.AST, msg: str) -> None:
        out.append(f"{filename}:{getattr(node, 'lineno', '?')}: {msg}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in ALLOWED_IMPORTS and top not in local:
                    bad(node, f"import {alias.name!r} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: only within the skill folder
                continue
            top = (node.module or "").split(".")[0]
            if top not in ALLOWED_IMPORTS and top not in local:
                bad(node, f"import from {node.module!r} is not allowed")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in BANNED_CALLS:
                bad(node, f"{node.func.id}() is not allowed")
        elif isinstance(node, ast.Name) and node.id in BANNED_CALLS | {"__builtins__"}:
            bad(node, f"{node.id} is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            bad(node, f"attribute {node.attr!r} is not allowed")
    return out


def _skill_md(skill_dir: Path, name: str, report: Report) -> None:
    path = skill_dir / "SKILL.md"
    if not path.is_file():
        report.errors.append("SKILL.md is missing")
        return
    m = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
    if not m:
        report.errors.append("SKILL.md needs frontmatter with name and description")
        return
    fields = dict(
        (k.strip(), v.strip()) for k, _, v in (ln.partition(":") for ln in m.group(1).splitlines())
    )
    if fields.get("name") != name:
        report.errors.append(f"SKILL.md name must be {name!r}")
    if not fields.get("description"):
        report.errors.append("SKILL.md needs a description")


def _ruff(skill_dir: Path, report: Report) -> None:
    exe = shutil.which("ruff")
    cmd = [exe] if exe else [sys.executable, "-m", "ruff"]
    try:
        r = subprocess.run(
            [*cmd, "check", "--no-cache", "--quiet", "--select", "E9,F", str(skill_dir)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        report.warnings.append("ruff not available; lint skipped")
        return
    if r.returncode == 0:
        return
    if "No module named ruff" in r.stderr:
        report.warnings.append("ruff not available; lint skipped")
        return
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()][:10]
    report.errors.extend(f"ruff: {ln}" for ln in lines or ["lint failed"])


def validate(skill_dir: Path, lock: SkillLock | None = None, *, lint: bool = True) -> Report:
    report = Report(skill_dir.name)
    try:
        manifest = load_manifest(skill_dir)
    except ManifestError as exc:
        report.errors.append(str(exc))
        return report
    report.manifest = manifest
    _skill_md(skill_dir, manifest.name, report)
    links = [p for p in skill_dir.rglob("*") if _is_link(p)]
    if _is_link(skill_dir) or links:
        report.errors.append("links (symlinks/junctions) are not allowed in a skill")
        return report

    local = _local_modules(skill_dir)
    entry = skill_dir / f"{manifest.module}.py"
    if manifest.module in sys.stdlib_module_names:
        report.errors.append(f"entry module {manifest.module!r} shadows a standard module")
    if not entry.is_file():
        report.errors.append(f"entry module {manifest.module}.py is missing")
    for py in skill_files(skill_dir):
        if py.suffix != ".py" or py.relative_to(skill_dir).parts[0] == "tests":
            continue  # tests/ is never executed by FLINS (fixtures are)
        rel = py.relative_to(skill_dir).as_posix()
        source = py.read_text(encoding="utf-8")
        report.errors.extend(check_source(source, rel, local))
        if py == entry and manifest.function not in {
            n.name for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
        }:
            report.errors.append(f"{rel} has no top-level function {manifest.function}()")
    if not list((skill_dir / "fixtures").glob("*.json")):
        report.errors.append("add at least one test in fixtures/*.json")
    for fx in (skill_dir / "fixtures").glob("*.json"):
        try:
            json.loads(fx.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            report.errors.append(f"fixtures/{fx.name}: not valid JSON ({exc.msg})")
    if lint and not report.errors:
        _ruff(skill_dir, report)

    previous = lock.entries.get(manifest.name) if lock else None
    report.needs_approval = escalation(manifest, previous)
    report.sha256 = hash_skill(skill_dir)
    return report
