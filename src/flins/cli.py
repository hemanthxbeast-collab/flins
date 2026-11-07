"""`flins` command line."""

from __future__ import annotations

import argparse
import asyncio
import platform
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from flins import __version__
from flins.adapters.baileys import BaileysAdapter, SidecarAuthError, SidecarBusyError
from flins.adapters.base import ConnectionChanged, SidecarHello
from flins.adapters.protocol import MIN_SECRET_LENGTH
from flins.adapters.sidecar_proc import Sidecar, SidecarSetupError, port_open
from flins.core.audit import AuditLog
from flins.core.db import connect, migrate, table_names
from flins.core.events import EventBus
from flins.core.ingest import ContactsResult, Ingestor, StoredMessage
from flins.settings import Settings, find_root, load_settings

OK, WARN, FAIL = "ok", "warn", "FAIL"

# Commands that exist in the CLI but arrive in a later milestone.
STUBS: dict[str, str] = {
    "run": "M2",
    "stop": "M4",
    "mode": "M2",
    "skills": "M7",
}

SECRET_HINT = (
    "FLINS_WA_SECRET is missing or shorter than 32 characters. Add a line to .env, e.g.:\n"
    '  FLINS_WA_SECRET=<output of: python -c "import secrets; print(secrets.token_hex(32))">'
)


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
    _line(
        OK if policy.dry_run else WARN,
        "dry_run ON: nothing will ever be sent"
        if policy.dry_run
        else "dry_run OFF: FLINS may send messages",
    )

    try:
        for status, msg in asyncio.run(_check_db(settings)):
            _line(status, msg)
            failed |= status == FAIL
    except Exception as exc:
        _line(FAIL, f"database: {exc}")
        failed = True

    _line(*_check_node())
    sidecar = Sidecar(settings.root, settings.data_dir, settings.app.network.sidecar_port)
    _line(
        OK if sidecar.script.is_file() else WARN,
        "WhatsApp sidecar built"
        if sidecar.script.is_file()
        else "WhatsApp sidecar not built (npm --prefix wa-sidecar install && "
        "npm --prefix wa-sidecar run build)",
    )
    _line(
        OK if sidecar.is_paired() else WARN,
        "WhatsApp paired" if sidecar.is_paired() else "WhatsApp not paired (flins wa pair)",
    )

    s = settings.secrets
    _line(
        OK if s.anthropic_api_key else WARN,
        "ANTHROPIC_API_KEY set"
        if s.anthropic_api_key
        else "ANTHROPIC_API_KEY not set (needed from M3)",
    )
    _line(
        OK if s.flins_wa_secret else WARN,
        "FLINS_WA_SECRET set" if s.flins_wa_secret else "FLINS_WA_SECRET not set (needed from M1)",
    )

    print("result:", "problems found" if failed else "healthy")
    return 1 if failed else 0


def _load(root: Path | None) -> Settings | None:
    try:
        return load_settings(root)
    except Exception as exc:
        print(f"config error: {exc}")
        return None


def cmd_wa_pair(root: Path | None, force: bool) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    sidecar = Sidecar(settings.root, settings.data_dir, settings.app.network.sidecar_port)
    try:
        return sidecar.pair(force=force)
    except SidecarSetupError as exc:
        print(exc)
        return 1


def _feed_line(m: StoredMessage) -> str:
    when = datetime.fromisoformat(m.ts).astimezone().strftime("%H:%M:%S")
    who = m.contact_name or m.chat_jid
    where = " [group]" if m.is_group else ""
    body = m.text if m.text is not None else ""
    body = body.replace("\n", " ⏎ ")
    if len(body) > 120:
        body = body[:117] + "..."
    tag = "" if m.type == "text" else f"<{m.type}> "
    return f"{when}  {m.direction:<3} {who}{where}: {tag}{body}"


STOP_STATES = {
    "not_paired": "WhatsApp isn't paired yet. Run: uv run flins wa pair",
    "logged_out": "WhatsApp logged this device out. Run: uv run flins wa pair",
    "conflict": (
        "Another WhatsApp Web session took over (is Hermes' WhatsApp gateway on?). Stopping."
    ),
}


async def _wa_tail(settings: Settings, spawn: bool) -> int:
    secret_value = settings.secrets.flins_wa_secret
    secret = secret_value.get_secret_value() if secret_value else ""
    if len(secret) < MIN_SECRET_LENGTH:
        print(SECRET_HINT)
        return 1

    port = settings.app.network.sidecar_port
    sidecar = Sidecar(settings.root, settings.data_dir, port)
    spawned = False
    if not port_open(port):
        if not spawn:
            print(f"no sidecar on port {port} (and --no-spawn was given)")
            return 1
        try:
            await sidecar.start(secret)
        except SidecarSetupError as exc:
            print(exc)
            return 1
        spawned = True
        print(f"started WhatsApp sidecar (log: {sidecar.log_path})")

    conn = await connect(settings.db_path)
    await migrate(conn)
    audit = AuditLog(conn)
    ingest = Ingestor(conn, EventBus(), audit)
    adapter = BaileysAdapter(port, secret)
    counts = {"in": 0, "out": 0, "contacts": 0}
    code = 0
    print("watching WhatsApp (read-only). Ctrl+C to stop.")
    try:
        async for event in adapter.events():
            result = await ingest.handle(event)
            if isinstance(event, ConnectionChanged | SidecarHello):
                print(f"-- whatsapp: {event.state}")
                if event.state in STOP_STATES:
                    print(STOP_STATES[event.state])
                    code = 1
                    break
            if isinstance(result, StoredMessage):
                counts["in" if result.direction == "IN" else "out"] += 1
                print(_feed_line(result))
            elif isinstance(result, ContactsResult) and result.created:
                counts["contacts"] += result.created
                print(f"-- imported {result.created} new contacts")
    except (SidecarAuthError, SidecarBusyError) as exc:
        print(exc)
        code = 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if spawned:
            await adapter.request_sidecar_shutdown()
        await adapter.close()
        if spawned:
            await sidecar.stop()
        await conn.close()
        print(
            f"stopped. {counts['in']} incoming, {counts['out']} outgoing, "
            f"{counts['contacts']} new contacts stored."
        )
    return code


def cmd_wa_tail(root: Path | None, spawn: bool) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    try:
        return asyncio.run(_wa_tail(settings, spawn))
    except KeyboardInterrupt:
        return 0


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
    pair = wa_sub.add_parser("pair", help="pair FLINS as a linked device (shows a QR)")
    pair.add_argument("--force", action="store_true", help="replace an existing pairing")
    tail = wa_sub.add_parser("tail", help="store and show incoming messages live (read-only)")
    tail.add_argument("--no-spawn", action="store_true", help="don't start the sidecar")
    skills = sub.add_parser("skills", help="manage skills (M7)")
    skills.add_argument("args", nargs="*")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = find_root(args.root.resolve()) if args.root else None
    if args.command == "doctor":
        return cmd_doctor(root)
    if args.command == "wa":
        if args.wa_command == "pair":
            return cmd_wa_pair(root, args.force)
        return cmd_wa_tail(root, spawn=not args.no_spawn)
    return _stub(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
