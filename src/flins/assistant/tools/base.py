"""The tool layer (JARVIS phase 4, docs/milestones/J4.md).

A tool takes one string and returns text. The conversation model can only *ask* for a tool by
name; the core checks the name against this registry, applies the risk gate and runs it here.
Nothing in this package can send a message, change a mode or reach the WhatsApp pipeline.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from flins.llm.context import neutralise

# read     harmless lookups and maths: runs straight away
# private  reads your own files: runs only inside folders you approved
# write    changes something on disk: never runs without your yes to the exact content
# online   sends its input to a public service (phase 4b): off until you switch it on, and
#          only through tools/net.py
Risk = Literal["read", "private", "write", "online"]

MAX_INPUT_CHARS = 2000
MAX_RESULT_CHARS = 4000
TIMEOUT_S = 6.0


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    text: str


Run = Callable[[str, datetime], Awaitable[ToolResult]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str  # one line for the model: what it's for
    usage: str  # what to put in the input string
    risk: Risk
    run: Run
    # True when the result is content FLINS didn't write (a file, later a web page). The
    # core then treats the rest of the turn as tainted: no more tools, no memory suggestion.
    outside_content: bool = False
    # For write tools: the yes/no question shown before anything is written.
    confirm: Callable[[str], str] | None = None
    timeout_s: float | None = None  # its own limit (online tools); None = the tool box's

    def line(self) -> str:
        return f"{self.name}: {self.description} Input: {self.usage}"


def fail(text: str) -> ToolResult:
    return ToolResult(False, text)


def ok(text: str) -> ToolResult:
    return ToolResult(True, text)


@dataclass(frozen=True)
class Ran:
    tool: Tool
    result: ToolResult
    ms: int


class ToolBox:
    def __init__(self, tools: list[Tool], *, timeout_s: float = TIMEOUT_S) -> None:
        self._tools = {t.name: t for t in tools}
        self._timeout = timeout_s

    def __bool__(self) -> bool:
        return bool(self._tools)

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools.values())

    def get(self, name: str | None) -> Tool | None:
        return self._tools.get((name or "").strip().lower())

    def lines(self) -> list[str]:
        return [t.line() for t in self._tools.values()]

    async def run(self, tool: Tool, arg: str, now: datetime) -> Ran:
        """Run one tool. Never raises: a crash or a timeout becomes a failed result."""
        started = time.monotonic()
        arg = (arg or "").strip()
        if len(arg) > MAX_INPUT_CHARS:
            result = fail(f"The input is too long (over {MAX_INPUT_CHARS} characters).")
        else:
            try:
                limit = tool.timeout_s or self._timeout
                result = await asyncio.wait_for(tool.run(arg, now), limit)
            except TimeoutError:
                result = fail(f"{tool.name} took too long and was stopped.")
            except Exception as exc:  # a tool bug must not break the conversation
                result = fail(f"{tool.name} failed ({type(exc).__name__}).")
        text = result.text.strip()
        if len(text) > MAX_RESULT_CHARS:
            text = text[: MAX_RESULT_CHARS - 20].rstrip() + "\n… (cut short)"
        return Ran(tool, ToolResult(result.ok, text), int((time.monotonic() - started) * 1000))


def result_block(ran: Ran, arg: str) -> str:
    """What the model gets back. Everything is neutralised, so a file can't open or close a
    block, and outside content is labelled as such."""
    kind = ' content="outside"' if ran.tool.outside_content else ""
    status = "ok" if ran.result.ok else "failed"
    return (
        f'<tool_result tool="{ran.tool.name}" status="{status}"{kind}>\n'
        f"input: {neutralise(arg)[:300]}\n"
        f"{neutralise(ran.result.text)}\n"
        "</tool_result>"
    )
