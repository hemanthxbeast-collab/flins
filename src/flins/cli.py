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
from flins.assistant.core import Assistant
from flins.assistant.plans import render
from flins.assistant.task_commands import deliver_due
from flins.assistant.timeparse import extract
from flins.core.approvals import ApprovalService
from flins.core.audit import AuditLog
from flins.core.clock import SystemClock
from flins.core.commands import CommandRouter, SelfChatCommands
from flins.core.db import connect, migrate, table_names
from flins.core.events import Event, EventBus
from flins.core.ingest import ContactsResult, Ingestor, StoredMessage
from flins.core.intents import IntentParser
from flins.core.levels import Level, Mode
from flins.core.modes import ModeManager, effective_mode
from flins.core.outbox import OutboxWorker
from flins.core.pipeline import Pipeline
from flins.core.selfchat import SelfChatNotifier
from flins.evaluation import load_cases, run_eval
from flins.llm.admin import list_models, status_lines, test_routes
from flins.llm.gateway import LLMError
from flins.llm.prompts import PromptError, load_prompt
from flins.settings import Settings, find_root, load_settings
from flins.skills import admin as skills_admin
from flins.web.app import create_app
from flins.wiring import (
    build_router,
    conversation_store,
    make_assistant,
    make_brain,
    make_skills,
    memory_store,
    open_assistant,
    plan_store,
    task_store,
)

OK, WARN, FAIL = "ok", "warn", "FAIL"

# Commands that exist in the CLI but arrive in a later milestone.
STUBS: dict[str, str] = {}

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
    if settings.app.assistant.enabled:
        for role in ("chat", "reason"):
            routes_ = [f"{r.provider}/{r.model}" for r in router.routes_for(role)]
            _line(OK, f"assistant {role}: {' -> '.join(routes_) or 'nothing configured'}")
    else:
        _line(WARN, "assistant.enabled is false: FLINS runs commands but doesn't talk")
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

    wsl = wsl_networking_warning(Path.home() / ".wslconfig")
    if wsl:
        _line(WARN, wsl)

    print("result:", "problems found" if failed else "healthy")
    return 1 if failed else 0


