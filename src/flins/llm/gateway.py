"""The one place FLINS talks to an LLM.

Every call: structured JSON output, a hard timeout, retries, Pydantic validation of the answer,
refusal handling, and a metering row in `llm_calls` (provider, model, tokens, cost, prompt
version; never message text). Any failure raises `LLMError`; callers then take the cautious path.

Several providers can be configured per job (triage / draft / redflag). The gateway tries them in
order and moves on when one fails or returns unusable JSON. A *refusal* is never routed around:
if a model declines, FLINS tells the user instead of asking another model.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol, TypeVar, cast
from zoneinfo import ZoneInfo

import aiosqlite
from pydantic import BaseModel, ValidationError

from flins.core.clock import Clock, to_iso
from flins.llm.prompts import Prompt

T = TypeVar("T", bound=BaseModel)

# USD per million tokens (input, output). Cache reads 0.1x input, cache writes 1.25x input.
# Models not listed here cost 0 unless a price is set in flins.yaml (llm.roles.*.price_in/out).
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "openai/gpt-oss-20b": (0.075, 0.30),  # Groq list price
    "openai/gpt-oss-120b": (0.15, 0.60),  # Groq list price
}
# Outer safety net per attempt; each provider also has its own HTTP timeout.
DEFAULT_TIMEOUT_S = 90.0


class LLMError(RuntimeError):
    """The call failed or the answer was unusable. Never act on a partial answer."""


class LLMRefusal(LLMError):
    """The model declined. FLINS treats this as 'tell the user'."""


@dataclass(frozen=True)
class RawResponse:
    text: str
    model: str
    stop_reason: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    provider: str | None = None
    price: tuple[float, float] | None = None  # overrides PRICES when set


class Backend(Protocol):
    async def complete(
        self, *, prompt: Prompt, user: str, schema: dict[str, Any]
    ) -> RawResponse: ...


def cost_usd(r: RawResponse) -> float:
    inp, out = r.price or PRICES.get(r.model, (0.0, 0.0))
    return (
        r.input_tokens * inp
        + r.output_tokens * out
        + r.cache_read * inp * 0.1
        + r.cache_write * inp * 1.25
    ) / 1_000_000


@dataclass(frozen=True)
class Attempt:
    """One provider/model to try for a prompt."""

    backend: Backend
    prompt: Prompt  # the prompt with this attempt's model filled in
    provider: str
    price: tuple[float, float] | None = None


class Router(Protocol):
    def chain(self, prompt: Prompt) -> list[Attempt]: ...


class AnthropicBackend:
    """Live Claude API. Reads ANTHROPIC_API_KEY (or another SDK credential source)."""

    name = "anthropic"

    def __init__(self, api_key: str | None = None, *, timeout_s: float = 20.0) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=2)

    async def complete(self, *, prompt: Prompt, user: str, schema: dict[str, Any]) -> RawResponse:
        from anthropic.types import MessageParam, OutputConfigParam, TextBlockParam

        a = self._anthropic
        system: list[TextBlockParam] = [
            {"type": "text", "text": prompt.system, "cache_control": {"type": "ephemeral"}}
        ]
        messages: list[MessageParam] = [{"role": "user", "content": user}]
        output_config = cast(
            OutputConfigParam,
            {"effort": prompt.effort, "format": {"type": "json_schema", "schema": schema}},
        )
        try:
            msg = await self._client.messages.create(
                model=prompt.model,
                max_tokens=prompt.max_tokens,
                system=system,
                messages=messages,
                output_config=output_config,
            )
        except a.AuthenticationError as exc:
            raise LLMError("authentication failed (check ANTHROPIC_API_KEY)") from exc
        except a.NotFoundError as exc:
            raise LLMError(f"model not found: {prompt.model}") from exc
        except a.RateLimitError as exc:
            raise LLMError("rate limited") from exc
        except a.APITimeoutError as exc:
            raise LLMError("timeout") from exc
        except a.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code}") from exc
        except a.APIConnectionError as exc:
            raise LLMError("connection error") from exc
        text = next((b.text for b in msg.content if b.type == "text"), "")
        u = msg.usage
        return RawResponse(
            text=text,
            model=msg.model,
            stop_reason=msg.stop_reason,
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read=u.cache_read_input_tokens or 0,
            cache_write=u.cache_creation_input_tokens or 0,
            provider="anthropic",
        )


Responder = Callable[[Prompt, str], Awaitable[Any] | Any]


class FakeBackend:
    """Recorded answers for tests and evals.

    `answers` maps a prompt name ("triage_v1", "draft_v1", "redflag_v1") to either a dict
    (returned as JSON), a string (returned as-is, e.g. broken JSON), an Exception (raised), or a
    callable (prompt, user_text) -> one of those.
    """

    name = "fake"

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers: dict[str, Any] = dict(answers or {})
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def job(name: str) -> str:
        """'draft_v2' -> 'draft'. Recorded answers match any version of a job's prompt."""
        return name.rsplit("_v", 1)[0] if "_v" in name else name

    async def complete(self, *, prompt: Prompt, user: str, schema: dict[str, Any]) -> RawResponse:
        self.calls.append((prompt.name, user))
        answer = self.answers.get(prompt.name)
        if answer is None:
            by_job = {self.job(k): v for k, v in self.answers.items()}
            answer = by_job.get(self.job(prompt.name))
        if callable(answer):
            answer = answer(prompt, user)
            if asyncio.iscoroutine(answer):
                answer = await answer
        if answer is None:
            raise LLMError(f"no recorded answer for {prompt.name}")
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, dict) and answer.get("__refusal__"):
            return RawResponse(text="", model=prompt.model, stop_reason="refusal")
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return RawResponse(
            text=text,
            model=prompt.model,
            stop_reason="end_turn",
            input_tokens=len(user) // 4,
            output_tokens=len(text) // 4,
        )


