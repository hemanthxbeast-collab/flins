"""Turns commands (CLI, WhatsApp self-chat, voice) into actions and reply text.

Tier A: fixed phrases (core/grammar.py), no AI. Tier B: free-form text understood by the
LLM (core/intents.py), only for voice and self-chat, and anything it would *do* (send,
change mode, approve) is read back first and waits for "yes".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite

from flins.core.approvals import ApprovalService, summarise
from flins.core.audit import AuditLog
from flins.core.clock import Clock
from flins.core.events import Event, EventBus
from flins.core.grammar import (
    ApprovalCommand,
    ModeCommand,
    ParseError,
    StatusCommand,
    parse,
    parse_time,
)
from flins.core.intents import Intent, IntentParser, find_contacts
from flins.core.levels import Level
from flins.core.modes import ModeEvent, ModeManager, TransitionError
from flins.core.outbox import SELF_PREFIX, OutboxError, enqueue
from flins.core.policy_schema import Policy
from flins.llm.gateway import LLMError

NO_APPROVALS = "Approvals only work while `flins run` is running."
CONFIRM_FOR = timedelta(seconds=60)
YES = {
    "yes",
    "y",
    "yeah",
    "yep",
    "yup",
    "haan",
    "ha",
    "han",
    "ok",
    "okay",
    "confirm",
    "do it",
    "sure",
    "go ahead",
    "send it",
    "yes please",
    "yes send it",
}
NO = {"no", "n", "nope", "nah", "nahi", "cancel", "don't", "dont", "no thanks", "leave it"}
HELP = (
    "Sorry, I didn't get that. Try 'working mood till 6', 'what did I miss', "
    "'approve 3', or 'tell Rahul I'll call at 8'."
)


@dataclass
class _Confirmation:
    question: str
    run: Callable[[], Awaitable[str]]
    expires: datetime


def _plain(text: str) -> str:
    return " ".join(text.lower().strip().strip(".!?").split())


class CommandRouter:
    def __init__(
        self,
        modes: ModeManager,
        policy: Policy,
        clock: Clock,
        approvals: ApprovalService | None = None,
        *,
        intents: IntentParser | None = None,
        conn: aiosqlite.Connection | None = None,
    ) -> None:
        self._modes = modes
        self._policy = policy
        self._clock = clock
        self._tz = ZoneInfo(policy.user.timezone)
        self._approvals = approvals
        self._intents = intents
        self._conn = conn
        self._pending: dict[str, _Confirmation] = {}

    async def handle_text(self, text: str, *, source: str) -> str | None:
        """Run a command. Returns reply text, or None if `text` isn't a command."""
        now = self._clock.now()
        waiting = self._pending.pop(source, None)
        if waiting is not None and waiting.expires > now:
            answer = _plain(text)
            if answer in YES:
                return await waiting.run()
            if answer in NO:
                return "Okay, I won't."
            # Anything else: the question lapses and the new text is handled normally.

        cmd = parse(text, now=now, tz=self._tz)
        if cmd is None:
            return await self._tier_b(text, source)
        if isinstance(cmd, ParseError):
            return cmd.message
        if isinstance(cmd, StatusCommand):
            await self._modes.refresh()
            return await self._modes.status_text()
        if isinstance(cmd, ApprovalCommand):
            return await self._approval(cmd, source)
        assert isinstance(cmd, ModeCommand)
        return await self._mode(cmd.event, source, until=cmd.until, profile=cmd.profile)

    async def _mode(
        self,
        event: ModeEvent,
        source: str,
        *,
        until: datetime | None = None,
        profile: str | None = None,
    ) -> str:
        try:
            result = await self._modes.apply(event, source=source, until=until, profile=profile)
        except TransitionError as exc:
            return str(exc)
        return result.readback

    async def _approval(self, cmd: ApprovalCommand, source: str) -> str:
        if self._approvals is None:
            return NO_APPROVALS
        now = self._clock.now()
        pending = await self._approvals.pending(now)
        if cmd.op == "list":
            return summarise(pending)
        number = cmd.number
        if number is None:
            if len(pending) != 1:
                return summarise(pending) if pending else "Nothing is waiting for approval."
            number = pending[0].short_id
        action = {"approve": "approve", "reject": "reject", "edit": "edit"}[cmd.op]
        return await self._approvals.decide(
            action, now=now, channel=source, short_id=number, text=cmd.text
        )

    # ------------------------------------------------------------------ Tier B

    def _confirm(self, source: str, question: str, run: Callable[[], Awaitable[str]]) -> str:
        self._pending[source] = _Confirmation(question, run, self._clock.now() + CONFIRM_FOR)
        return f"{question} Say yes or no."

    async def _tier_b(self, text: str, source: str) -> str | None:
        if self._intents is None or source not in ("voice", "self_chat"):
            return None
        try:
            intent = await self._intents.parse(text)
        except LLMError:
            return None if source == "self_chat" else "Sorry, I can't understand that right now."
        a = intent.action
        if a == "unknown":
            return None if source == "self_chat" else HELP
        if a == "stop":  # stopping is always safe: no confirmation
            return await self._mode(ModeEvent.KILL, source)
        if a == "status":
            await self._modes.refresh()
            return await self._modes.status_text()
        if a == "summary":
            return await self._modes.summary_text()
        if a == "pending":
            return await self._approval(ApprovalCommand("list"), source)
        if a == "reject" and intent.number:
            return await self._approval(ApprovalCommand("reject", intent.number), source)
        if a in ("approve", "edit"):
            return await self._confirm_approval(intent, source)
        if a in ("mode", "extend", "done", "resume"):
            return self._confirm_mode(intent, source)
        if a == "send":
            return await self._confirm_send(intent, source)
        return None if source == "self_chat" else HELP

    def _confirm_mode(self, intent: Intent, source: str) -> str:
        now = self._clock.now()
        until: datetime | None = None
        if intent.duration_or_time:
            until = parse_time(intent.duration_or_time, now, self._tz)
            if until is None:
                return (
                    f"I didn't get the time '{intent.duration_or_time}'. "
                    "Try 'till 6pm' or 'for 2 hours'."
                )
        if intent.action == "extend":
            if until is None:
                return "Until when? Try 'extend till 7pm'."
            event, what = ModeEvent.EXTEND, f"Keep the mode on until {self._hm(until)}?"
        elif intent.action == "done":
            event, what = ModeEvent.EXIT_TO_NORMAL, "Go back to normal mode?"
        elif intent.action == "resume":
            event, what = ModeEvent.RESUME, "Resume FLINS?"
        else:
            event = {
                "working": ModeEvent.ENTER_WORKING,
                "focus": ModeEvent.ENTER_FOCUS,
                "away": ModeEvent.ENTER_AWAY,
            }[intent.mode or "working"]
            name = (intent.mode or "working").capitalize()
            what = f"Switch to {name} mode" + (f" until {self._hm(until)}?" if until else "?")

        async def run() -> str:
            return await self._mode(event, source, until=until)

        return self._confirm(source, what, run)

    async def _confirm_approval(self, intent: Intent, source: str) -> str:
        if self._approvals is None:
            return NO_APPROVALS
        if intent.number is None:
            return "Which draft? Say 'approve' and its number."
        drafts = {p.short_id: p for p in await self._approvals.pending(self._clock.now())}
        p = drafts.get(intent.number)
        if p is None:
            return f"There's no draft #{intent.number} waiting."
        if intent.action == "edit":
            if not intent.text:
                return "What should it say instead?"
            text = intent.text
            cmd = ApprovalCommand("edit", intent.number, text)
        else:
            text = p.draft
            cmd = ApprovalCommand("approve", intent.number)

        async def run() -> str:
            return await self._approval(cmd, source)

        return self._confirm(source, f'Send to {p.who}: "{text}"?', run)

    async def _confirm_send(self, intent: Intent, source: str) -> str:
        if self._conn is None:
            return "Sending messages only works while `flins run` is running."
        if not intent.contact:
            return "Who should I send it to?"
        matches = await find_contacts(self._conn, intent.contact)
        if not matches:
            return f"I couldn't find a contact called {intent.contact}."
        if len(matches) > 1:
            names = ", ".join(m.name for m in matches[:4])
            return f"Which one: {names}? Say the full name."
        who = matches[0]
        if not intent.text:
            return f"What should I tell {who.name}?"
        user = self._policy.user.name
        signature = self._policy.user.signature
        # FLINS speaks *for* you, never *as* you.
        body = f"{user} asked me to tell you: {intent.text.strip()}"
        final = f"{body} {signature}".strip()

        async def run() -> str:
            assert self._conn is not None
            try:
                await enqueue(
                    self._conn,
                    chat_jid=who.jid,
                    text=final,
                    level=int(Level.DRAFT),
                    now=self._clock.now(),
                )
            except OutboxError as exc:
                return f"Can't send to {who.name}: {exc}"
            note = " (dry run is on: it will be recorded, not sent)" if self._policy.dry_run else ""
            return f"Sending to {who.name}{note}."

        return self._confirm(source, f'Send to {who.name}: "{body}"?', run)

    def _hm(self, dt: datetime) -> str:
        return dt.astimezone(self._tz).strftime("%H:%M")


