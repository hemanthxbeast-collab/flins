"""Multi-provider LLM support: OpenAI-compatible backend, routing/fallback, config, .env keys."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite
import httpx
import pytest

from flins.core.clock import FrozenClock
from flins.core.db import connect, migrate
from flins.llm.gateway import Attempt, FakeBackend, LLMError, LLMGateway, LLMRefusal
from flins.llm.prompts import load_prompt
from flins.llm.providers import (
    BUILTIN_PROVIDERS,
    ConfiguredRouter,
    OpenAICompatBackend,
    Route,
    extract_json,
)
from flins.llm.services import REDFLAG_SCHEMA, RedFlagOut
from flins.settings import load_settings, read_env_file
from flins.wiring import build_router, make_brain, provider_status
from tests.conftest import REPO_ROOT

T0 = datetime(2026, 11, 9, 4, 30, tzinfo=UTC)
PROMPT = load_prompt(REPO_ROOT / "prompts", "redflag_v1")
OK_JSON = {"safe": True, "reason": "fine"}


def chat_response(content: str, finish: str = "stop", **message: Any) -> dict[str, Any]:
    return {
        "model": "m",
        "choices": [{"message": {"content": content, **message}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def backend(
    name: str, handler: Callable[[httpx.Request], httpx.Response], **kw: Any
) -> OpenAICompatBackend:
    spec = replace(BUILTIN_PROVIDERS[name], **kw)
    return OpenAICompatBackend(spec, "test-key", transport=httpx.MockTransport(handler), retries=1)


# ------------------------------------------------------------------ OpenAI-compatible backend


async def test_request_shape_for_json_schema_providers() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "url": str(req.url),
                "auth": req.headers["authorization"],
                "body": json.loads(req.content),
            }
        )
        return httpx.Response(200, json=chat_response(json.dumps(OK_JSON)))

    b = backend("groq", handler)
    raw = await b.complete(
        prompt=replace(PROMPT, model="openai/gpt-oss-20b"), user="hi", schema=REDFLAG_SCHEMA
    )
    assert raw.stop_reason == "end_turn" and json.loads(raw.text) == OK_JSON
    assert raw.input_tokens == 100 and raw.provider == "groq"
    req = seen[0]
    assert req["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert req["auth"] == "Bearer test-key"
    body = req["body"]
    assert body["model"] == "openai/gpt-oss-20b"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == REDFLAG_SCHEMA
    assert body["reasoning_effort"] == "low"
    assert body["max_tokens"] >= 4096  # room for thinking before the JSON
    assert body["messages"][0]["role"] == "system" and body["messages"][1]["content"] == "hi"


async def test_prompt_mode_puts_schema_in_system_prompt() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=chat_response("```json\n" + json.dumps(OK_JSON) + "\n```"))

    raw = await backend("nvidia", handler).complete(prompt=PROMPT, user="hi", schema=REDFLAG_SCHEMA)
    assert json.loads(raw.text) == OK_JSON  # code fences stripped
    assert "response_format" not in bodies[0]
    assert '"safe"' in bodies[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('Sure! Here it is: {"a": 1} hope that helps', '{"a": 1}'),
        ("no json here", "no json here"),
    ],
)
def test_extract_json(text: str, expected: str) -> None:
    assert extract_json(text) == expected


async def test_retries_rate_limit_then_succeeds() -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429, headers={"retry-after": "0"}, json={"error": {"message": "slow down"}}
            )
        return httpx.Response(200, json=chat_response(json.dumps(OK_JSON)))

    raw = await backend("gemini", handler).complete(prompt=PROMPT, user="x", schema=REDFLAG_SCHEMA)
    assert calls == 2 and json.loads(raw.text) == OK_JSON


@pytest.mark.parametrize(
    ("status", "match"),
    [(401, "authentication failed .*GROQ_API_KEY"), (404, "not found"), (400, "HTTP 400")],
)
async def test_errors_are_llm_errors(status: int, match: str) -> None:
    b = backend("groq", lambda req: httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(LLMError, match=match):
        await b.complete(prompt=PROMPT, user="x", schema=REDFLAG_SCHEMA)


async def test_connection_errors_are_llm_errors() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(LLMError, match="connection error"):
        await backend("ollama", handler).complete(prompt=PROMPT, user="x", schema=REDFLAG_SCHEMA)


@pytest.mark.parametrize(
    ("response", "stop"),
    [
        (chat_response("", "content_filter"), "refusal"),
        (chat_response("", "stop", refusal="I can't help with that"), "refusal"),
        (chat_response('{"safe": tr', "length"), "max_tokens"),
    ],
)
async def test_stop_reasons(response: dict[str, Any], stop: str) -> None:
    raw = await backend("openai", lambda req: httpx.Response(200, json=response)).complete(
        prompt=PROMPT, user="x", schema=REDFLAG_SCHEMA
    )
    assert raw.stop_reason == stop


# ------------------------------------------------------------------ routing and fallback


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    c = await connect(tmp_path / "t.db")
    await migrate(c)
    yield c
    await c.close()


def gateway(conn: aiosqlite.Connection, routes: list[tuple[str, FakeBackend]]) -> LLMGateway:
    router = ConfiguredRouter(
        {"redflag": [Route(name, f"{name}-model") for name, _ in routes]},
        dict(routes),  # type: ignore[arg-type]
    )
    return LLMGateway(
        None,
        conn,
        FrozenClock(T0),
        router=router,
        daily_budget_usd=1.0,
        tz=ZoneInfo("Asia/Kolkata"),
    )


async def calls(conn: aiosqlite.Connection) -> list[tuple[str, int]]:
    async with conn.execute("SELECT provider, ok FROM llm_calls ORDER BY id") as cur:
        return [(r[0], r[1]) for r in await cur.fetchall()]


async def test_falls_through_errors_and_bad_json(conn: aiosqlite.Connection) -> None:
    gw = gateway(
        conn,
        [
            ("groq", FakeBackend({"redflag_v1": LLMError("HTTP 503")})),
            ("gemini", FakeBackend({"redflag_v1": "not json"})),
            ("nvidia", FakeBackend({"redflag_v1": OK_JSON})),
        ],
    )
    out = await gw.json(prompt=PROMPT, user="x", schema=REDFLAG_SCHEMA, out=RedFlagOut)
    assert out.safe is True
    assert await calls(conn) == [("groq", 0), ("gemini", 0), ("nvidia", 1)]


async def test_refusal_is_never_routed_to_another_model(conn: aiosqlite.Connection) -> None:
    second = FakeBackend({"redflag_v1": OK_JSON})
    gw = gateway(
        conn, [("groq", FakeBackend({"redflag_v1": {"__refusal__": True}})), ("gemini", second)]
    )
    with pytest.raises(LLMRefusal):
        await gw.json(prompt=PROMPT, user="x", schema=REDFLAG_SCHEMA, out=RedFlagOut)
    assert second.calls == []


async def test_all_fail_reports_each(conn: aiosqlite.Connection) -> None:
    gw = gateway(
        conn,
        [
            ("groq", FakeBackend({"redflag_v1": LLMError("a")})),
            ("gemini", FakeBackend({"redflag_v1": LLMError("b")})),
        ],
    )
    with pytest.raises(LLMError, match=r"all providers failed .*groq: a.*gemini: b"):
        await gw.json(prompt=PROMPT, user="x", schema=REDFLAG_SCHEMA, out=RedFlagOut)


async def test_model_comes_from_the_route(conn: aiosqlite.Connection) -> None:
    fake = FakeBackend({"redflag_v1": OK_JSON})
    gw = gateway(conn, [("groq", fake)])
    attempts: list[Attempt] = gw.chain(PROMPT)
    assert attempts[0].prompt.model == "groq-model"


# ------------------------------------------------------------------ config and keys


def test_env_file_reader(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text(
        "# comment\nGROQ_API_KEY=gsk_abc   # from console\nGEMINI_API_KEY=\n"
        "NVIDIA_API_KEY='nvapi-x #notacomment'\nANTHROPIC_API_KEY=     # empty with comment\n",
        encoding="utf-8",
    )
    env = read_env_file(f)
    assert env == {"GROQ_API_KEY": "gsk_abc", "NVIDIA_API_KEY": "nvapi-x #notacomment"}


def write_project(project: Path, flins_yaml: str, env: str) -> None:
    (project / "config" / "flins.yaml").write_text(flins_yaml, encoding="utf-8")
    (project / ".env").write_text(env, encoding="utf-8")


ROLES_YAML = """
llm:
  roles:
    triage:
      - {provider: anthropic, model: claude-haiku-5-5}
      - {provider: groq, model: openai/gpt-oss-20b}
    draft:
      - {provider: gemini, model: gemini-3.8-flash}
      - {provider: nvidia, model: meta/llama-3.3-70b-instruct}
    redflag: [{provider: groq, model: openai/gpt-oss-20b, price_in: 0.1, price_out: 0.2}]
