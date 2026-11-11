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
from datetime import datetime, timedelta
from typing import Protocol

from flins.assistant.chat import ChatEngine, ChatOut, Role, Summariser
from flins.assistant.context import ContextBuilder, TurnContext
from flins.assistant.conversation import ConversationStore, Session
from flins.assistant.memory import MemoryStore, NoMemory
from flins.assistant.models import SOURCES, Reply, Route
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
    "modes, stop and resume, approvals, sending a message you dictate, and what you missed."
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
            return Reply(CANT, "chat", [*stages, "reply replaced"], role, flagged)
        return Reply(out.reply.strip(), "chat", [*stages, out.kind], role)

    async def _turn_context(
        self, text: str, source: str, now: datetime, session: Session
    ) -> TurnContext:
        await self._modes.refresh()
        pending = None
        if self._approvals is not None:
            pending = len(await self._approvals.pending(now))
        memories = []
        if self._cfg.use_about_me and self._cfg.max_facts:
            memories = await self._memory.relevant(text, self._cfg.max_facts)
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
        await self._store.add(
            session, role="assistant", text=reply.text, source=source, now=now, route=reply.route
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
