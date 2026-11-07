"""`flins llm status | test | models`: see and check the configured LLM providers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import aiosqlite

from flins.core.clock import Clock
from flins.llm.gateway import Attempt, LLMError, LLMGateway
from flins.llm.prompts import load_prompt
from flins.llm.providers import BUILTIN_PROVIDERS, OpenAICompatBackend, make_backend
from flins.llm.services import REDFLAG_SCHEMA, RedFlagOut
from flins.settings import Settings
from flins.wiring import build_router, provider_specs, provider_status, routes

# A tiny, harmless request: costs a fraction of a cent and proves key, model and JSON mode work.
PROBE = (
    "<untrusted_thread>\n[10:00] Contact: hi, are you free later?\n</untrusted_thread>\n"
    "<proposed_reply>\nHey! He's busy right now and will reply later.\n</proposed_reply>\n"
    "Is this reply safe to send unreviewed?"
)


def status_lines(settings: Settings) -> list[str]:
    lines = []
    for name, st in sorted(provider_status(settings).items()):
        if not st.used_by and name not in settings.app.llm.providers:
            continue
        key = (
            "no key needed"
            if st.spec.key_env is None
            else f"{st.spec.key_env} {'set' if st.has_key else 'NOT set'}"
        )
        used = ", ".join(st.used_by) if st.used_by else "not used by any job"
        lines.append(f"{name:<11} {key:<28} {used}")
    _, problems = routes(settings)
    router, _ = build_router(settings)
    lines.append("")
    for role, chain in router.describe().items():
        lines.append(f"{role:<8} tries: {' -> '.join(chain) or '(nothing configured)'}")
    missing = router.missing_roles()
    if missing:
        lines.append(f"No usable provider for: {', '.join(missing)}. FLINS will run without AI.")
    lines += [f"config problem: {p}" for p in problems]
    return lines


@dataclass
class ProbeResult:
    route: str
    ok: bool
    detail: str
    ms: int


async def test_routes(
    settings: Settings, conn: aiosqlite.Connection, clock: Clock, only: str | None = None
) -> list[ProbeResult]:
    """Send PROBE through every configured provider/model that has a key, one at a time."""
    router, _ = build_router(settings, only)
    prompt = load_prompt(settings.prompts_dir, "redflag")
    gw = LLMGateway(
        None,
        conn,
        clock,
        router=router,
        daily_budget_usd=settings.policy.limits.daily_llm_budget_usd,
        tz=ZoneInfo(settings.policy.user.timezone),
    )
    results = []
    seen: set[str] = set()
    for role in ("triage", "draft", "redflag"):
        role_prompt = load_prompt(settings.prompts_dir, role)
        for attempt in router.chain(role_prompt):
            label = f"{attempt.provider}/{attempt.prompt.model}"
            if label in seen:
                continue
            seen.add(label)
            single = Attempt(
                attempt.backend,
                prompt.__class__(**{**prompt.__dict__, "model": attempt.prompt.model}),
                attempt.provider,
                attempt.price,
            )
            started = time.monotonic()
            try:
                out = await gw._one(single, PROBE, REDFLAG_SCHEMA, RedFlagOut)
                results.append(
                    ProbeResult(
                        label,
                        True,
                        f"answered safe={out.safe}",
                        int((time.monotonic() - started) * 1000),
                    )
                )
            except LLMError as exc:
                results.append(
                    ProbeResult(label, False, str(exc), int((time.monotonic() - started) * 1000))
                )
    return results


async def list_models(settings: Settings, provider: str) -> list[str]:
    specs, _ = provider_specs(settings)
    spec = specs.get(provider)
    if spec is None:
        known = ", ".join(sorted(specs))
        raise LLMError(f"unknown provider {provider!r}; known: {known}")
    key = settings.env_value(spec.key_env)
    if spec.key_env is not None and not key:
        raise LLMError(f"{spec.key_env} is not set")
    if spec.kind == "anthropic":
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=key)
        return sorted([m.id async for m in client.models.list()])
    backend = make_backend(spec, key)
    assert isinstance(backend, OpenAICompatBackend)
    try:
        return await backend.list_models()
    finally:
        await backend.aclose()


__all__ = ["BUILTIN_PROVIDERS", "list_models", "status_lines", "test_routes"]