class SelfChatCommands:
    """Listens for messages the user writes in their own 'Message yourself' chat."""

    def __init__(self, conn: aiosqlite.Connection, bus: EventBus, router: CommandRouter) -> None:
        self._conn = conn
        self._bus = bus
        self._audit = AuditLog(conn)
        self._router = router
        bus.subscribe("wa.message", self._on_message)

    async def _on_message(self, event: Event) -> None:
        d = event.data
        if not d.get("self_chat") or d.get("upsert_type") != "notify":
            return  # only live messages: never replay old self-chat history as commands
        async with self._conn.execute(
            "SELECT text FROM messages WHERE id = ?", (d["message_id"],)
        ) as cur:
            row = await cur.fetchone()
        if row is None or not row[0]:
            return
        if row[0].lstrip().startswith(SELF_PREFIX.strip()):
            return  # FLINS's own note (see core/selfchat.py): never a command
        async with self._conn.execute(
            "SELECT 1 FROM outbox WHERE wa_msg_id = (SELECT wa_msg_id FROM messages WHERE id = ?)",
            (d["message_id"],),
        ) as cur:
            if await cur.fetchone() is not None:
                return  # something FLINS sent itself
        reply = await self._router.handle_text(row[0], source="self_chat")
        if reply is None:
            return
        await self._audit.record(
            "commands", "command.self_chat", {"message_id": d["message_id"], "reply": reply}
        )
        # SelfChatNotifier (M5) sends this back into the self-chat; flins run also prints it.
        await self._bus.publish("command.reply", {"source": "self_chat", "text": reply})