class LLMGateway:
    def __init__(
        self,
        backend: Backend | None,
        conn: aiosqlite.Connection,
        clock: Clock,
        *,
        daily_budget_usd: float,
        tz: ZoneInfo,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        router: Router | None = None,
    ) -> None:
        if backend is None and router is None:
            raise ValueError("LLMGateway needs a backend or a router")
        self._backend = backend
        self._router = router
        self._conn = conn
        self._clock = clock
        self._budget = daily_budget_usd
        self._tz = tz
        self._timeout = timeout_s

    def chain(self, prompt: Prompt) -> list[Attempt]:
        if self._router is not None:
            return self._router.chain(prompt)
        assert self._backend is not None
        return [Attempt(self._backend, prompt, getattr(self._backend, "name", "llm"))]

    async def spent_today(self) -> float:
        local_now = self._clock.now().astimezone(self._tz)
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        async with self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE ts >= ?", (to_iso(start),)
        ) as cur:
            row = await cur.fetchone()
        return float(row[0]) if row else 0.0

    async def budget_ok(self) -> bool:
        return await self.spent_today() < self._budget

    async def json(self, *, prompt: Prompt, user: str, schema: dict[str, Any], out: type[T]) -> T:
        attempts = self.chain(prompt)
        if not attempts:
            raise LLMError(f"no LLM provider available for {prompt.name}")
        failures: list[str] = []
        for attempt in attempts:
            try:
                return await self._one(attempt, user, schema, out)
            except LLMRefusal:
                raise  # never route a refusal to another model
            except LLMError as exc:
                failures.append(f"{attempt.provider}: {exc}")
        if len(failures) == 1:
            raise LLMError(failures[0])
        raise LLMError("all providers failed (" + "; ".join(failures) + ")")

    async def _one(self, a: Attempt, user: str, schema: dict[str, Any], out: type[T]) -> T:
        started = time.monotonic()
        raw: RawResponse | None = None
        error: str | None = None
        try:
            raw = await asyncio.wait_for(
                a.backend.complete(prompt=a.prompt, user=user, schema=schema), self._timeout
            )
            raw = replace(raw, provider=raw.provider or a.provider, price=raw.price or a.price)
            if raw.stop_reason == "refusal":
                raise LLMRefusal("model declined")
            if raw.stop_reason == "max_tokens":
                raise LLMError("answer cut off (max_tokens)")
            try:
                return out.model_validate_json(raw.text)
            except ValidationError as exc:
                raise LLMError(f"invalid JSON answer ({exc.error_count()} errors)") from exc
        except TimeoutError as exc:
            error = "timeout"
            raise LLMError("timeout") from exc
        except LLMError as exc:
            error = str(exc)[:200]
            raise
        finally:
            await self._meter(a, raw, error, int((time.monotonic() - started) * 1000))

    async def _meter(
        self, a: Attempt, raw: RawResponse | None, error: str | None, latency_ms: int
    ) -> None:
        prompt = a.prompt
        await self._conn.execute(
            "INSERT INTO llm_calls (ts, purpose, provider, model, prompt_version, ok, error, "
            "input_tokens, output_tokens, cache_read, cache_write, cost_usd, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                to_iso(self._clock.now()),
                prompt.name,
                a.provider,
                raw.model if raw else prompt.model,
                prompt.version,
                int(error is None),
                error,
                raw.input_tokens if raw else 0,
                raw.output_tokens if raw else 0,
                raw.cache_read if raw else 0,
                raw.cache_write if raw else 0,
                cost_usd(raw) if raw else 0.0,
                latency_ms,
            ),
        )
        await self._conn.commit()


class CountingBackend:
    """Wraps a backend and totals calls and cost (used by `flins eval --live`)."""

    def __init__(self, inner: Backend) -> None:
        self._inner = inner
        self.name = getattr(inner, "name", "llm")
        self.calls = 0
        self.cost = 0.0

    async def complete(self, *, prompt: Prompt, user: str, schema: dict[str, Any]) -> RawResponse:
        raw = await self._inner.complete(prompt=prompt, user=user, schema=schema)
        self.calls += 1
        self.cost += cost_usd(raw)
        return raw
