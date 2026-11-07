"""Build the runtime pieces from settings (for `flins run`, `flins eval --live`, `flins llm`)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from zoneinfo import ZoneInfo

import aiosqlite

from flins.core.clock import Clock
from flins.core.pipeline import Brain
from flins.llm.gateway import Backend, LLMGateway
from flins.llm.prompts import load_prompt
from flins.llm.providers import (
    BUILTIN_PROVIDERS,
    DEFAULT_ROLES,
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