def wsl_networking_warning(path: Path) -> str | None:
    """Hermes (M8) must not reach Windows' 127.0.0.1, where the dashboard runs. WSL's mirrored
    networking would let it."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.split("#")[0].partition("=")
        if key.strip().lower() == "networkingmode" and value.strip().lower() == "mirrored":
            return (
                f"{path}: networkingMode=mirrored lets WSL (Hermes) reach the dashboard. "
                "Use the default NAT mode (see docs/HERMES.md)"
            )
    return None


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


async def _run_live(settings: Settings, spawn: bool, *, brain: bool, voice: bool = False) -> int:
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
    backups_task: asyncio.Task[None] | None = None
    dashboard: uvicorn.Server | None = None
    dashboard_task: asyncio.Task[None] | None = None
    voice_task: asyncio.Task[None] | None = None
    hotkey = None
    if brain:
        clock = SystemClock()
        modes = await ModeManager.open(settings.db_path, bus, settings.policy, clock)
        ai = make_brain(settings, conn, clock)
        skills = make_skills(settings, conn)
        if skills is not None:
            await skills.sync_db()
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
            skills=skills,
        )
        approvals = ApprovalService(conn, settings.policy)
        outbox = OutboxWorker(conn, settings.policy, adapter, clock, bus)
        intents = (
            IntentParser(ai.gateway, load_prompt(settings.prompts_dir, "intent"))
            if ai is not None
            else None
        )
        router = CommandRouter(
            modes,
            settings.policy,
            clock,
            approvals,
            intents=intents,
            conn=conn,
            skills=skills,
            skill_requests=settings.skills_dir / "_incoming",
            conversational=True,
        )
        # Everything you say to FLINS, on any channel, goes through the assistant core.
        assistant = make_assistant(settings, conn, clock, modes, router, ai, approvals)
        SelfChatCommands(conn, bus, assistant)
        notifier = SelfChatNotifier(conn, bus, settings.policy, clock, lambda: ingest.me)
        voice_loop = None
        if voice or settings.app.voice.enabled:
            voice_loop, hotkey = await _start_voice(settings, assistant, bus, conn)
            if voice_loop is not None:
                spoken = voice_loop

                async def one_moment(source: str) -> None:
                    if source == "voice":
                        await spoken.say("One moment.")

                assistant.on_wait = one_moment
        _attach_printers(bus)
        dashboard = _dashboard_server(settings, assistant)
        dashboard_task = asyncio.create_task(dashboard.serve())
        print(f"FLINS running. {await modes.status_text()}")
        if skills is not None:
            on = [s.name for s in skills.all() if s.runnable]
            off = [s.name for s in skills.all() if s.status == "changed"]
            print(f"Skills on: {', '.join(on) or 'none'} (flins skills list)")
            if off:
                print(f"Skills changed since approval, not running: {', '.join(off)}")
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
                + {
                    "holding_only": ", and holding replies go out automatically in busy modes.",
                    "unlocked": ", holding replies go out automatically, and AI replies go out "
                    "unreviewed to contacts who earned it (flins contacts).",
                    "off": ". Automatic replies are off.",
                }[settings.policy.auto_replies]
            )
        )
        print("Ctrl+C to stop.")

        reminders = task_store(settings, conn)

        async def tick_forever() -> None:
            assert modes is not None
            while True:
                try:
                    await pipeline.flush_due()
                    await approvals.expire_due(clock.now())
                    await outbox.run_due()
                    # Reminders that are due: to your self-chat, and spoken with voice on.
                    state_now = effective_mode(modes.state, clock.now())
                    for line in await deliver_due(
                        reminders,
                        clock.now(),
                        paused=state_now == Mode.PAUSED,
                        focus=state_now == Mode.FOCUS,
                        note=notifier.note,
                        say=voice_loop.say if voice_loop is not None else None,
                    ):
                        print(f"   FLINS: {line}")
                    result = await modes.tick()
                    if result is not None:
                        print(f"   FLINS: {result.readback}")
                        await notifier.note(result.readback)  # e.g. the session summary
                        if voice_loop is not None and settings.app.voice.speak_alerts:
                            await voice_loop.say(result.readback)
                except Exception as exc:  # keep ticking; one bad burst must not stop expiry
                    print(f"-- tick error: {type(exc).__name__}: {exc}")
                await asyncio.sleep(1)

        ticker = asyncio.create_task(tick_forever())
        backups_task = asyncio.create_task(_daily_backups(settings))
        if voice_loop is not None and hotkey is not None:
            voice_task = asyncio.create_task(voice_loop.run(hotkey.presses))
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
        if backups_task is not None:
            backups_task.cancel()
        if voice_task is not None:
            voice_task.cancel()
        wake_parts = getattr(voice_loop, "wake_parts", None) if brain else None
        if wake_parts is not None:
            wake_parts[0].stop()
            wake_parts[1].cancel()
        if hotkey is not None:
            hotkey.stop()
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


def _dashboard_server(settings: Settings, assistant: Assistant | None = None) -> uvicorn.Server:
    port = settings.app.network.dashboard_port
    app = create_app(
        settings.db_path, settings.policy, port=port, settings=settings, assistant=assistant
    )
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


async def _start_voice(
    settings: Settings, router: Any, bus: EventBus, conn: Any = None
) -> tuple[Any, Any]:
    """Voice loop + hotkey, or (None, None) with a message if voice can't start."""
    from flins.voice.factory import VoiceUnavailable, make_loop
    from flins.voice.hotkey import Hotkey

    try:
        loop = make_loop(settings, router, conn=conn)
    except VoiceUnavailable as exc:
        print(f"-- voice off: {exc}")
        return None, None
    v = settings.app.voice
    model = loop._stt.model_name  # type: ignore[attr-defined]
    print(
        f"Voice: loading speech model '{model}' ({v.accuracy}, {v.language or 'auto'}); "
        "the first time it downloads..."
    )
    await asyncio.get_running_loop().run_in_executor(None, loop._stt.load)  # type: ignore[attr-defined]
    hotkey = Hotkey(v.hotkey)
    hotkey.start()
    print(f"Voice ready: press {v.hotkey.replace('<', '').replace('>', '').title()} and speak.")
    if v.wake_word:
        await _start_wake_word(settings, loop)

    if v.speak_alerts:

        async def on_decision(e: Event) -> None:
            if e.data.get("interrupt") or e.data.get("escalate"):
                who = e.data.get("contact_name") or "someone"
                await loop.say(f"Urgent message from {who}.")

        async def on_expiring(e: Event) -> None:
            await loop.say(str(e.data["text"]))

        bus.subscribe("decision.made", on_decision)
        bus.subscribe("mode.expiring", on_expiring)
    return loop, hotkey


async def _start_wake_word(settings: Settings, loop: Any) -> None:
    """Say "FLINS" instead of pressing the hotkey. Runs until FLINS stops."""
    from flins.voice.stt import WhisperSTT
    from flins.voice.wake import MicBursts, WakeWord

    v = settings.app.voice
    stt = WhisperSTT(v.wake_model, device=v.stt_device, language=v.language or "en", beam_size=1)
    print(f"Wake word: loading '{v.wake_model}' model...")
    await asyncio.get_running_loop().run_in_executor(None, stt.load)
    mic = MicBursts()
    mic.start()
    wake = WakeWord(mic, stt, loop)
    task = asyncio.create_task(wake.run())
    loop.wake_parts = (mic, task)  # stopped with the voice loop
    print('Wake word on: say "FLINS", then your command (or "FLINS, status" in one go).')


