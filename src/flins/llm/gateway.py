"""The one place FLINS talks to an LLM.

Every call: structured JSON output, a hard timeout, SDK retries, Pydantic validation of the
answer, refusal handling, and a metering row in `llm_calls` (tokens, cost, prompt version, no
message text). Any failure raises `LLMError`; callers then fall back to the cautious path.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast
from zoneinfo import ZoneInfo

import aiosqlite
from pydantic import BaseModel, ValidationError

from flins.core.clock import Clock, to_iso
from flins.llm.prompts import Prompt

T = TypeVar("T", bound=BaseModel)

# USD per million tokens (input, output). Cache reads 0.1x input, cache writes 1.25x input.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
DEFAULT_TIMEOUT_S = 30.0


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


class Backend(Protocol):
    async def complete(
        self, *, prompt: Prompt, user: str, schema: dict[str, Any]
    ) -> RawResponse: ...


def cost_usd(r: RawResponse) -> float:
    inp, out = PRICES.get(r.model, (0.0, 0.0))
    return (
        r.input_tokens * inp
        + r.output_tokens * out
        + r.cache_read * inp * 0.1
        + r.cache_write * inp * 1.25
    ) / 1_000_000


class AnthropicBackend:
    """Live Claude API. Reads ANTHROPIC_API_KEY (or another SDK credential source)."""

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
        )


Responder = Callable[[Prompt, str], Awaitable[Any] | Any]


class FakeBackend:
    """Recorded answers for tests and evals.

    `answers` maps a prompt name ("triage", "draft", "redflag") to either a dict (returned as
    JSON), a string (returned as-is, e.g. broken JSON), an Exception (raised), or a callable
    (prompt, user_text) -> one of those.
    """

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers: dict[str, Any] = dict(answers or {})
        self.calls: list[tuple[str, str]] = []

    async def complete(self, *, prompt: Prompt, user: str, schema: dict[str, Any]) -> RawResponse:
        self.calls.append((prompt.name, user))
        answer = self.answers.get(prompt.name)
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
        backend: Backend,
        conn: aiosqlite.Connection,
        clock: Clock,
        *,
        daily_budget_usd: float,
        tz: ZoneInfo,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._backend = backend
        self._conn = conn
        self._clock = clock
        self._budget = daily_budget_usd
        self._tz = tz
        self._timeout = timeout_s

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
        started = time.monotonic()
        raw: RawResponse | None = None
        error: str | None = None
        try:
            raw = await asyncio.wait_for(
                self._backend.complete(prompt=prompt, user=user, schema=schema), self._timeout
            )
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
            await self._meter(prompt, raw, error, int((time.monotonic() - started) * 1000))

    async def _meter(
        self, prompt: Prompt, raw: RawResponse | None, error: str | None, latency_ms: int
    ) -> None:
        await self._conn.execute(
            "INSERT INTO llm_calls (ts, purpose, model, prompt_version, ok, error, input_tokens, "
            "output_tokens, cache_read, cache_write, cost_usd, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                to_iso(self._clock.now()),
                prompt.name,
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
        self.calls = 0
        self.cost = 0.0

    async def complete(self, *, prompt: Prompt, user: str, schema: dict[str, Any]) -> RawResponse:
        raw = await self._inner.complete(prompt=prompt, user=user, schema=schema)
        self.calls += 1
        self.cost += cost_usd(raw)
        return raw
