"""Build the runtime pieces from settings (for `flins run`, `flins eval --live`, `flins llm`)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import timedelta
from zoneinfo import ZoneInfo

import aiosqlite

from flins.assistant.chat import ChatEngine, Summariser
from flins.assistant.conversation import ConversationStore
from flins.assistant.core import Assistant, Commands
from flins.assistant.memory import CombinedMemory, ProfileMemory, SqlMemory
from flins.assistant.memory_commands import MemoryCommands
from flins.assistant.task_commands import TaskCommands
from flins.assistant.tasks import TaskStore
from flins.core.approvals import ApprovalService
from flins.core.audit import AuditLog
from flins.core.clock import Clock, SystemClock
from flins.core.commands import CommandRouter
from flins.core.db import connect, migrate
from flins.core.events import EventBus
from flins.core.intents import IntentParser
from flins.core.modes import ModeManager
from flins.core.pipeline import Brain
from flins.llm.gateway import Backend, LLMGateway
from flins.llm.prompts import load_prompt
from flins.llm.providers import (
    BUILTIN_PROVIDERS,
    DEFAULT_ROLES,
    OPTIONAL_ROLES,
    ROLES,
    ConfiguredRouter,
    ProviderSpec,
    Route,
    make_backend,
)
from flins.llm.services import Drafter, RedFlagChecker, Triager
from flins.settings import Settings
from flins.skills.engine import SkillEngine


@dataclass
class ProviderStatus:
    spec: ProviderSpec
    has_key: bool
    used_by: list[str] = field(default_factory=list)


def provider_specs(settings: Settings) -> tuple[dict[str, ProviderSpec], list[str]]:
    """Built-in providers merged with flins.yaml overrides and custom providers."""
    specs = dict(BUILTIN_PROVIDERS)
    problems: list[str] = []
    for name, cfg in settings.app.llm.providers.items():
        base = specs.get(name)
        if base is None:
            if not cfg.base_url:
                problems.append(f"provider {name!r}: unknown, and no base_url given")
                continue
            base = ProviderSpec(name, "openai_compat", cfg.base_url, cfg.key_env)
        specs[name] = replace(
            base,
            base_url=cfg.base_url or base.base_url,
            key_env=cfg.key_env or base.key_env,
            json_mode=cfg.json_mode or base.json_mode,
            reasoning_effort=base.reasoning_effort
            if cfg.reasoning_effort is None
            else cfg.reasoning_effort,
            min_max_tokens=cfg.min_max_tokens or base.min_max_tokens,
            local=base.local if cfg.local is None else cfg.local,
            timeout_s=cfg.timeout_s or base.timeout_s,
        )
    return specs, problems


def routes(settings: Settings) -> tuple[dict[str, list[Route]], list[str]]:
    specs, problems = provider_specs(settings)
    configured = settings.app.llm.roles
    out: dict[str, list[Route]] = {}
    for role in ROLES:
        entries = configured.get(role)  # type: ignore[call-overload]
        raw = [e.model_dump() for e in entries] if entries else DEFAULT_ROLES[role]
        chain = []
        for e in raw:
            if e["provider"] not in specs:
                problems.append(f"llm.roles.{role}: unknown provider {e['provider']!r}")
                continue
            price = None
            if e.get("price_in") is not None or e.get("price_out") is not None:
                price = (float(e.get("price_in") or 0), float(e.get("price_out") or 0))
            chain.append(Route(e["provider"], e["model"], price))
        out[role] = chain
    for role in OPTIONAL_ROLES:  # only when flins.yaml lists them; otherwise they borrow
        entries = configured.get(role)  # type: ignore[call-overload]
        if not entries:
            continue
        chain = []
        for cfg in entries:
            if cfg.provider not in specs:
                problems.append(f"llm.roles.{role}: unknown provider {cfg.provider!r}")
                continue
            price = None
            if cfg.price_in is not None or cfg.price_out is not None:
                price = (float(cfg.price_in or 0), float(cfg.price_out or 0))
            chain.append(Route(cfg.provider, cfg.model, price))
        out[role] = chain
    return out, problems


def provider_status(settings: Settings) -> dict[str, ProviderStatus]:
    specs, _ = provider_specs(settings)
    role_routes, _ = routes(settings)
    status = {
        name: ProviderStatus(spec, spec.key_env is None or bool(settings.env_value(spec.key_env)))
        for name, spec in specs.items()
    }
    for role, chain in role_routes.items():
        for r in chain:
            status[r.provider].used_by.append(f"{role}:{r.model}")
    return status


def build_router(settings: Settings, only: str | None = None) -> tuple[ConfiguredRouter, list[str]]:
    """Backends for every provider that's used and has a key. `only` limits to one provider."""
    specs, problems = provider_specs(settings)
    role_routes, route_problems = routes(settings)
    problems += route_problems
    if only is not None:
        role_routes = {k: [r for r in v if r.provider == only] for k, v in role_routes.items()}
    used = {r.provider for chain in role_routes.values() for r in chain}
    backends: dict[str, Backend] = {}
    for name in sorted(used):
        spec = specs[name]
        key = settings.env_value(spec.key_env)
        if spec.key_env is not None and not key:
            continue
        backends[name] = make_backend(spec, key)
    return ConfiguredRouter(role_routes, backends), problems