def _piper_download(settings: Settings, name: str) -> int:
    """Fetch a Piper voice (about 60 MB) from Piper's official voice collection."""
    try:
        from piper.download_voices import download_voice, list_voices
    except ImportError:
        print("Piper isn't installed. Run: uv sync --extra voice")
        return 1
    if name.strip() == "--list":
        list_voices()
        return 0
    voice = name.strip() or settings.app.voice.piper_voice
    folder = settings.data_dir / "voices"
    folder.mkdir(parents=True, exist_ok=True)
    print(f"Downloading Piper voice {voice} into {folder} ...")
    try:
        download_voice(voice, folder)
    except Exception as exc:
        print(f"Download failed: {exc}")
        return 1
    print(
        f"Done. To use it, set in config/flins.yaml under voice: tts: piper and "
        f"piper_voice: {voice}. Try it: uv run flins voice say hello"
    )
    return 0


async def _voice_command(settings: Settings, action: str, text: str) -> int:
    from flins.voice.factory import VoiceUnavailable, check_installed, make_stt, make_tts
    from flins.voice.tts import speakable

    try:
        check_installed()
    except VoiceUnavailable as exc:
        print(exc)
        return 1
    if action == "download":
        return _piper_download(settings, text)
    try:
        tts = make_tts(settings)
    except VoiceUnavailable as exc:
        print(exc)
        return 1
    if action == "voices":
        from flins.voice.tts import SapiTTS

        for name in SapiTTS().voices():
            print(name)
        return 0
    if action == "say":
        await tts.say(speakable(text or "Hello, I'm FLINS, Hemanth's assistant."))
        return 0
    # test: one round of listen -> transcribe -> speak back, with timings
    import time

    from flins.voice.audio import MicRecorder, beep

    stt = make_stt(settings)
    v = settings.app.voice
    print(f"Loading speech model '{stt.model_name}' ({v.accuracy}); downloads the first time...")
    started = time.monotonic()
    await asyncio.get_running_loop().run_in_executor(None, stt.load)
    print(f"Model ready in {time.monotonic() - started:.1f} s. Speak after the beep.")
    beep()
    rec = await MicRecorder(max_s=v.max_record_s, silence_s=v.silence_s).record()
    if not rec.heard_speech:
        print("I didn't hear anything. Check the microphone in Windows sound settings.")
        return 1
    t = await stt.transcribe(rec.audio)
    reply = f"I heard: {t.text}" if t.text else "I couldn't make out any words."
    latency = time.monotonic() - rec.speech_end
    print(f"Heard: {t.text!r} (language {t.language}, transcribed in {t.seconds:.2f} s)")
    print(f"From the end of your speech to FLINS answering: {latency:.2f} s (target < 2.5 s)")
    await tts.say(reply)
    return 0


def cmd_voice(root: Path | None, action: str, text: str) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    return asyncio.run(_voice_command(settings, action, text))


def cmd_live(root: Path | None, spawn: bool, *, brain: bool, voice: bool = False) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    try:
        return asyncio.run(_run_live(settings, spawn, brain=brain, voice=voice))
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


