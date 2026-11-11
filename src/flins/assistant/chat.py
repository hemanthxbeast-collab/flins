"""The two model calls of the conversation engine: answer a turn, and fold old turns into a
summary. Both go through llm/gateway.py (timeouts, fallbacks, metering, validated JSON).

The conversation model has no tools. It can only return text, which the core checks before
anything reaches you.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from flins.assistant.models import Turn
from flins.llm.context import neutralise
from flins.llm.gateway import LLMGateway
from flins.llm.prompts import Prompt

Role = Literal["chat", "reason"]


class ChatOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str = Field(max_length=6000)
    kind: Literal["answer", "clarify", "cannot"]
    claims_action: bool  # the reply says or implies FLINS did / will do / will remember something
    # A lasting fact, preference or decision the user just stated that may be worth keeping.
    # Only a proposal: FLINS asks the user, and saves it only on "yes".
    remember: str | None = Field(default=None, max_length=400)
    remember_kind: Literal["semantic", "episodic", "relationship"] | None = None
    remember_importance: float | None = Field(default=None, ge=0, le=1)


CHAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "kind": {"type": "string", "enum": ["answer", "clarify", "cannot"]},
        "claims_action": {"type": "boolean"},
        "remember": {"type": ["string", "null"]},
        "remember_kind": {
            "type": ["string", "null"],
            "enum": ["semantic", "episodic", "relationship", None],
        },
        "remember_importance": {"type": ["number", "null"]},
    },
    "required": [
        "reply",
        "kind",
        "claims_action",
        "remember",
        "remember_kind",
        "remember_importance",
    ],
    "additionalProperties": False,
}


class ChatEngine:
    def __init__(self, gateway: LLMGateway, prompt: Prompt) -> None:
        self._gw = gateway
        self._prompt = prompt

    @property
    def prompt_version(self) -> str:
        return self._prompt.version

    async def reply(self, context: str, role: Role = "chat") -> ChatOut:
        prompt = self._prompt
        if role == "reason":  # same instructions, more thinking room, the stronger model
            prompt = replace(prompt, effort="high", max_tokens=max(prompt.max_tokens, 4096))
        return await self._gw.json(
            prompt=prompt, user=context, schema=CHAT_SCHEMA, out=ChatOut, role=role
        )


class SummaryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(max_length=2000)


SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


class Summariser:
    def __init__(self, gateway: LLMGateway, prompt: Prompt) -> None:
        self._gw = gateway
        self._prompt = prompt

    async def summarise(self, previous: str | None, turns: list[Turn], *, assistant: str) -> str:
        lines = [
            f"{'User' if t.role == 'user' else neutralise(assistant)}: {neutralise(t.text)[:1200]}"
            for t in turns
        ]
        user = (
            f"<previous_summary>\n{neutralise(previous) or '(none)'}\n</previous_summary>\n"
            "<turns>\n" + "\n".join(lines) + "\n</turns>"
        )
        out = await self._gw.json(
            prompt=self._prompt, user=user, schema=SUMMARY_SCHEMA, out=SummaryOut
        )
        return out.summary.strip()
