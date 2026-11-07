"""Build the runtime pieces from settings (shared by `flins run` and `flins eval --live`)."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import aiosqlite

from flins.core.clock import Clock
from flins.core.pipeline import Brain
from flins.llm.gateway import AnthropicBackend, Backend, LLMGateway
from flins.llm.prompts import load_prompt
from flins.llm.services import Drafter, RedFlagChecker, Triager
from flins.settings import Settings


def live_backend(settings: Settings) -> AnthropicBackend | None:
    key = settings.secrets.anthropic_api_key
    return AnthropicBackend(key.get_secret_value()) if key else None


def make_brain(
    settings: Settings, conn: aiosqlite.Connection, clock: Clock, backend: Backend | None = None
) -> Brain | None:
    """The LLM parts, or None when there's no API key (FLINS then decides without AI)."""
    backend = backend or live_backend(settings)
    if backend is None:
        return None
    policy = settings.policy
    gw = LLMGateway(
        backend,
        conn,
        clock,
        daily_budget_usd=policy.limits.daily_llm_budget_usd,
        tz=ZoneInfo(policy.user.timezone),
    )
    prompts = settings.prompts_dir
    return Brain(
        gateway=gw,
        triager=Triager(gw, load_prompt(prompts, "triage_v1"), list(policy.categories)),
        drafter=Drafter(gw, load_prompt(prompts, "draft_v1")),
        redflag=RedFlagChecker(gw, load_prompt(prompts, "redflag_v1")),
    )
