"""JARVIS phase L: local models. One model in video memory at a time, a `code` role, and
task routing. Ollama is faked with an HTTP mock: no model is needed to run these."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from flins.assistant.chat import ChatOut
from flins.llm.local import LocalModels, native_base
from flins.llm.prompts import load_prompt
from flins.llm.providers import (
    BUILTIN_PROVIDERS,
    ConfiguredRouter,
    LocalBackend,
    OpenAICompatBackend,
    Route,
    make_backend,
)
from flins.settings import AppConfig
from tests.conftest import REPO_ROOT
from tests.unit.test_j1_assistant import Rig, build

PROMPTS = REPO_ROOT / "prompts"


class FakeOllama:
    """Enough of Ollama's API: /api/ps, /api/generate (unload), /v1/chat/completions (load)."""

    def __init__(self, loaded: list[str] | None = None) -> None:
        self.loaded = list(loaded or [])
        self.log: list[str] = []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.16"})
        if path == "/api/ps":
            models = [
                {"name": n, "size": 4_000_000_000, "size_vram": 4_000_000_000} for n in self.loaded
            ]
            return httpx.Response(200, json={"models": models})
        if path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "small:4b", "size": 3_300_000_000}]}
            )
        body = json.loads(request.content)
        if path == "/api/generate":
            assert body["keep_alive"] == 0
            self.loaded.remove(body["model"])
            self.log.append(f"unload {body['model']}")
            return httpx.Response(200, json={"done": True})
        if path == "/v1/chat/completions":
            self.bodies.append(body)
            if body["model"] not in self.loaded:
                self.loaded.append(body["model"])
            self.log.append(f"ask {body['model']}")
            answer = {
                "reply": "ok",
                "kind": "answer",
                "claims_action": False,
                "remember": None,
                "remember_kind": None,
                "remember_importance": None,
            }
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "choices": [
                        {"message": {"content": json.dumps(answer)}, "finish_reason": "stop"}
                    ],
                },
            )
        return httpx.Response(404)


def test_native_base() -> None:
    assert native_base("http://localhost:11434/v1") == "http://localhost:11434"
    assert native_base("http://localhost:11434/v1/") == "http://localhost:11434"
    assert native_base("http://box:11434") == "http://box:11434"


async def test_only_one_model_stays_loaded() -> None:
    fake = FakeOllama(loaded=["old:9b"])
    backend = LocalBackend(BUILTIN_PROVIDERS["ollama"], None, transport=httpx.MockTransport(fake))
    chat = load_prompt(PROMPTS, "chat")
    from dataclasses import replace

    for model in ("small:4b", "small:4b", "coder:7b", "small:4b"):
        await backend.complete(prompt=replace(chat, model=model), user="hi", schema={})
    assert fake.log == [
        "unload old:9b",  # whatever was there before FLINS started
        "ask small:4b",
        "ask small:4b",  # same model again: nothing to unload, no extra round trip
        "unload small:4b",
        "ask coder:7b",
        "unload coder:7b",
        "ask small:4b",
    ]
    assert fake.loaded == ["small:4b"]
    assert fake.bodies[0]["response_format"]["type"] == "json_schema"  # schema-constrained


async def test_status_helpers_and_ollama_down() -> None:
    fake = FakeOllama(loaded=["small:4b"])
    models = LocalModels("http://localhost:11434/v1", transport=httpx.MockTransport(fake))
    assert await models.version() == "0.40.16"
    assert [m.name for m in await models.installed()] == ["small:4b"]
    loaded = await models.loaded()
    assert loaded[0].vram_gb == pytest.approx(4.0)
    assert await models.unload_all() == ["small:4b"] and fake.loaded == []

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    down = LocalModels("http://localhost:11434/v1", transport=httpx.MockTransport(refuse))
    assert await down.version() is None
    assert await down.use("small:4b") == []  # never raises: the real request reports it


def test_ollama_is_local_and_other_providers_are_not() -> None:
    assert isinstance(make_backend(BUILTIN_PROVIDERS["ollama"], None), LocalBackend)
    groq = make_backend(BUILTIN_PROVIDERS["groq"], "k")
    assert isinstance(groq, OpenAICompatBackend) and not isinstance(groq, LocalBackend)


def test_code_role_borrows_and_can_be_configured() -> None:
    from flins.llm.gateway import FakeBackend

    routes = {"triage": [Route("a", "t")], "draft": [Route("a", "d")], "redflag": [Route("a", "t")]}
    router = ConfiguredRouter(routes, {"a": FakeBackend(), "ollama": FakeBackend()})
    chat = load_prompt(PROMPTS, "chat")
    assert [x.prompt.model for x in router.chain(chat, "code")] == [
        "d"
    ]  # code->reason->chat->draft
    routes["reason"] = [Route("ollama", "think:9b")]
    assert [x.prompt.model for x in router.chain(chat, "code")] == ["think:9b"]
    routes["code"] = [Route("ollama", "coder:7b"), Route("a", "d")]  # local first, cloud fallback
    assert [(x.provider, x.prompt.model) for x in router.chain(chat, "code")] == [
        ("ollama", "coder:7b"),
        ("a", "d"),
    ]
    AppConfig.model_validate(
        {"llm": {"roles": {"code": [{"provider": "ollama", "model": "coder:7b"}]}}}
    )


@pytest.fixture
async def rig(project: Path) -> AsyncIterator[Rig]:
    r = await build(project)
    yield r
    await r.modes.close()
    await r.conn.close()


async def test_questions_go_to_the_right_specialist(rig: Rig) -> None:
    seen: list[tuple[str, str]] = []

    class Spy:
        prompt_version = "chat_v2"

        async def reply(self, context: str, role: str = "chat") -> ChatOut:
            seen.append((context.rsplit("<message>\n", 1)[1].split("\n")[0][:28], role))
            return ChatOut(reply="ok", kind="answer", claims_action=False)

    rig.assistant._chat = Spy()  # type: ignore[assignment]
    for text in (
        "what's the capital of France?",
        "why does my python loop skip items?",
        "```\nfor i in x: pass\n```",
        "make a plan for my revision week",
        "is java or javascript older?",
        "what is the capital gain here?",  # "api" must not match inside "capital"
    ):
        await rig.say(text, source="cli")
    assert [r for _, r in seen] == ["chat", "code", "code", "reason", "code", "chat"]
    await rig.say("debug my python script", source="voice")
    assert seen[-1][1] == "chat"  # voice stays on the fast model by default
    rig.settings.app.assistant.voice_big_models = True
    await rig.say("debug my python script", source="voice")
    await rig.say("compare these two plans step by step", source="voice")
    assert [r for _, r in seen[-2:]] == ["code", "reason"]
