"""`flins` command line."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import platform
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn

from flins import __version__
from flins.adapters.baileys import BaileysAdapter, SidecarAuthError, SidecarBusyError
from flins.adapters.base import ConnectionChanged, SidecarHello
from flins.adapters.protocol import MIN_SECRET_LENGTH
from flins.adapters.sidecar_proc import Sidecar, SidecarSetupError, port_open
from flins.core.approvals import ApprovalService
from flins.core.audit import AuditLog
from flins.core.clock import SystemClock
from flins.core.commands import CommandRouter, SelfChatCommands
from flins.core.db import connect, migrate, table_names
from flins.core.events import Event, EventBus
from flins.core.ingest import ContactsResult, Ingestor, StoredMessage
from flins.core.modes import ModeManager
from flins.core.outbox import OutboxWorker
from flins.core.pipeline import Pipeline
from flins.core.selfchat import SelfChatNotifier
from flins.evaluation import load_cases, run_eval
from flins.llm.admin import list_models, status_lines, test_routes
from flins.llm.gateway import LLMError
from flins.llm.prompts import PromptError, load_prompt
from flins.settings import Settings, find_root, load_settings
from flins.web.app import create_app
from flins.wiring import build_router, make_brain

OK, WARN, FAIL = "ok", "warn", "FAIL"

# Commands that exist in the CLI but arrive in a later milestone.
STUBS: dict[str, str] = {
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

    for name in ("triage", "draft", "redflag"):
        try:
            prompt = load_prompt(settings.prompts_dir, name)
            _line(OK, f"prompt {prompt.version} ({prompt.model})")
        except (OSError, PromptError) as exc:
            _line(FAIL, f"prompt {name}: {exc}")
            failed = True

    s = settings.secrets
    router, problems = build_router(settings)
    for role, chain in router.describe().items():
        usable = [c for c in chain if not c.endswith("(no key)")]
        _line(OK if usable else WARN, f"AI {role}: {' -> '.join(chain) or 'nothing configured'}")
    if router.missing_roles():
        _line(WARN, "some AI jobs have no provider with a key: FLINS decides without AI")
    else:
        _line(OK, f"daily AI budget ${policy.limits.daily_llm_budget_usd:.2f}")
    for problem in problems:
        _line(FAIL, f"flins.yaml llm: {problem}")
        failed = True
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


LEVEL_WORDS = {
    "auto": "would reply by itself",
    "auto_safe": "would send a holding reply",
    "draft": "would draft a reply for your approval",
    "notify": "would just tell you",
    "ignore": "would ignore",
}


def _decision_line(d: dict[str, Any]) -> str:
    words = LEVEL_WORDS.get(d["action"], d["action"])
    extra = []
    if d.get("template"):
        extra.append(f"template '{d['template']}'")
    if d.get("hard_rules"):
        extra.append("rules: " + ", ".join(d["hard_rules"]))
    tail = f" ({'; '.join(extra)})" if extra else ""
    urgent = "  ⚠ URGENT" if d.get("escalate") else ""
    line = f"          -> {words} [{d['category']}]{tail}{urgent}"
    if d.get("draft"):
        how = "would send" if d.get("would_send") else "draft, held back"
        line += f"\n             {how}: {d['draft']}"
        if d.get("approval"):
            n = d["approval"]
            line += (
                f"\n             to send it: y{n} in your self-chat, or Approve on the dashboard"
            )
    return line


def _attach_printers(bus: EventBus) -> None:
    async def on_decision(e: Event) -> None:
        print(_decision_line(e.data))

    async def on_reply(e: Event) -> None:
        print(f"   FLINS: {e.data['text']}")

    async def on_mode(e: Event) -> None:
        if e.data.get("source") == "external":
            print(f"-- mode changed elsewhere: {e.data['from']} -> {e.data['to']}")

    async def on_expiring(e: Event) -> None:
        print(f"   FLINS: {e.data['text']}")

    bus.subscribe("decision.made", on_decision)
    bus.subscribe("command.reply", on_reply)
    bus.subscribe("mode.changed", on_mode)
    bus.subscribe("mode.expiring", on_expiring)

    async def on_outbox(e: Event) -> None:
        d = e.data
        words = {
            "sent": "SENT",
            "dry_run": "not sent (dry run is on)",
            "failed": "NOT sent",
            "cancelled": "cancelled",
        }.get(d["status"], d["status"])
        extra = f": {d['error']}" if d.get("error") else ""
        if d.get("kind") == "self" and d["status"] == "sent":
            return  # notes to your self-chat: don't clutter the terminal
        what = {"auto": "holding reply", "self": "self-chat note"}.get(d.get("kind", ""), "reply")
        print(f"   OUTBOX #{d['outbox_id']} ({what}) -> {words}{extra}")

    async def on_cancelled(e: Event) -> None:
        print(f"   OUTBOX: cancelled {e.data['count']} queued message(s) because FLINS was paused")

    bus.subscribe("outbox.update", on_outbox)
    bus.subscribe("outbox.cancelled", on_cancelled)


async def _run_live(settings: Settings, spawn: bool, *, brain: bool) -> int:
    """`wa tail` (brain=False): store and show. `run` (brain=True): also modes and decisions."""
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
    bus = EventBus()
    ingest = Ingestor(conn, bus, audit)
    adapter = BaileysAdapter(port, secret)
    counts = {"in": 0, "out": 0, "contacts": 0}
    code = 0
    modes: ModeManager | None = None
    ticker: asyncio.Task[None] | None = None
    dashboard: uvicorn.Server | None = None
    dashboard_task: asyncio.Task[None] | None = None
    if brain:
        clock = SystemClock()
        modes = await ModeManager.open(settings.db_path, bus, settings.policy, clock)
        ai = make_brain(settings, conn, clock)
        pipeline = Pipeline(
            conn,
            bus,
            settings.policy,
            modes,
            clock,
            brain=ai,
            tones=settings.tones,
            about=settings.about,
            debounce=True,
        )
        approvals = ApprovalService(conn, settings.policy)
        outbox = OutboxWorker(conn, settings.policy, adapter, clock, bus)
        SelfChatCommands(conn, bus, CommandRouter(modes, settings.policy, clock, approvals))
        notifier = SelfChatNotifier(conn, bus, settings.policy, clock, lambda: ingest.me)
        _attach_printers(bus)
        dashboard = _dashboard_server(settings)
        dashboard_task = asyncio.create_task(dashboard.serve())
        print(f"FLINS running. {await modes.status_text()}")
        print(f"Dashboard: http://127.0.0.1:{settings.app.network.dashboard_port}")
        if ai is None:
            missing = build_router(settings)[0].missing_roles()
            print(
                f"No AI provider with a key for: {', '.join(missing)}. FLINS decides without AI "
                "(no triage, no drafts). See `flins llm status`."
            )
        print("Type commands in your WhatsApp 'Message yourself' chat, or use `flins mode` here.")
        print(
            "Dry run: nothing is sent, even when you approve."
            if settings.policy.dry_run
            else (
                "SENDING IS ON: drafts you approve are sent"
                + (
                    ", and holding replies go out automatically in busy modes."
                    if settings.policy.auto_replies == "holding_only"
                    else ". Automatic replies are off."
                )
            )
        )
        print("Ctrl+C to stop.")

        async def tick_forever() -> None:
            assert modes is not None
            while True:
                try:
                    await pipeline.flush_due()
                    await approvals.expire_due(clock.now())
                    await outbox.run_due()
                    result = await modes.tick()
                    if result is not None:
                        print(f"   FLINS: {result.readback}")
                        await notifier.note(result.readback)  # e.g. the session summary
                except Exception as exc:  # keep ticking; one bad burst must not stop expiry
                    print(f"-- tick error: {type(exc).__name__}: {exc}")
                await asyncio.sleep(1)

        ticker = asyncio.create_task(tick_forever())
    else:
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
        if ticker is not None:
            ticker.cancel()
        if dashboard is not None:
            dashboard.should_exit = True
            if dashboard_task is not None:
                await asyncio.gather(dashboard_task, return_exceptions=True)
        if spawned:
            await adapter.request_sidecar_shutdown()
        await adapter.close()
        if spawned:
            await sidecar.stop()
        if modes is not None:
            await modes.close()
        await conn.close()
        print(
            f"stopped. {counts['in']} incoming, {counts['out']} outgoing, "
            f"{counts['contacts']} new contacts stored."
        )
    return code


def _dashboard_server(settings: Settings) -> uvicorn.Server:
    port = settings.app.network.dashboard_port
    app = create_app(settings.db_path, settings.policy, port=port)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    return uvicorn.Server(config)


def cmd_dashboard(root: Path | None) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    print(f"Dashboard: http://127.0.0.1:{settings.app.network.dashboard_port}  (Ctrl+C to stop)")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_dashboard_server(settings).serve())
    return 0


async def _eval(settings: Settings, live: bool, only: str | None, provider: str | None) -> int:
    cases = load_cases(settings.root / "tests" / "eval" / "whatsapp_cases.jsonl")
    if only:
        cases = [c for c in cases if only in c["id"]]
    factory = None
    if live or provider:
        router, _ = build_router(settings, provider)
        missing = router.missing_roles()
        if missing:
            who = f"provider {provider!r}" if provider else "the configured providers"
            print(
                f"`--live`: no key/route for {', '.join(missing)} with {who}. "
                "See `flins llm status`."
            )
            return 1
        # Wrong/leaky recorded answers only make sense with recorded LLM output. Live runs
        # use the real models on the 'good' situations.
        cases = [c for c in cases if c.get("variant") == "good"]
        chain = router.describe()
        print(f"Live eval: {len(cases)} situations, up to {len(cases) * 3} AI calls.")
        for role, routes_ in chain.items():
            print(f"  {role}: {' -> '.join(routes_)}")

        def factory(conn: Any, clock: Any) -> Any:
            return make_brain(settings, conn, clock, only=provider)

    report = await run_eval(
        cases,
        settings.policy,
        settings.data_dir / "eval",
        settings.prompts_dir,
        brain_factory=factory,
    )
    print(report.summary(show_usage=factory is not None))
    return 1 if report.unsafe else 0


def cmd_eval(root: Path | None, live: bool, only: str | None, provider: str | None) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    return asyncio.run(_eval(settings, live, only, provider))


async def _llm(settings: Settings, action: str, provider: str | None) -> int:
    if action == "status":
        print("\n".join(status_lines(settings)))
        return 0
    if action == "models":
        if not provider:
            print("usage: flins llm models <provider>   e.g. groq, gemini, nvidia, anthropic")
            return 2
        try:
            for model in await list_models(settings, provider):
                print(model)
        except LLMError as exc:
            print(f"{provider}: {exc}")
            return 1
        return 0
    conn = await connect(settings.db_path)
    await migrate(conn)
    try:
        results = await test_routes(settings, conn, SystemClock(), provider)
    finally:
        await conn.close()
    if not results:
        print("Nothing to test: no provider with a key. See `flins llm status`.")
        return 1
    for r in results:
        print(f"  [{'ok' if r.ok else 'FAIL':>4}] {r.route:<50} {r.ms:>5} ms  {r.detail}")
    return 0 if all(r.ok for r in results) else 1


def cmd_llm(root: Path | None, action: str, provider: str | None) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    return asyncio.run(_llm(settings, action, provider))


def cmd_live(root: Path | None, spawn: bool, *, brain: bool) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    try:
        return asyncio.run(_run_live(settings, spawn, brain=brain))
    except KeyboardInterrupt:
        return 0


async def _mode_command(settings: Settings, text: str) -> int:
    conn = await connect(settings.db_path)
    await migrate(conn)
    await conn.close()
    clock = SystemClock()
    modes = await ModeManager.open(settings.db_path, EventBus(), settings.policy, clock)
    try:
        if not text.strip():
            print(await modes.status_text())
            return 0
        reply = await CommandRouter(modes, settings.policy, clock).handle_text(text, source="cli")
    finally:
        await modes.close()
    if reply is None:
        print(
            f"Not a command FLINS knows: {text!r}. Try 'working mood till 6pm', 'status', "
            "'stop', 'resume', 'I'm done'."
        )
        return 2
    print(reply)
    return 0


def cmd_mode(root: Path | None, text: str) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    return asyncio.run(_mode_command(settings, text))


def _stub(name: str) -> int:
    print(f"`flins {name}` is not built yet. It arrives in milestone {STUBS[name]}.")
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="flins", description="FLINS personal assistant")
    p.add_argument("--version", action="version", version=f"flins {__version__}")
    p.add_argument("--root", type=Path, default=None, help="project root (default: auto-detect)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check config, database and tools")
    run = sub.add_parser("run", help="start FLINS: WhatsApp + modes + decisions (shadow mode)")
    run.add_argument("--no-spawn", action="store_true", help="don't start the sidecar")
    sub.add_parser("stop", help="kill switch: pause FLINS until you say 'resume'")
    sub.add_parser("resume", help="undo `flins stop`: back to the previous mode if it hasn't ended")
    mode = sub.add_parser("mode", help="show or change mode, e.g. flins mode working mood till 6pm")
    mode.add_argument("args", nargs="*")
    wa = sub.add_parser("wa", help="WhatsApp sidecar commands (M1)")
    wa_sub = wa.add_subparsers(dest="wa_command", required=True)
    pair = wa_sub.add_parser("pair", help="pair FLINS as a linked device (shows a QR)")
    pair.add_argument("--force", action="store_true", help="replace an existing pairing")
    tail = wa_sub.add_parser("tail", help="store and show incoming messages live (read-only)")
    tail.add_argument("--no-spawn", action="store_true", help="don't start the sidecar")
    sub.add_parser("dashboard", help="open the local dashboard without running FLINS")
    ev = sub.add_parser("eval", help="run the WhatsApp safety eval (recorded LLM answers)")
    ev.add_argument("--live", action="store_true", help="use the real AI models (costs money)")
    ev.add_argument("--provider", default=None, help="live, with only this provider (e.g. groq)")
    ev.add_argument("--only", default=None, help="only cases whose id contains this text")
    llm = sub.add_parser("llm", help="AI providers: status, test, models <provider>")
    llm.add_argument("action", nargs="?", default="status", choices=["status", "test", "models"])
    llm.add_argument("provider", nargs="?", default=None)
    skills = sub.add_parser("skills", help="manage skills (M7)")
    skills.add_argument("args", nargs="*")
    return p


def _utf8_console() -> None:
    """Windows consoles and pipes may default to cp1252, which can't print emoji from WhatsApp
    messages (or "≈"). Print UTF-8 and replace anything a terminal still can't show."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    root = find_root(args.root.resolve()) if args.root else None
    if args.command == "doctor":
        return cmd_doctor(root)
    if args.command == "wa":
        if args.wa_command == "pair":
            return cmd_wa_pair(root, args.force)
        return cmd_live(root, spawn=not args.no_spawn, brain=False)
    if args.command == "run":
        return cmd_live(root, spawn=not args.no_spawn, brain=True)
    if args.command == "mode":
        return cmd_mode(root, " ".join(args.args))
    if args.command in ("stop", "resume"):
        return cmd_mode(root, args.command)
    if args.command == "dashboard":
        return cmd_dashboard(root)
    if args.command == "eval":
        return cmd_eval(root, args.live, args.only, args.provider)
    if args.command == "llm":
        return cmd_llm(root, args.action, args.provider)
    return _stub(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
