"""Turns commands (CLI, WhatsApp self-chat, voice) into actions and reply text.

Tier A: fixed phrases (core/grammar.py), no AI. Tier B: free-form text understood by the
LLM (core/intents.py), only for voice and self-chat, and anything it would *do* (send,
change mode, approve) is read back first and waits for "yes".
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol
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
from flins.core.intents import (
    ContactMatch,
    Intent,
    IntentParser,
    contact_names,
    find_contacts,
)
from flins.core.levels import Level
from flins.core.modes import ModeEvent, ModeManager, TransitionError
from flins.core.outbox import SELF_PREFIX, OutboxError, enqueue
from flins.core.policy_schema import Policy
from flins.llm.gateway import LLMError
from flins.skilldev.workspace import write_request
from flins.skills.engine import SkillEngine
from flins.skills.models import SkillInput

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
NO = {
    "no",
    "n",
    "nope",
    "nah",
    "nahi",
    "cancel",
    "don't",
    "dont",
    "no thanks",
    "leave it",
    "never mind",
    "nevermind",
    "forget it",
    "skip it",
    "no need",
    "rehne do",
}
OWNER_B = ("voice", "self_chat")  # channels with free-form commands before the assistant core
HELP = (
    "Sorry, I didn't get that. Try 'working mood till 6', 'what did I miss', "
    "'approve 3', or 'tell Rahul I'll call at 8'."
)


@dataclass
class _Confirmation:
    question: str
    run: Callable[[], Awaitable[str]]
    expires: datetime


@dataclass
class _FollowUp:
    """A question FLINS asked that your next words answer."""

    kind: Literal["message", "contact", "time"]
    expires: datetime
    contact: ContactMatch | None = None  # "message": who it's for
    text: str | None = None  # "contact": the message waiting for a recipient


# "make a skill that replies to Mom's 'did you eat' with a warm message" (SPEC §10b)
SKILL_REQUEST = re.compile(
    r"^(?:hey flins,?\s*)?(?:please\s+)?(?:can you\s+)?(?:make|create|build|write)\s+"
    r"(?:me\s+)?an?\s+(?:new\s+)?skill\s+(?:that|which|to|for|so that)\s+(.{6,})$",
    re.IGNORECASE | re.DOTALL,
)


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
        skills: SkillEngine | None = None,
        skill_requests: Path | None = None,
        conversational: bool = False,
        can_send: bool = True,
    ) -> None:
        # conversational: used under the assistant core. Free-form commands are understood on
        # every owner channel, and "not a command" is reported as None (the core then talks)
        # instead of the help text.
        self._conversational = conversational
        # can_send=False: this process has no outbox worker (`flins ask`, the standalone
        # dashboard), so it must not queue messages that would go out much later.
        self._can_send = can_send
        self._modes = modes
        self._skills = skills
        self._requests_dir = skill_requests  # skills/_incoming: requests for Hermes (M8)
        self._policy = policy
        self._clock = clock
        self._tz = ZoneInfo(policy.user.timezone)
        self._approvals = approvals
        self._intents = intents
        self._conn = conn
        self._pending: dict[str, _Confirmation] = {}
        self._follow: dict[str, _FollowUp] = {}

    def awaiting(self, source: str) -> bool:
        """True while FLINS is waiting for your yes/no or an answer to its question."""
        now = self._clock.now()
        p, f = self._pending.get(source), self._follow.get(source)
        return bool((p and p.expires > now) or (f and f.expires > now))

    def awaiting_answer(self, source: str) -> bool:
        """True while FLINS asked an open question ("What should I tell Rahul?"): your next
        words are the answer, whatever they say."""
        f = self._follow.get(source)
        return bool(f and f.expires > self._clock.now())

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

        follow = self._follow.pop(source, None)
        if follow is not None and follow.expires > now:
            if _plain(text) in NO:
                return "Okay, never mind."
            command = parse(text, now=now, tz=self._tz)
            if command is None or isinstance(command, ParseError):
                return await self._answer(follow, text, source)
            # A clear command ("stop", "status", ...) wins over the open question.

        wish = SKILL_REQUEST.match(text.strip())
        if wish is not None and self._requests_dir is not None:
            body = wish.group(1).strip().rstrip(".")
            write_request(self._requests_dir, f"Make a skill that {body}", source, now)
            return (
                f'Saved a skill request for Hermes: "{body}". When it is built, it will wait '
                "for your review on the dashboard's Skills page."
            )

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

    async def summary(self) -> str:
        """'What did I miss?': the whats_missed skill if you enabled it, else the debrief."""
        if self._skills is not None:
            skill = await self._skills.for_command("summary")
            if skill is not None:
                now = self._clock.now()
                since = await self._modes.session_started_at()
                fmt = "%Y-%m-%d %H:%M"
                inp = SkillInput(
                    trigger="command:summary",
                    mode=self._modes.state.mode.value,
                    now_local=now.astimezone(self._tz).strftime(fmt),
                    since_local=since.astimezone(self._tz).strftime(fmt),
                    timezone=self._policy.user.timezone,
                    user_name=self._policy.user.name,
                )
                result, _ = await self._skills.run(
                    skill, inp, self._skills.db_provider(None), now=now
                )
                note = result.output.first("note") if result.output else None
                if note is not None:
                    return note.text
        return await self._modes.summary_text()

    # ------------------------------------------------------------------ Tier B

    def confirm(self, source: str, question: str, run: Callable[[], Awaitable[str]]) -> str:
        """Ask a yes/no question; `run` happens only if the next answer is yes (60 s)."""
        return self._confirm(source, question, run)

    def _confirm(self, source: str, question: str, run: Callable[[], Awaitable[str]]) -> str:
        self._pending[source] = _Confirmation(question, run, self._clock.now() + CONFIRM_FOR)
        return f"{question} Say yes or no."

    async def _tier_b(self, text: str, source: str) -> str | None:
        owner = ("voice", "self_chat", "cli", "dashboard") if self._conversational else OWNER_B
        if self._intents is None or source not in owner:
            return None
        quiet = self._conversational or source == "self_chat"
        names = await contact_names(self._conn) if self._conn is not None else []
        try:
            intent = await self._intents.parse(text, names)
        except LLMError:
            return None if quiet else "Sorry, I can't understand that right now."
        a = intent.action
        if a == "unknown":
            return None if quiet else HELP
        if a == "stop":  # stopping is always safe: no confirmation
            return await self._mode(ModeEvent.KILL, source)
        if a == "status":
            await self._modes.refresh()
            return await self._modes.status_text()
        if a == "summary":
            return await self.summary()
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
        return None if quiet else HELP

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
                self._ask(source, _FollowUp("time", self._expiry()))
                return "Until when? For example 'till 7pm' or 'one more hour'."
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
        if self._conn is None or not self._can_send:
            return "Sending messages only works while `flins run` is running."
        return await self._send_flow(intent.contact, intent.text, source)

    async def _send_flow(self, spoken: str | None, text: str | None, source: str) -> str:
        """Find the contact and the message, asking for whichever is missing."""
        assert self._conn is not None
        if not spoken:
            self._ask(source, _FollowUp("contact", self._expiry(), text=text))
            return "Who should I send it to?"
        matches = await find_contacts(self._conn, spoken)
        if not matches:
            self._ask(source, _FollowUp("contact", self._expiry(), text=text))
            return f"I couldn't find a contact called {spoken}. Who should I send it to?"
        if len(matches) > 1:
            self._ask(source, _FollowUp("contact", self._expiry(), text=text))
            names = ", ".join(m.name for m in matches[:4])
            return f"Which one: {names}? Say the full name."
        who = matches[0]
        if not text:
            self._ask(source, _FollowUp("message", self._expiry(), contact=who))
            return f"What should I tell {who.name}?"
        return self._confirm_send_to(who, text, source)

    def _confirm_send_to(self, who: ContactMatch, text: str, source: str) -> str:
        user = self._policy.user.name
        signature = self._policy.user.signature
        # FLINS speaks *for* you, never *as* you.
        body = f"{user} asked me to tell you: {text.strip()}"
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

    # ------------------------------------------------------------------ follow-ups

    def _expiry(self) -> datetime:
        return self._clock.now() + CONFIRM_FOR

    def _ask(self, source: str, follow: _FollowUp) -> None:
        self._follow[source] = follow

    async def _answer(self, follow: _FollowUp, text: str, source: str) -> str:
        """Your words answer FLINS's last question."""
        if follow.kind == "message":
            assert follow.contact is not None
            message = await self._addressed_to(follow.contact.name, text)
            return self._confirm_send_to(follow.contact, message, source)
        if follow.kind == "contact":
            if self._conn is None:
                return "Sending messages only works while `flins run` is running."
            spoken = text.strip().strip(".!?")
            if not await find_contacts(self._conn, spoken) and self._intents is not None:
                # "to Radika HD please" -> let the model pick the name out.
                try:
                    names = await contact_names(self._conn)
                    guess = await self._intents.parse(f"send a message to {spoken}", names)
                    spoken = guess.contact or spoken
                except LLMError:
                    pass
            return await self._send_flow(spoken, follow.text, source)
        # "time": answer to "Until when?"
        until = parse_time(text.strip().strip(".!?").lower(), self._clock.now(), self._tz)
        if until is None and self._intents is not None:
            try:
                guess = await self._intents.parse(f"extend {text}")
                if guess.duration_or_time:
                    until = parse_time(guess.duration_or_time, self._clock.now(), self._tz)
            except LLMError:
                pass
        if until is None:
            self._ask(source, _FollowUp("time", self._expiry()))
            return "I didn't get the time. Try 'till 7pm' or 'for one more hour'."
        target = until

        async def run() -> str:
            return await self._mode(ModeEvent.EXTEND, source, until=target)

        return self._confirm(source, f"Keep the mode on until {self._hm(target)}?", run)

    async def _addressed_to(self, name: str, said: str) -> str:
        """'I'll call him back at 8' -> 'I'll call you back at 8' (written to the recipient)."""
        said = said.strip()
        if self._intents is None:
            return said
        try:
            guess = await self._intents.parse(f"tell {name} {said}", [name])
        except LLMError:
            return said
        return guess.text.strip() if guess.action == "send" and guess.text else said

    def _hm(self, dt: datetime) -> str:
        return dt.astimezone(self._tz).strftime("%H:%M")


class TextHandler(Protocol):
    """Anything that answers your text: the CommandRouter, or the assistant core around it."""

    async def handle_text(self, text: str, *, source: str) -> str | None: ...


class SelfChatCommands:
    """Listens for messages the user writes in their own 'Message yourself' chat."""

    def __init__(self, conn: aiosqlite.Connection, bus: EventBus, router: TextHandler) -> None:
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
