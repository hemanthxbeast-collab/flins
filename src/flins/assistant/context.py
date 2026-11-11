"""Builds what the conversation model sees for one turn (JARVIS_ARCHITECTURE §2, "context
budget"): persona, who you are, the time, FLINS's state, what it knows, the summary of the
older conversation and the last few turns. Never the whole history.

Every value is neutralised (`<` and `>` replaced), so nothing you typed, and nothing stored, can
open or close a block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from flins.assistant.models import MemoryItem, Turn
from flins.core.policy_schema import Policy
from flins.llm.context import neutralise
from flins.settings import AssistantConfig

MAX_TURN_CHARS = 1200  # one long paste shouldn't crowd out everything else
MAX_MESSAGE_CHARS = 6000


def _clip(text: str, n: int) -> str:
    t = neutralise(text)
    return t if len(t) <= n else t[: n - 1] + "…"


@dataclass(frozen=True)
class TurnContext:
    """Everything that varies per turn. Collected by the core, rendered here."""

    text: str
    source: str
    now: datetime
    state_line: str  # e.g. "Working mode until 18:00."
    pending_approvals: int | None  # None = not available in this process
    memories: list[MemoryItem]
    summary: str | None
    earlier: str | None  # summary of the previous session
    turns: list[Turn]
    tasks: list[str] = field(default_factory=list)  # open tasks, soonest first
    tools: list[str] = field(default_factory=list)  # tools the model may ask for


class ContextBuilder:
    def __init__(self, policy: Policy, cfg: AssistantConfig) -> None:
        self._policy = policy
        self._cfg = cfg
        self._tz = ZoneInfo(policy.user.timezone)

    def build(self, c: TurnContext) -> str:
        cfg, user = self._cfg, self._policy.user
        local = c.now.astimezone(self._tz)
        persona = [f"name: {neutralise(cfg.name)}", f"style: {cfg.style}", f"humor: {cfg.humor}"]
        persona += [f"note: {_clip(n, 200)}" for n in cfg.notes]
        state = [f"mode: {neutralise(c.state_line)}"]
        if c.pending_approvals is not None:
            state.append(f"drafts waiting for approval: {c.pending_approvals}")
        blocks = [
            "<assistant>\n" + "\n".join(persona) + "\n</assistant>",
            "<user>\n"
            f"name: {neutralise(user.name)}\n"
            f"timezone: {neutralise(user.timezone)}\n"
            f"languages: {', '.join(neutralise(x) for x in user.languages)}\n"
            "</user>",
            f"<now>{local:%A %d %B %Y, %H:%M}</now>",
            f"<channel>{neutralise(c.source)}</channel>",
            "<state>\n" + "\n".join(state) + "\n</state>",
        ]
        profile = [m for m in c.memories if m.origin == "profile"]
        stored = [m for m in c.memories if m.origin == "memory"]
        if profile:
            by_source: dict[str, list[str]] = {}
            for m in profile:
                by_source.setdefault(m.source, []).append(f"- {_clip(m.content, 300)}")
            for source, lines in by_source.items():
                blocks.append(
                    f'<what_you_know source="{neutralise(source)}">\n'
                    + "\n".join(lines)
                    + "\n</what_you_know>"
                )
        if stored:
            # Each memory with where it came from, so the model can say so and never guess.
            lines = [f"- {_clip(m.content, 500)} ({neutralise(m.source)})" for m in stored]
            blocks.append("<memories>\n" + "\n".join(lines) + "\n</memories>")
        if c.tasks:
            lines = [f"- {_clip(t, 260)}" for t in c.tasks]
            blocks.append("<tasks>\n" + "\n".join(lines) + "\n</tasks>")
        if c.tools:
            lines = [f"- {_clip(t, 500)}" for t in c.tools]
            blocks.append("<tools>\n" + "\n".join(lines) + "\n</tools>")
        if c.earlier:
            blocks.append(
                f"<earlier_conversation>\n{_clip(c.earlier, 1500)}\n</earlier_conversation>"
            )
        if c.summary:
            blocks.append(f"<summary>\n{_clip(c.summary, 1500)}\n</summary>")
        if c.turns:
            name = neutralise(cfg.name)
            lines = [
                f"[{t.ts.astimezone(self._tz):%H:%M}] "
                f"{neutralise(user.name) if t.role == 'user' else name}: "
                f"{_clip(t.text, MAX_TURN_CHARS)}"
                for t in c.turns
            ]
            blocks.append("<conversation>\n" + "\n".join(lines) + "\n</conversation>")
        blocks.append(f"<message>\n{_clip(c.text, MAX_MESSAGE_CHARS)}\n</message>")
        return "\n".join(blocks)
