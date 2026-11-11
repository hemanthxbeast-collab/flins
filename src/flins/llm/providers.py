"""LLM providers: Anthropic plus anything that speaks the OpenAI chat-completions API.

Built in: anthropic, gemini, groq, nvidia, openai, openrouter, ollama (local). More can be added
in flins.yaml with a base_url and the name of the environment variable holding the key.

For each job (triage, draft, redflag) flins.yaml lists providers in order of preference. Entries
whose provider has no key are skipped; the gateway falls through the rest on errors.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Literal

import httpx

from flins.llm.gateway import AnthropicBackend, Attempt, Backend, LLMError, RawResponse
from flins.llm.local import LocalModels
from flins.llm.prompts import Prompt

JsonMode = Literal["json_schema", "json_object", "prompt"]
ROLES = ("triage", "draft", "redflag")  # needed for WhatsApp handling
# Optional jobs. Without their own route list in flins.yaml they borrow another job's:
#   intent  (understanding commands)        -> triage   (small and fast)
#   chat    (talking with you)              -> draft
#   reason  (long / planning-style requests) -> chat -> draft
#   summary (conversation summaries)        -> triage
#   code    (programming questions)          -> reason -> chat -> draft
OPTIONAL_ROLES = ("intent", "chat", "reason", "code", "summary")
ROLE_FALLBACK = {
    "intent": "triage",
    "chat": "draft",
    "reason": "chat",
    "code": "reason",
    "summary": "triage",
}


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    kind: Literal["anthropic", "openai_compat"]
    base_url: str | None
    key_env: str | None  # None = no key needed (local)
    json_mode: JsonMode = "json_schema"
    reasoning_effort: bool = False  # send `reasoning_effort` (maps from the prompt's effort)
    min_max_tokens: int = 1024  # thinking models need room before the JSON starts
    timeout_s: float = 30.0  # per HTTP request
    local: bool = False  # runs on this PC (Ollama): one model in video memory at a time


BUILTIN_PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec("anthropic", "anthropic", None, "ANTHROPIC_API_KEY"),
    "gemini": ProviderSpec(
        "gemini",
        "openai_compat",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY",
        "json_schema",
        reasoning_effort=True,
        min_max_tokens=4096,
    ),
    "groq": ProviderSpec(
        "groq",
        "openai_compat",
        "https://api.groq.com/openai/v1",
        "GROQ_API_KEY",
        "json_schema",
        reasoning_effort=True,
        min_max_tokens=4096,
    ),
    "nvidia": ProviderSpec(
        "nvidia",
        "openai_compat",
        "https://integrate.api.nvidia.com/v1",
        "NVIDIA_API_KEY",
        "prompt",
        # Hosted reasoning models think for ~2k tokens before the JSON and can take ~20 s.
        min_max_tokens=8192,
        timeout_s=60.0,
    ),
    "openai": ProviderSpec(
        "openai", "openai_compat", "https://api.openai.com/v1", "OPENAI_API_KEY", "json_schema"
    ),
    "openrouter": ProviderSpec(
        "openrouter",
        "openai_compat",
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        "json_schema",
    ),
    "ollama": ProviderSpec(
        "ollama",
        "openai_compat",
        "http://localhost:11434/v1",
        None,
        "json_schema",  # Ollama constrains the answer to the schema
        min_max_tokens=2048,
        timeout_s=180.0,  # the first call after a model switch loads several GB from disk
        local=True,
    ),
}

# Used when flins.yaml has no llm.roles: Anthropic only, as in M3.
DEFAULT_ROLES: dict[str, list[dict[str, Any]]] = {
    "triage": [{"provider": "anthropic", "model": "claude-haiku-5-5"}],
    "draft": [{"provider": "anthropic", "model": "claude-sonnet-5-5"}],
    "redflag": [{"provider": "anthropic", "model": "claude-haiku-5-5"}],
}

THINKING_EFFORTS = ("high", "xhigh", "max")
LOCAL_THINKING_TOKENS = 8192  # room for the thinking *and* the answer
LOCAL_NUM_CTX = 12288  # context window for local models (prompt + thinking + answer)
LOCAL_TEMPERATURE = 0.2
LOCAL_KEEP_ALIVE = "30m"  # how long an idle local model stays in video memory
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def extract_json(text: str) -> str:
    """Models without strict JSON mode sometimes wrap the object in ``` fences or prose."""
    t = _FENCE.sub("", text.strip()).strip()
    if t.startswith("{"):
        return t
    start, end = t.find("{"), t.rfind("}")
    return t[start : end + 1] if start != -1 and end > start else t


class OpenAICompatBackend:
    """Chat-completions over HTTP for Gemini, Groq, NVIDIA, OpenAI, OpenRouter, Ollama, etc."""

    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str | None,
        *,
        timeout_s: float = 20.0,
        retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        assert spec.base_url is not None
        self.name = spec.name
        self._spec = spec
        self._retries = retries
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=spec.base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout_s,
            transport=transport,
        )

    def _body(self, prompt: Prompt, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        spec = self._spec
        system = prompt.system
        if spec.json_mode != "json_schema":
            system += (
                "\n\nAnswer with only one JSON object that matches this JSON schema. "
                "No other text, no code fences.\n" + json.dumps(schema)
            )
        body: dict[str, Any] = {
            "model": prompt.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max(prompt.max_tokens, spec.min_max_tokens),
        }
        if spec.json_mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": re.sub(r"\W", "_", prompt.name)[:64], "schema": schema},
            }
        elif spec.json_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        if spec.reasoning_effort:
            body["reasoning_effort"] = _EFFORT.get(prompt.effort, "low")
        return body

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        last: str = "unknown error"
        for attempt in range(self._retries + 1):
            try:
                resp = await self._client.post(path, json=body)
            except httpx.TimeoutException as exc:
                last = "timeout"
                if attempt == self._retries:
                    raise LLMError(last) from exc
            except httpx.HTTPError as exc:
                last = f"connection error ({type(exc).__name__})"
                if attempt == self._retries:
                    raise LLMError(last) from exc
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    if not isinstance(data, dict):
                        raise LLMError("unexpected response shape")
                    return data
                detail = _error_detail(resp)
                if resp.status_code in (401, 403):
                    raise LLMError(f"authentication failed (check {self._spec.key_env})")
                if resp.status_code == 404:
                    raise LLMError(f"model or endpoint not found: {detail}")
                if resp.status_code == 429 or resp.status_code >= 500:
                    last = f"HTTP {resp.status_code}: {detail}"
                    if attempt < self._retries:
                        wait = _retry_after(resp) or 2**attempt
                        await asyncio.sleep(min(wait, 10))
                        continue
                    raise LLMError(last)
                raise LLMError(f"HTTP {resp.status_code}: {detail}")
            await asyncio.sleep(2**attempt)
        raise LLMError(last)

    async def complete(self, *, prompt: Prompt, user: str, schema: dict[str, Any]) -> RawResponse:
        data = await self._post("chat/completions", self._body(prompt, user, schema))
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("response had no choices") from exc
        content = message.get("content") or ""
        finish = choice.get("finish_reason")
        if message.get("refusal") or finish == "content_filter":
            stop = "refusal"
        elif finish == "length":
            stop = "max_tokens"
        else:
            stop = "end_turn"
        usage = data.get("usage") or {}
        return RawResponse(
            text=extract_json(content) if isinstance(content, str) else "",
            model=str(data.get("model") or prompt.model),
            stop_reason=stop,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            provider=self.name,
        )

    async def list_models(self) -> list[str]:
        resp = await self._client.get("models")
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}: {_error_detail(resp)}")
        data = resp.json().get("data", [])
        return sorted(str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id"))

    async def aclose(self) -> None:
        await self._client.aclose()


def _error_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            return str(err.get("message", ""))[:160]
        if isinstance(data, list) and data and isinstance(data[0], dict):
            inner = data[0].get("error", {})
            return str(inner.get("message", ""))[:160]
        return str(err or data)[:160]
    except ValueError:
        return resp.text[:160]


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers.get("retry-after", ""))
    except ValueError:
        return None


# ------------------------------------------------------------------ routing


@dataclass(frozen=True)
class Route:
    provider: str
    model: str
    price: tuple[float, float] | None = None


class ConfiguredRouter:
    """Maps a prompt (triage_v1, draft_v1, redflag_v1) to the providers to try, in order."""

    def __init__(self, routes: dict[str, list[Route]], backends: dict[str, Backend]) -> None:
        self._routes = routes
        self._backends = backends

    @staticmethod
    def role_of(prompt: Prompt) -> str:
        return prompt.name.split("_v")[0]

    def routes_for(self, role: str) -> list[Route]:
        """This role's routes, or those of the role it borrows from (following the chain)."""
        seen: set[str] = set()
        while role and role not in seen:
            seen.add(role)
            routes = self._routes.get(role)
            if routes:
                return routes
            role = ROLE_FALLBACK.get(role, "")
        return []

    def chain(self, prompt: Prompt, role: str | None = None) -> list[Attempt]:
        out = []
        for r in self.routes_for(role or self.role_of(prompt)):
            backend = self._backends.get(r.provider)
            if backend is not None:
                out.append(Attempt(backend, replace(prompt, model=r.model), r.provider, r.price))
        return out

    def describe(self) -> dict[str, list[str]]:
        """For `flins doctor`: which provider/model each job will try, and which are skipped."""
        return {
            role: [
                f"{r.provider}/{r.model}" + ("" if r.provider in self._backends else " (no key)")
                for r in routes
            ]
            for role, routes in self._routes.items()
        }

    def missing_roles(self) -> list[str]:
        return [
            role
            for role in ROLES
            if not any(r.provider in self._backends for r in self._routes.get(role, []))
        ]


class LocalBackend(OpenAICompatBackend):
    """A provider on this PC. Before each call, other loaded models are unloaded so the one
    being asked fits in video memory (llm/local.py).

    Small local models "think" before answering unless told not to. Measured on an RTX 4060
    with a 4B model: thinking off ~0.5 s per answer, thinking on 15-35 s. On two test
    questions the answers were wrong without thinking and right with it. So they think only
    for the reasoning job (prompt effort "high")."""

    def __init__(
        self,
        spec: ProviderSpec,
        api_key: str | None,
        *,
        timeout_s: float = 180.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(spec, api_key, timeout_s=timeout_s, retries=0, transport=transport)
        assert spec.base_url is not None
        self.models = LocalModels(spec.base_url, transport=transport)

    async def complete(self, *, prompt: Prompt, user: str, schema: dict[str, Any]) -> RawResponse:
        """Uses Ollama's own chat API rather than the OpenAI-compatible one: only there can
        the context window be set (the default is too small for FLINS's prompt plus a model's
        thinking, which cut reasoning answers off) and thinking be switched per call."""
        await self.models.use(prompt.model)
        thinking = prompt.effort in THINKING_EFFORTS
        body: dict[str, Any] = {
            "model": prompt.model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema,  # the answer is constrained to this JSON schema
            "think": thinking,
            "keep_alive": LOCAL_KEEP_ALIVE,
            "options": {
                # The same window for every call: Ollama reloads a model when it changes.
                "num_ctx": LOCAL_NUM_CTX,
                # Low randomness for plain answers: at the default, a 4B model answered
                # "25 times 4" with "two and a half thousand". Thinking keeps the default.
                **({} if thinking else {"temperature": LOCAL_TEMPERATURE}),
                "num_predict": LOCAL_THINKING_TOKENS
                if thinking
                else max(prompt.max_tokens, self._spec.min_max_tokens),
            },
        }
        data = await self._chat(body)
        if data.get("__no_think__"):  # a model without a thinking mode: ask again plainly
            body.pop("think")
            data = await self._chat(body)
        message = data.get("message") or {}
        content = message.get("content") or ""
        return RawResponse(
            text=extract_json(content) if isinstance(content, str) else "",
            model=str(data.get("model") or prompt.model),
            stop_reason="max_tokens" if data.get("done_reason") == "length" else "end_turn",
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            provider=self.name,
        )

    async def _chat(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self.models.post("/api/chat", body)
        except httpx.TimeoutException as exc:
            raise LLMError("local model timed out") from exc
        except httpx.HTTPError as exc:
            raise LLMError("local model not reachable (is Ollama running?)") from exc
        if resp.status_code == 200:
            data = resp.json()
            if not isinstance(data, dict):
                raise LLMError("unexpected response shape")
            return data
        detail = _error_detail(resp)
        if resp.status_code == 400 and "think" in detail.lower() and "think" in body:
            return {"__no_think__": True}
        if resp.status_code == 404:
            raise LLMError(f"local model not downloaded: {detail}")
        raise LLMError(f"HTTP {resp.status_code}: {detail}")


def make_backend(spec: ProviderSpec, key: str | None) -> Backend:
    if spec.kind == "anthropic":
        return AnthropicBackend(key, timeout_s=spec.timeout_s)
    if spec.local:
        return LocalBackend(spec, key, timeout_s=spec.timeout_s)
    return OpenAICompatBackend(spec, key, timeout_s=spec.timeout_s)
