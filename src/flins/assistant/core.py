"""The assistant core: every message you send FLINS, on any channel, goes through `handle`.

    input -> commands (fixed phrases first, no AI; then intent + confirmation)
          -> is this for me? (self-chat notes aren't)
          -> context builder -> conversation model (no tools) -> reply check
          -> conversation memory -> audit -> reply

Safety properties, each with a test:
- "stop", "resume", "status" and the other fixed phrases never reach a model.
- The conversation model can't act. A reply claiming an action is replaced with an honest one.
- A failure is always said out loud (AI down, budget used up), never swallowed.
- Nothing typed in your self-chat that isn't for FLINS is answered or stored.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Protocol

from flins.assistant.chat import ChatEngine, ChatOut, Role, Summariser
from flins.assistant.context import ContextBuilder, TurnContext
from flins.assistant.conversation import ConversationStore, Session
from flins.assistant.memory import MemoryStore, NoMemory, SqlMemory, is_secret
from flins.assistant.memory_commands import MemoryCommands
from flins.assistant.models import SOURCES, MemoryItem, Reply, Route
from flins.core.approvals import ApprovalService
from flins.core.audit import AuditLog
from flins.core.clock import Clock
from flins.core.policy_schema import Policy
from flins.llm.gateway import LLMError, LLMGateway, LLMRefusal
from flins.settings import AssistantConfig

# "FLINS, ..." / "hey flins ..." at the start: you're talking to it.
ADDRESSED = re.compile(r"^\W*(?:(?:hey|hi|ok|okay)[\s,]+)?flins\b", re.IGNORECASE)
# A reply that says FLINS did, will do or will remember something. The conversation model can't.
ACTION_CLAIM = re.compile(
    r"\bI(?:\s+have|['’]ve|\s+will|['’]ll)\s+(?:just\s+|now\s+|also\s+)?"
    r"(?:sen[dt]|set|schedul\w+|creat\w+|add(?:ed)?|remind\w*|messag\w+|book\w*|sav\w+|"
    r"remember\w*|not(?:e|ed)|delet\w+|forg[eo]t\w*|cancel\w*|updat\w+|"
    r"keep (?:that|this|it) in mind)\b"
    r"|\breminder (?:is |has been )?set\b|\bconsider it done\b|^\W*(?:done|noted|saved)\b",
    re.IGNORECASE,
)
SUMMARY_SLACK = 6  # fold old turns into the summary once this many pile up beyond history_turns

CANT = (
    "I can't do that yet. For now I can talk things through and run my built-in commands: "
    "modes, stop and resume, approvals, sending a message you dictate, what you missed, and "
    "remembering things when you say 'remember that …'."
)
NO_AI = (
    "I can only run commands right now, because no AI model is set up (see `flins llm status`). "
    "Try 'working mood till 6', 'status' or 'stop'."
)
BUDGET = "Today's AI budget is used up, so I can only run commands until tomorrow."
DECLINED = "The AI model declined to answer that one."


def ai_down(why: str) -> str:
    return (
        f"I couldn't get an answer from the AI model ({why}). "
        "Commands like 'stop', 'status' and 'working mood till 6' still work."
    )


class Commands(Protocol):
    """The command executor (core/commands.py CommandRouter in conversational mode)."""

    async def handle_text(self, text: str, *, source: str) -> str | None: ...
    def awaiting(self, source: str) -> bool: ...
    def awaiting_answer(self, source: str) -> bool: ...
    def confirm(self, source: str, question: str, run: Callable[[], Awaitable[str]]) -> str: ...


class ModeView(Protocol):
    async def refresh(self) -> object: ...
    async def status_text(self) -> str: ...


class Assistant:
    def __init__(
        self,
        commands: Commands,
        store: ConversationStore,
        modes: ModeView,
        policy: Policy,
        cfg: AssistantConfig,
        clock: Clock,
        audit: AuditLog,
        *,
        chat: ChatEngine | None = None,
        summariser: Summariser | None = None,
        gateway: LLMGateway | None = None,
        memory: MemoryStore | None = None,
        approvals: ApprovalService | None = None,
        stored: SqlMemory | None = None,
        memory_commands: MemoryCommands | None = None,
    ) -> None:
        self._commands = commands
        self._store = store
        self._modes = modes
        self._cfg = cfg
        self._clock = clock
        self._audit = audit
        self._chat = chat
        self._summariser = summariser
        self._gateway = gateway
        self._memory: MemoryStore = memory or NoMemory()
        self._approvals = approvals
        self._stored = stored  # long-term memory you can add to; None = not available
        self._memory_cmds = memory_commands
        self._last_used: dict[str, list[MemoryItem]] = {}  # per channel: memories last shown
        self._last_suggest: datetime | None = None
        self._context = ContextBuilder(policy, cfg)

    @property
    def store(self) -> ConversationStore:
        return self._store

    async def handle_text(self, text: str, *, source: str) -> str | None:
        """Drop-in for the old router interface (voice loop, self-chat listener)."""
        return (await self.handle(text, source=source)).text

    async def handle(self, text: str, *, source: str) -> Reply:
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}")
        started = time.monotonic()
        now = self._clock.now()
        text = text.strip()
        if not text:
            return Reply(None, "silent", ["empty"])

        # 0. Memory commands ("remember that ...", "forget that", "why do you know that"):
        #    fixed phrases, no model. Not while FLINS asked an open question (then your words
        #    are its answer), and in your self-chat only when you're talking to it.
        if (
            self._memory_cmds is not None
            and not self._commands.awaiting_answer(source)
            and (source != "self_chat" or await self._for_me(text, source, now))
        ):
            said = await self._memory_cmds.handle(
                text, source, now, self._last_used.get(source, [])
            )
            if said is not None:
                reply = Reply(said, "command", ["memory command"])
                await self._finish(text, source, now, reply, started)
                return reply

        # 1. Commands. Fixed phrases are matched without any model, so the kill switch never
        #    depends on one. Free-form commands are read back and wait for "yes".
        answer = await self._commands.handle_text(text, source=source)
        if answer is not None:
            reply = Reply(answer, "command", ["command"])
            await self._finish(text, source, now, reply, started)
            return reply

        # 2. Not a command. In your self-chat, most such messages are notes to yourself.
        if not await self._for_me(text, source, now):
            return Reply(None, "silent", ["not addressed"])

        # 3. Conversation.
        reply = await self._converse(text, source, now)
        await self._finish(text, source, now, reply, started)
        return reply

    # ------------------------------------------------------------------ steps

    async def _for_me(self, text: str, source: str, now: datetime) -> bool:
        if source != "self_chat":
            return True  # voice, CLI and the dashboard chat are always addressed to FLINS
        mode = self._cfg.self_chat
        if mode == "off":
            return False
        if mode == "all" or ADDRESSED.match(text):
            return True
        last = await self._store.last_reply_at(source)
        window = timedelta(seconds=self._cfg.follow_window_s)
        return last is not None and now - last <= window

    def _role(self, text: str, source: str) -> Role:
        """Cheap and fast by default; the reasoning model for long or planning-style requests.
        Voice always gets the fast one (you're waiting for it to speak)."""
        r = self._cfg.reasoning
        if source == "voice":
            return "chat"
        low = text.lower()
        if len(text) >= r.min_chars or any(cue in low for cue in r.cues):
            return "reason"
        return "chat"

    async def _converse(self, text: str, source: str, now: datetime) -> Reply:
        stages = ["chat"]
        if not self._cfg.enabled or self._chat is None:
            return Reply(NO_AI, "error", [*stages, "no model"])
        if self._gateway is not None and not await self._gateway.budget_ok():
            return Reply(BUDGET, "error", [*stages, "budget"])
        session = await self._store.session(now)
        context = self._context.build(await self._turn_context(text, source, now, session))
        role = self._role(text, source)
        try:
            out = await self._chat.reply(context, role)
        except LLMRefusal:
            return Reply(DECLINED, "error", [*stages, "refused"], role)
        except LLMError as exc:
            return Reply(ai_down(str(exc)[:120]), "error", [*stages, "model failed"], role)
        flagged = self._check(out)
        if flagged:
            # The model said it did or will remember something. It can't, so its words are
            # dropped. If what it wanted was to keep a fact, ask you instead (the honest
            # version of the same thing); otherwise say plainly that it can't.
            question = await self._suggest_memory(out, source, now)
            if question is not None:
                steps = [*stages, "reply replaced", "memory suggested"]
                return Reply(question, "chat", steps, role, flagged)
            return Reply(CANT, "chat", [*stages, "reply replaced"], role, flagged)
        said = out.reply.strip()
        stages.append(out.kind)
        question = await self._suggest_memory(out, source, now)
        if question is not None:
            said = f"{said}\n{question}"
            stages.append("memory suggested")
        return Reply(said, "chat", stages, role)

    async def _suggest_memory(self, out: ChatOut, source: str, now: datetime) -> str | None:
        """The model may *propose* a memory. FLINS asks you; only "yes" saves it, and exactly
        the text you were shown."""
        m = self._cfg.memory
        fact = " ".join((out.remember or "").split())
        if (
            self._stored is None
            or not (m.enabled and m.suggest)
            or not 5 <= len(fact) <= 300
            or (out.remember_importance or 0.0) < m.suggest_min_importance
            or self._commands.awaiting(source)
            or is_secret(fact)
        ):
            return None
        cooldown = timedelta(minutes=m.suggest_cooldown_minutes)
        if self._last_suggest is not None and now - self._last_suggest < cooldown:
            return None
        if not await self._stored.enabled() or await self._stored.duplicate_of(fact):
            return None
        stored, kind, importance = self._stored, out.remember_kind, out.remember_importance

        async def run() -> str:
            result = await stored.save(
                fact,
                source="suggested",
                channel=source,
                now=self._clock.now(),
                kind=kind,
                importance=importance,
            )
            if result.status == "refused":
                return f"I won't save that: {result.reason}."
            return "Okay, I'll remember that."

        self._last_suggest = now
        return self._commands.confirm(source, f'Should I remember this: "{fact}"?', run)

    async def _turn_context(
        self, text: str, source: str, now: datetime, session: Session
    ) -> TurnContext:
        await self._modes.refresh()
        pending = None
        if self._approvals is not None:
            pending = len(await self._approvals.pending(now))
        memories = await self._memory.relevant(text, self._cfg.max_facts)
        self._last_used[source] = memories  # for "why do you know that?"
        return TurnContext(
            text=text,
            source=source,
            now=now,
            state_line=await self._modes.status_text(),
            pending_approvals=pending,
            memories=memories,
            summary=session.summary,
            earlier=session.earlier,
            turns=await self._store.recent(session, self._cfg.history_turns),
        )

    @staticmethod
    def _check(out: ChatOut) -> str | None:
        """Result validation: the model can't act, so it must not say it did."""
        if not out.reply.strip():
            return "empty reply"
        if ACTION_CLAIM.search(out.reply):
            return "claims an action"
        if out.claims_action and out.kind != "cannot":
            return "model reported claiming an action"
        return None

    async def _finish(
        self, text: str, source: str, now: datetime, reply: Reply, started: float
    ) -> None:
        """Memory update + audit. The audit row has no message text (it's in the turns)."""
        session = await self._store.session(now)
        await self._store.add(session, role="user", text=text, source=source, now=now)
        assert reply.text is not None
        stored_as = "memory" if "memory command" in reply.stages else reply.route
        await self._store.add(
            session, role="assistant", text=reply.text, source=source, now=now, route=stored_as
        )
        await self._maybe_summarise(session)
        route: Route = reply.route
        await self._audit.record(
            "assistant",
            "assistant.turn",
            {
                "source": source,
                "route": route,
                "stages": reply.stages,
                "model_role": reply.model_role,
                "prompt": self._chat.prompt_version if self._chat and route == "chat" else None,
                "flagged": reply.flagged,
                "session": session.id,
                "chars_in": len(text),
                "chars_out": len(reply.text),
                "ms": int((time.monotonic() - started) * 1000),
            },
        )

    async def _maybe_summarise(self, session: Session) -> None:
        if self._summariser is None:
            return
        keep = self._cfg.history_turns
        turns = await self._store.unsummarised(session)
        excess = len(turns) - keep - SUMMARY_SLACK
        if excess <= 0 or ((excess - 1) // 2) % 3:
            return  # not enough yet, or a recent attempt failed: retry every third exchange
        old = turns[:-keep]
        try:
            summary = await self._summariser.summarise(
                session.summary, old, assistant=self._cfg.name
            )
        except LLMError as exc:
            # Not fatal: the model just sees fewer old turns. Recorded, so it isn't silent.
            await self._audit.record(
                "assistant", "assistant.summary_failed", {"session": session.id, "why": str(exc)}
            )
            return
        if summary:
            await self._store.set_summary(session, summary, old[-1].id)