async def _ask(settings: Settings, text: str, interactive: bool) -> int:
    """One message (`flins ask`), or a conversation in the terminal (`flins chat`)."""
    name = settings.app.assistant.name
    async with open_assistant(settings) as rt:

        async def say(line: str) -> None:
            reply = await rt.assistant.handle(line, source="cli")
            print(f"{name}: {reply.text}" if interactive else reply.text)

        if not interactive:
            await say(text)
            # A free-form command asks "yes or no?". Answer here, while this process is alive.
            for _ in range(4):
                if not (rt.router.awaiting("cli") and sys.stdin.isatty()):
                    break
                try:
                    await say(input("> "))
                except EOFError:
                    break
            return 0
        print(f"Talking to {name}. Type 'exit' to leave.")
        while True:
            try:
                line = (await asyncio.to_thread(input, "you: ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if line.lower() in ("exit", "quit", "bye"):
                return 0
            if line:
                await say(line)


async def _chat_history(settings: Settings, clear: bool) -> int:
    conn = await connect(settings.db_path)
    await migrate(conn)
    try:
        store = conversation_store(settings, conn)
        if clear:
            n = await store.clear()
            await AuditLog(conn).record("user", "assistant.conversation_cleared", {"turns": n})
            print(f"Deleted {n} conversation turns.")
            return 0
        tz = datetime.now().astimezone().tzinfo
        name = settings.app.assistant.name
        turns = await store.history(40)
        for t in turns:
            who = "you" if t.role == "user" else name
            print(f"[{t.ts.astimezone(tz):%d %b %H:%M}] {who} ({t.source}): {t.text}")
        if not turns:
            print('No conversation yet. Try: uv run flins ask "what can you do?"')
    finally:
        await conn.close()
    return 0


async def _memory(settings: Settings, action: str, rest: list[str]) -> int:
    """`flins memory`: see, add, correct and delete what FLINS remembers."""
    conn = await connect(settings.db_path)
    await migrate(conn)
    clock = SystemClock()
    try:
        mem = memory_store(settings, conn, clock)
        now = clock.now()
        if action in ("on", "off"):
            await mem.set_enabled(action == "on")
            print(f"Memory is {action}.")
            return 0
        if action == "list":
            records = await mem.all(now)
            state = "on" if await mem.enabled() else "OFF (kept, but not used or added to)"
            print(f"Memory is {state}. {len(records)} stored.")
            for r in records:
                tag = " [sensitive]" if r.privacy == "sensitive" else ""
                print(f"  #{r.id} [{r.kind}, {r.importance:.2f}]{tag} {r.content}")
                print(f"       {r.where(mem.tz)}")
            return 0
        if action == "add":
            result = await mem.save(" ".join(rest), source="cli", channel=None, now=now)
        elif action in ("forget", "edit"):
            if not rest or not rest[0].lstrip("#").isdigit():
                print(f"Which one? flins memory {action} <number>  (see: flins memory list)")
                return 2
            memory_id = int(rest[0].lstrip("#"))
            if action == "forget":
                n = await mem.forget([memory_id], via="cli")
                print(f"Forgotten #{memory_id}." if n else f"There's no memory #{memory_id}.")
                return 0 if n else 1
            result = await mem.edit(memory_id, " ".join(rest[1:]), now)
        else:  # clear
            n = await mem.count()
            if not n:
                print("There are no memories.")
                return 0
            if not sys.stdin.isatty():
                print("Refusing: deleting every memory needs you at an interactive terminal.")
                return 1
            if input(f"Delete all {n} memories? Type 'delete' to confirm: ").strip() != "delete":
                print("Nothing deleted.")
                return 1
            print(f"Deleted {await mem.forget_all(via='cli')} memories.")
            return 0
        if result.status == "refused":
            print(f"Not saved: {result.reason}.")
            return 1
        assert result.record is not None
        word = "Already had" if result.status == "duplicate" else "Saved"
        print(f"{word} #{result.record.id}: {result.record.content}")
        return 0
    finally:
        await conn.close()


async def _tasks(settings: Settings, action: str, rest: list[str]) -> int:
    """`flins tasks`: your task list and reminders."""
    conn = await connect(settings.db_path)
    await migrate(conn)
    now = SystemClock().now()
    try:
        tasks = task_store(settings, conn)
        if action == "list":
            items = await tasks.open()
            print(f"{len(items)} open task{'s' if len(items) != 1 else ''}.")
            for t in items:
                print(f"  #{t.id} {t.line(now, tasks.tz)}")
            done = await tasks.recently_done(5)
            if done:
                print("Recently done:")
                for t in done:
                    print(f"  #{t.id} {t.title}")
            return 0
        if action == "add":
            when = extract(" ".join(rest), now, tasks.tz)
            title = when.title.strip()
            if not title:
                print('Add what? flins tasks add "call the bank tomorrow at 7"')
                return 2
            t = await tasks.add(
                title, source="cli", channel="cli", now=now, at=when.at, repeat=when.repeat
            )
            print(f"Added #{t.id}: {t.line(now, tasks.tz)}")
            return 0
        if not rest or not rest[0].lstrip("#").isdigit():
            print(f"Which one? flins tasks {action} <number>  (see: flins tasks list)")
            return 2
        task = await tasks.get(int(rest[0].lstrip("#")))
        if task is None or task.status != "open":
            print(f"There's no open task #{rest[0].lstrip('#')}.")
            return 1
        if action == "done":
            after = await tasks.complete(task, now)
            again = f" Next: {after.line(now, tasks.tz)}" if after.status == "open" else ""
            print(f"Done #{task.id}: {task.title}.{again}")
        else:
            await tasks.cancel(task, now, via="cli")
            print(f"Deleted #{task.id}: {task.title}.")
        return 0
    finally:
        await conn.close()


async def _plans(settings: Settings, action: str, rest: list[str]) -> int:
    """`flins plans`: plans in progress. They are made by talking: "make a plan to …"."""
    conn = await connect(settings.db_path)
    await migrate(conn)
    now = SystemClock().now()
    try:
        plans = plan_store(settings, conn)
        if action == "list":
            items = await plans.recent()
            if not items:
                print('No plans yet. Make one by saying or typing: "make a plan to …"')
            for p in items:
                state = "" if p.status == "open" else f" [{p.status}]"
                print(f"  #{p.id} {p.title} ({p.progress()}){state}")
            return 0
        if not rest or not rest[0].lstrip("#").isdigit():
            print(f"Which one? flins plans {action} <number>  (see: flins plans)")
            return 2
        plan = await plans.get(int(rest[0].lstrip("#")))
        if plan is None:
            print(f"There's no plan #{rest[0].lstrip('#')}.")
            return 1
        if action == "show":
            print(render(plan))
            return 0
        if plan.status != "open":
            print(f"Plan #{plan.id} is already {plan.status}.")
            return 1
        n = await plans.cancel(plan, now, via="cli")
        print(f"Deleted plan #{plan.id} '{plan.title}' and its {n} open steps.")
        return 0
    finally:
        await conn.close()


def cmd_plans(root: Path | None, action: str, rest: list[str]) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    return asyncio.run(_plans(settings, action, rest))


def cmd_tasks(root: Path | None, action: str, rest: list[str]) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    return asyncio.run(_tasks(settings, action, rest))


def cmd_memory(root: Path | None, action: str, rest: list[str]) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    return asyncio.run(_memory(settings, action, rest))


async def _local(settings: Settings, action: str) -> int:
    """`flins local`: the models running on this PC (Ollama)."""
    from flins.llm.local import LocalModels
    from flins.wiring import provider_specs, routes

    specs, _ = provider_specs(settings)
    local = [s for s in specs.values() if s.local and s.base_url]
    if not local:
        print("No local provider is configured.")
        return 1
    role_routes, _ = routes(settings)
    for spec in local:
        assert spec.base_url is not None
        models = LocalModels(spec.base_url)
        version = await models.version()
        if version is None:
            print(f"{spec.name}: not running. Start Ollama (Start menu), then try again.")
            return 1
        if action == "unload":
            gone = await models.unload_all()
            print(f"Unloaded: {', '.join(gone) or 'nothing was loaded'}.")
            continue
        print(f"{spec.name} {version} at {spec.base_url}")
        installed = await models.installed()
        loaded = {m.name: m for m in await models.loaded()}
        for m in sorted(installed, key=lambda m: m.name):
            state = ""
            if m.name in loaded:
                on = loaded[m.name]
                where = (
                    "all in video memory"
                    if on.vram_gb >= on.size_gb * 0.98
                    else (f"{on.vram_gb:.1f} GB in video memory, the rest in RAM (slower)")
                )
                state = f"   LOADED: {on.size_gb:.1f} GB, {where}"
            print(f"  {m.name:<28} {m.size_gb:>5.1f} GB on disk{state}")
        if not installed:
            print("  no models downloaded yet (e.g. ollama pull qwen3.5:4b)")
        names = {m.name for m in installed}
        for role, chain in sorted(role_routes.items()):
            for r in chain:
                if r.provider == spec.name:
                    ok = "" if r.model in names else "   NOT DOWNLOADED"
                    print(f"  {role:<8} uses {r.model}{ok}")
    return 0


def cmd_local(root: Path | None, action: str) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    return asyncio.run(_local(settings, action))


def cmd_ask(root: Path | None, text: str, *, interactive: bool) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    if not interactive and not text.strip():
        print('Ask something, e.g. uv run flins ask "what can you do?"')
        return 2
    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(_ask(settings, text, interactive))
    return 0


def cmd_chat_history(root: Path | None, clear: bool) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    return asyncio.run(_chat_history(settings, clear))


def cmd_mode(root: Path | None, text: str) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    return asyncio.run(_mode_command(settings, text))


def _ask_typed(what: list[str], word: str) -> bool:
    """Enabling a skill needs a person at the keyboard: type its name to approve."""
    print("\n".join(what))
    if not sys.stdin.isatty():
        print("Refusing: enabling a skill needs you at an interactive terminal.")
        return False
    try:
        answer = input(f"Type {word!r} to approve, anything else to cancel: ")
    except EOFError:
        return False
    return answer.strip() == word


async def _skills(settings: Settings, action: str, name: str | None) -> int:
    if action == "list":
        print("\n".join(skills_admin.list_lines(settings)))
        return 0
    if not name:
        print(f"Which skill? e.g. flins skills {action} holding_reply")
        return 2
    if action == "validate":
        from flins.skills.lock import LOCK_FILE, SkillLock
        from flins.skills.validator import validate

        d = settings.skills_dir / name
        if not (d / "flins.skill.yaml").is_file():
            print(f"No skill called {name!r} in {settings.skills_dir}.")
            return 1
        report = validate(d, SkillLock(settings.skills_dir / LOCK_FILE))
        print("\n".join(skills_admin.report_lines(report)))
        return 0 if report.ok else 1
    if action == "test":
        ok, lines = await skills_admin.test_skill(settings, name)
        print("\n".join(lines))
        print("PASSED" if ok else "FAILED")
        return 0 if ok else 1
    conn = await connect(settings.db_path)
    await migrate(conn)
    try:
        if action == "enable":
            ok, lines = await skills_admin.enable_skill(settings, conn, name, _ask_typed)
        else:
            ok, lines = await skills_admin.disable_skill(settings, conn, name)
    finally:
        await conn.close()
    print("\n".join(lines))
    return 0 if ok else 1


def cmd_skills(root: Path | None, action: str, name: str | None) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    return asyncio.run(_skills(settings, action, name))


def cmd_autostart(root: Path | None, action: str, voice: bool) -> int:
    from flins import autostart

    if platform.system() != "Windows":
        print("Autostart is for Windows (Task Scheduler).")
        return 1
    if action == "status":
        print(f"Autostart: {autostart.status().text()}")
        return 0
    if action == "off":
        print(autostart.disable())
        return 0
    try:
        project = root or find_root()
    except FileNotFoundError as exc:
        print(exc)
        return 1
    print(autostart.enable(project, voice))
    print("It runs: " + autostart.launch_line(project, voice))
    return 0


PASSPHRASE_HINT = (
    "Set FLINS_BACKUP_PASSPHRASE in .env (at least 12 characters, e.g. four random words) and "
    "keep a copy in your password manager: without it a backup can't be opened."
)


def cmd_backup(root: Path | None, action: str, file: Path | None, to: Path | None) -> int:
    from flins import backup

    settings = _load(root)
    if settings is None:
        return 1
    folder = to.resolve() if to and action != "restore" else settings.backup_dir
    if action == "list":
        files = sorted(folder.glob(f"flins-*{backup.SUFFIX}")) if folder.is_dir() else []
        for f in files:
            print(f"  {f.name}  {f.stat().st_size / 1024:.0f} KB")
        print(f"{len(files)} backup(s) in {folder}")
        return 0
    passphrase = settings.env_value("FLINS_BACKUP_PASSPHRASE")
    if not passphrase:
        print(PASSPHRASE_HINT)
        return 1
    try:
        if action == "restore":
            if file is None:
                print("Which file? flins backup restore <file> [--to <new folder>]")
                return 2
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = (to or settings.data_dir / f"restore-{stamp}").resolve()
            names = backup.restore_backup(file, passphrase, dest)
            print(f"Restored {len(names)} files into {dest} (your live files weren't touched).")
            print("To use it: stop FLINS, then copy flins.db into data/ and the config files back.")
            return 0
        result = backup.create_backup(
            settings.root,
            settings.db_path,
            folder,
            passphrase,
            datetime.now(),
            keep=settings.app.backup.keep,
        )
    except backup.BackupError as exc:
        print(f"Backup: {exc}")
        return 1
    print(
        f"Saved {result.path} ({result.size / 1024:.0f} KB, encrypted): {', '.join(result.files)}"
    )
    if result.removed:
        print(f"Removed {len(result.removed)} older backup(s), keeping {settings.app.backup.keep}.")
    if folder.is_relative_to(settings.data_dir):
        print("Tip: backups on the same disk don't survive a disk failure. Use --to or backup.dir.")
    return 0


async def _daily_backups(settings: Settings) -> None:
    """While FLINS runs: one encrypted backup a day, if you set a passphrase."""
    from flins import backup

    if not settings.app.backup.daily:
        return
    while True:
        passphrase = settings.env_value("FLINS_BACKUP_PASSPHRASE")
        newest = backup.latest(settings.backup_dir)
        due = newest is None or (datetime.now().timestamp() - newest.stat().st_mtime > 24 * 3600)
        if passphrase and due:
            try:
                result = await asyncio.to_thread(
                    backup.create_backup,
                    settings.root,
                    settings.db_path,
                    settings.backup_dir,
                    passphrase,
                    datetime.now(),
                    keep=settings.app.backup.keep,
                )
                print(f"-- daily backup saved: {result.path.name}")
            except Exception as exc:  # never let a backup problem stop FLINS
                print(f"-- daily backup failed: {exc}")
        await asyncio.sleep(3600)


async def _contacts(settings: Settings, action: str, rest: list[str]) -> int:
    from flins.core.autonomy import auto_stats, set_optin
    from flins.core.intents import find_contacts

    policy = settings.policy
    conn = await connect(settings.db_path)
    await migrate(conn)
    try:
        if action == "list":
            async with conn.execute(
                "SELECT id, COALESCE(display_name, '~' || push_name, wa_jid) AS name, tier, "
                "auto_optin FROM contacts WHERE auto_optin = 1 OR tier IN ('inner', 'trusted') "
                "ORDER BY auto_optin DESC, tier, name"
            ) as cur:
                rows = list(await cur.fetchall())
            print(f"Unreviewed AI replies: auto_replies = {policy.auto_replies}")
            if policy.auto_replies != "unlocked":
                print("  (set auto_replies: unlocked in config/policy.yaml to use earned replies)")
            for r in rows:
                stats = await auto_stats(conn, r["id"])
                print(f"  {r['name']:<28} {r['tier']:<8} {stats.describe(policy)}")
            if not rows:
                print(
                    "  No inner/trusted contacts yet. Set tiers on the dashboard's Contacts page."
                )
            return 0
        if len(rest) < 2 or rest[-1].lower() not in ("on", "off"):
            print('Usage: flins contacts auto "<name>" on|off')
            return 2
        on = rest[-1].lower() == "on"
        spoken = " ".join(rest[:-1])
        matches = await find_contacts(conn, spoken)
        if not matches:
            print(f"No contact matches {spoken!r}.")
            return 1
        if len(matches) > 1:
            print("Which one? " + ", ".join(m.name for m in matches[:6]))
            return 1
        async with conn.execute(
            "SELECT id, tier FROM contacts WHERE wa_jid = ? OR wa_lid = ?",
            (matches[0].jid, matches[0].jid),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        if on and row["tier"] in ("unknown", "blocked"):
            print(f"{matches[0].name} is {row['tier']}: give them a tier first.")
            return 1
        await set_optin(conn, row["id"], on, "cli")
        stats = await auto_stats(conn, row["id"])
        print(f"{matches[0].name}: {stats.describe(policy)}")
        tier_pol = policy.tiers.get(row["tier"])
        if on and (tier_pol is None or tier_pol.ceiling < Level.AUTO):
            print(
                f"Note: tier {row['tier']} is capped below AUTO in policy.yaml, so they'll keep "
                "getting holding replies or drafts until you move them to a tier that allows AUTO."
            )
        return 0
    finally:
        await conn.close()


def cmd_contacts(root: Path | None, action: str, rest: list[str]) -> int:
    settings = _load(root)
    if settings is None:
        return 1
    return asyncio.run(_contacts(settings, action, rest))


def cmd_skilldev(root: Path | None, incoming: Path | None) -> int:
    """stdout belongs to the MCP protocol: never print here. It reads no personal config."""
    from flins.skilldev.server import serve_stdio

    try:
        project = root or find_root()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    asyncio.run(serve_stdio(project, incoming.resolve() if incoming else None))
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
    run = sub.add_parser("run", help="start FLINS: WhatsApp + modes + decisions (shadow mode)")
    run.add_argument("--no-spawn", action="store_true", help="don't start the sidecar")
    run.add_argument("--voice", action="store_true", help="also listen on the push-to-talk hotkey")
    sub.add_parser("stop", help="kill switch: pause FLINS until you say 'resume'")
    sub.add_parser("resume", help="undo `flins stop`: back to the previous mode if it hasn't ended")
    mode = sub.add_parser("mode", help="show or change mode, e.g. flins mode working mood till 6pm")
    mode.add_argument("args", nargs="*")
    ask = sub.add_parser("ask", help='say one thing to FLINS, e.g. flins ask "what can you do?"')
    ask.add_argument("text", nargs="*")
    chat = sub.add_parser("chat", help="talk to FLINS in this terminal (or --history / --clear)")
    chat.add_argument("--history", action="store_true", help="show recent conversation turns")
    chat.add_argument("--clear", action="store_true", help="delete all stored conversation")
    loc = sub.add_parser("local", help="local AI models on this PC (Ollama): status, unload")
    loc.add_argument("action", nargs="?", default="status", choices=["status", "unload"])
    mem = sub.add_parser(
        "memory", help="what FLINS remembers: list, add <text>, forget <n>, edit <n> <text>, ..."
    )
    mem.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "add", "forget", "edit", "clear", "on", "off"],
    )
    mem.add_argument("rest", nargs="*")
    tsk = sub.add_parser(
        "tasks", help='tasks and reminders: list, add "<text with a time>", done <n>, delete <n>'
    )
    tsk.add_argument("action", nargs="?", default="list", choices=["list", "add", "done", "delete"])
    tsk.add_argument("rest", nargs="*")
    pln = sub.add_parser("plans", help="plans in progress: list, show <n>, delete <n>")
    pln.add_argument("action", nargs="?", default="list", choices=["list", "show", "delete"])
    pln.add_argument("rest", nargs="*")
    wa = sub.add_parser("wa", help="WhatsApp sidecar commands (M1)")
    wa_sub = wa.add_subparsers(dest="wa_command", required=True)
    pair = wa_sub.add_parser("pair", help="pair FLINS as a linked device (shows a QR)")
    pair.add_argument("--force", action="store_true", help="replace an existing pairing")
    tail = wa_sub.add_parser("tail", help="store and show incoming messages live (read-only)")
    tail.add_argument("--no-spawn", action="store_true", help="don't start the sidecar")
    sub.add_parser("dashboard", help="open the local dashboard without running FLINS")
    vo = sub.add_parser(
        "voice", help="voice: test, say <text>, voices, download [piper voice | --list]"
    )
    vo.add_argument("action", choices=["test", "say", "voices", "download"])
    vo.add_argument("text", nargs="*")
    vo.add_argument("--list", action="store_true", help="with download: list Piper voices")
    ev = sub.add_parser("eval", help="run the WhatsApp safety eval (recorded LLM answers)")
    ev.add_argument("--live", action="store_true", help="use the real AI models (costs money)")
    ev.add_argument("--provider", default=None, help="live, with only this provider (e.g. groq)")
    ev.add_argument("--only", default=None, help="only cases whose id contains this text")
    llm = sub.add_parser("llm", help="AI providers: status, test, models <provider>")
    llm.add_argument("action", nargs="?", default="status", choices=["status", "test", "models"])
    llm.add_argument("provider", nargs="?", default=None)
    sk = sub.add_parser("skills", help="skills: list, validate, test, enable, disable <name>")
    sk.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "validate", "test", "enable", "disable"],
    )
    sk.add_argument("name", nargs="?", default=None)
    au = sub.add_parser(
        "autostart", help="start FLINS when you log in: on [--voice] | off | status"
    )
    au.add_argument("action", nargs="?", default="status", choices=["on", "off", "status"])
    au.add_argument("--voice", action="store_true", help="start with push-to-talk/wake word")
    bk = sub.add_parser("backup", help="encrypted backup: create (default), list, restore <file>")
    bk.add_argument("action", nargs="?", default="create", choices=["create", "list", "restore"])
    bk.add_argument("file", nargs="?", type=Path, default=None)
    bk.add_argument("--to", type=Path, default=None, help="backup folder, or restore folder")
    ct = sub.add_parser(
        "contacts", help="earned auto replies: show progress, or: auto <name> on|off"
    )
    ct.add_argument("action", nargs="?", default="list", choices=["list", "auto"])
    ct.add_argument("rest", nargs="*", help="for auto: <name...> on|off")
    dev = sub.add_parser(
        "skilldev", help="MCP server (stdio) for Hermes to build skills in skills/_incoming"
    )
    dev.add_argument(
        "--incoming",
        type=Path,
        default=None,
        help="the shared _incoming folder (default: skills/_incoming)",
    )
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
        return cmd_live(root, spawn=not args.no_spawn, brain=True, voice=args.voice)
    if args.command == "local":
        return cmd_local(root, args.action)
    if args.command == "memory":
        return cmd_memory(root, args.action, args.rest)
    if args.command == "tasks":
        return cmd_tasks(root, args.action, args.rest)
    if args.command == "plans":
        return cmd_plans(root, args.action, args.rest)
    if args.command == "ask":
        return cmd_ask(root, " ".join(args.text), interactive=False)
    if args.command == "chat":
        if args.history or args.clear:
            return cmd_chat_history(root, args.clear)
        return cmd_ask(root, "", interactive=True)
    if args.command == "mode":
        return cmd_mode(root, " ".join(args.args))
    if args.command in ("stop", "resume"):
        return cmd_mode(root, args.command)
    if args.command == "voice":
        text = "--list" if args.list else " ".join(args.text)
        return cmd_voice(root, args.action, text)
    if args.command == "dashboard":
        return cmd_dashboard(root)
    if args.command == "eval":
        return cmd_eval(root, args.live, args.only, args.provider)
    if args.command == "llm":
        return cmd_llm(root, args.action, args.provider)
    if args.command == "skills":
        return cmd_skills(root, args.action, args.name)
    if args.command == "autostart":
        return cmd_autostart(root, args.action, args.voice)
    if args.command == "backup":
        return cmd_backup(root, args.action, args.file, args.to)
    if args.command == "contacts":
        return cmd_contacts(root, args.action, args.rest)
    if args.command == "skilldev":
        return cmd_skilldev(root, args.incoming)
    return _stub(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