def make_brain(
    settings: Settings,
    conn: aiosqlite.Connection,
    clock: Clock,
    backend: Backend | None = None,
    *,
    only: str | None = None,
) -> Brain | None:
    """The LLM parts, or None if some job has no usable provider (FLINS then decides without AI).

    `backend` (tests, recorded evals) bypasses the provider config entirely.
    """
    policy = settings.policy
    gw_kwargs = {
        "daily_budget_usd": policy.limits.daily_llm_budget_usd,
        "tz": ZoneInfo(policy.user.timezone),
    }
    if backend is not None:
        gw = LLMGateway(backend, conn, clock, **gw_kwargs)  # type: ignore[arg-type]
    else:
        router, _ = build_router(settings, only)
        if router.missing_roles():
            return None
        gw = LLMGateway(None, conn, clock, router=router, **gw_kwargs)  # type: ignore[arg-type]
    prompts = settings.prompts_dir
    return Brain(
        gateway=gw,
        triager=Triager(gw, load_prompt(prompts, "triage"), list(policy.categories)),
        drafter=Drafter(gw, load_prompt(prompts, "draft")),
        redflag=RedFlagChecker(gw, load_prompt(prompts, "redflag")),
    )


def make_skills(settings: Settings, conn: aiosqlite.Connection) -> SkillEngine | None:
    """The skill engine for a live run, or None if skills are switched off in flins.yaml."""
    cfg = settings.app.skills
    if not cfg.enabled:
        return None
    return SkillEngine(
        settings.skills_dir,
        settings.skill_runs_dir,
        tz=ZoneInfo(settings.policy.user.timezone),
        conn=conn,
        faq_path=settings.path(cfg.faq_file),
        calendar_path=settings.path(cfg.calendar_ics) if cfg.calendar_ics else None,
    )


def conversation_store(settings: Settings, conn: aiosqlite.Connection) -> ConversationStore:
    cfg = settings.app.assistant
    return ConversationStore(
        conn,
        idle=timedelta(minutes=cfg.session_idle_minutes),
        keep=timedelta(days=cfg.keep_days),
    )


def memory_store(settings: Settings, conn: aiosqlite.Connection, clock: Clock) -> SqlMemory:
    return SqlMemory(conn, ZoneInfo(settings.policy.user.timezone), clock)


def task_store(settings: Settings, conn: aiosqlite.Connection) -> TaskStore:
    return TaskStore(conn, ZoneInfo(settings.policy.user.timezone))


def make_assistant(
    settings: Settings,
    conn: aiosqlite.Connection,
    clock: Clock,
    modes: ModeManager,
    commands: Commands,
    brain: Brain | None,
    approvals: ApprovalService | None,
) -> Assistant:
    """The assistant core (docs/JARVIS_ARCHITECTURE.md). Without a Brain it still runs commands
    and says plainly that it can't chat."""
    cfg = settings.app.assistant
    chat = summariser = gateway = None
    if brain is not None and cfg.enabled:
        gateway = brain.gateway
        chat = ChatEngine(gateway, load_prompt(settings.prompts_dir, "chat"))
        summariser = Summariser(gateway, load_prompt(settings.prompts_dir, "summary"))
    store = conversation_store(settings, conn)
    stored = memory_store(settings, conn, clock)
    tasks = task_store(settings, conn)
    audit = AuditLog(conn)
    return Assistant(
        commands,
        store,
        modes,
        settings.policy,
        cfg,
        clock,
        audit,
        chat=chat,
        summariser=summariser,
        gateway=gateway,
        memory=CombinedMemory(ProfileMemory(settings.about), stored, cfg),
        approvals=approvals,
        stored=stored,
        memory_commands=MemoryCommands(stored, store, commands, cfg, audit),
        tasks=tasks,
        task_commands=TaskCommands(tasks, commands),
    )


@dataclass
class AssistantRuntime:
    assistant: Assistant
    router: CommandRouter
    conn: aiosqlite.Connection
    modes: ModeManager

    async def close(self) -> None:
        await self.modes.close()
        await self.conn.close()


async def start_assistant(settings: Settings, backend: Backend | None = None) -> AssistantRuntime:
    """An assistant for a process with no WhatsApp connection (`flins ask`, `flins chat`, the
    standalone dashboard). It can talk and change modes, but can't send or approve: there is
    no outbox worker here, and a queued message must never go out hours later."""
    conn = await connect(settings.db_path)
    await migrate(conn)
    clock = SystemClock()
    modes = await ModeManager.open(settings.db_path, EventBus(), settings.policy, clock)
    brain = make_brain(settings, conn, clock, backend)
    intents = (
        IntentParser(brain.gateway, load_prompt(settings.prompts_dir, "intent"))
        if brain is not None
        else None
    )
    router = CommandRouter(
        modes,
        settings.policy,
        clock,
        None,
        intents=intents,
        conn=conn,
        conversational=True,
        can_send=False,
    )
    assistant = make_assistant(settings, conn, clock, modes, router, brain, None)
    return AssistantRuntime(assistant, router, conn, modes)


@asynccontextmanager
async def open_assistant(
    settings: Settings, backend: Backend | None = None
) -> AsyncIterator[AssistantRuntime]:
    rt = await start_assistant(settings, backend)
    try:
        yield rt
    finally:
        await rt.close()
