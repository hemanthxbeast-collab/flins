"""`flins` command line."""

from __future__ import annotations

import argparse
import asyncio
import platform
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from flins import __version__
from flins.core.audit import AuditLog
from flins.core.db import connect, migrate, table_names
from flins.settings import Settings, find_root, load_settings

OK, WARN, FAIL = "ok", "warn", "FAIL"

# Commands that exist in the CLI but arrive in a later milestone.
STUBS: dict[str, str] = {
    "run": "M2",
    "stop": "M4",
    "mode": "M2",
    "wa pair": "M1",
    "wa tail": "M1",
    "skills": "M7",
}


def _line(status: str, msg: str) -> None:
    print(f"  [{status:>4}] {msg}")


def _check_node() -> tuple[str, str]:
    node = shutil.which("node")
    if not node:
        return WARN, "Node.js not found (needed for the WhatsApp sidecar from M1)"
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10)
        version = out.stdout.strip()
        major = int(version.lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return WARN, "Node.js found but its version could not be read"
    if major < 20:
        return WARN, f"Node.js {version} is too old; M1 needs >= 20"
    return OK, f"Node.js {version}"


async def _check_db(settings: Settings) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    conn = await connect(settings.db_path)
    try:
        applied = await migrate(conn)
        tables = await table_names(conn)
        async with conn.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        journal = row[0] if row else "?"
        what = f"applied {', '.join(applied)}" if applied else "schema up to date"
        results.append((OK, f"database {settings.db_path.name}: {what}, {len(tables)} tables"))
        status = OK if journal.lower() == "wal" else WARN
        results.append((status, f"journal mode {journal}"))
        await AuditLog(conn).record("cli", "doctor.run", {"version": __version__})
        results.append((OK, "audit log writable"))
    finally:
        await conn.close()
    return results


def cmd_doctor(root: Path | None) -> int:
    print(f"FLINS {__version__} doctor")
    failed = False

    py = sys.version_info
    py_ok = py >= (3, 12)
    _line(OK if py_ok else FAIL, f"Python {platform.python_version()}")
    failed |= not py_ok

    try:
        settings = load_settings(root)
    except Exception as exc:
        _line(FAIL, f"config: {exc}")
        return 1

    _line(OK, f"project root {settings.root}")
    for note in settings.notes:
        _line(WARN, note)
    policy = settings.policy
    _line(OK, f"policy loaded for {policy.user.name}")
    for w in policy.warnings:
        _line(WARN, f"policy safety clamp: {w}")
    _line(OK if policy.dry_run else WARN,
          "dry_run ON: nothing will ever be sent" if policy.dry_run
          else "dry_run OFF: FLINS may send messages")

    try:
        for status, msg in asyncio.run(_check_db(settings)):
            _line(status, msg)
            failed |= status == FAIL
    except Exception as exc:
        _line(FAIL, f"database: {exc}")
        failed = True

    _line(*_check_node())

    s = settings.secrets
    _line(OK if s.anthropic_api_key else WARN,
          "ANTHROPIC_API_KEY set" if s.anthropic_api_key
          else "ANTHROPIC_API_KEY not set (needed from M3)")
    _line(OK if s.flins_wa_secret else WARN,
          "FLINS_WA_SECRET set" if s.flins_wa_secret
          else "FLINS_WA_SECRET not set (needed from M1)")

    print("result:", "problems found" if failed else "healthy")
    return 1 if failed else 0


def _stub(name: str) -> int:
    print(f"`flins {name}` is not built yet. It arrives in milestone {STUBS[name]}.")
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="flins", description="FLINS personal assistant")
    p.add_argument("--version", action="version", version=f"flins {__version__}")
    p.add_argument("--root", type=Path, default=None, help="project root (default: auto-detect)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check config, database and tools")
    sub.add_parser("run", help="start FLINS (M2)")
    sub.add_parser("stop", help="kill switch: pause all sending (M4)")
    mode = sub.add_parser("mode", help="show or change mode (M2)")
    mode.add_argument("args", nargs="*")
    wa = sub.add_parser("wa", help="WhatsApp sidecar commands (M1)")
    wa_sub = wa.add_subparsers(dest="wa_command", required=True)
    wa_sub.add_parser("pair", help="pair FLINS as a linked device")
    wa_sub.add_parser("tail", help="live feed of incoming messages")
    skills = sub.add_parser("skills", help="manage skills (M7)")
    skills.add_argument("args", nargs="*")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        root = args.root.resolve() if args.root else None
        if root is not None:
            root = find_root(root)
        return cmd_doctor(root)
    if args.command == "wa":
        return _stub(f"wa {args.wa_command}")
    return _stub(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