"""


def test_router_uses_only_providers_with_keys(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    write_project(project, ROLES_YAML, "GROQ_API_KEY=gsk_1\nNVIDIA_API_KEY=nvapi-1\n")
    settings = load_settings(project)
    router, problems = build_router(settings)
    assert problems == []
    assert router.describe()["triage"] == [
        "anthropic/claude-haiku-5-5 (no key)",
        "groq/openai/gpt-oss-20b",
    ]
    assert router.missing_roles() == []
    draft = router.chain(load_prompt(REPO_ROOT / "prompts", "draft_v1"))
    assert [(a.provider, a.prompt.model) for a in draft] == [
        ("nvidia", "meta/llama-3.3-70b-instruct")
    ]
    redflag = router.chain(PROMPT)
    assert redflag[0].price == (0.1, 0.2)
    status = provider_status(settings)
    assert status["groq"].has_key and not status["gemini"].has_key


def test_only_one_provider(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    write_project(project, ROLES_YAML, "GROQ_API_KEY=gsk_1\nNVIDIA_API_KEY=nvapi-1\n")
    router, _ = build_router(load_settings(project), only="groq")
    assert router.missing_roles() == ["draft"]  # groq isn't listed for drafting


async def test_no_keys_means_no_brain(
    project: Path, monkeypatch: pytest.MonkeyPatch, conn: aiosqlite.Connection
) -> None:
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "NVIDIA_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    write_project(project, ROLES_YAML, "")
    assert make_brain(load_settings(project), conn, FrozenClock(T0)) is None


def test_unknown_provider_and_custom_provider(project: Path) -> None:
    write_project(
        project,
        """
llm:
  providers:
    together: {base_url: "https://api.together.xyz/v1", key_env: TOGETHER_API_KEY}
  roles:
    triage: [{provider: together, model: x}, {provider: mystery, model: y}]
""",
        "TOGETHER_API_KEY=t\n",
    )
    router, problems = build_router(load_settings(project))
    assert any("mystery" in p for p in problems)
    assert router.describe()["triage"] == ["together/x"]


def test_example_config_parses(project: Path) -> None:
    settings = load_settings(project)  # uses flins.example.yaml
    assert {r.provider for r in settings.app.llm.roles["draft"]} >= {
        "anthropic",
        "gemini",
        "groq",
        "nvidia",
    }
