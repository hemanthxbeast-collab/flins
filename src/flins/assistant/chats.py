"""Your chats, read-only (JARVIS phase 6a, docs/milestones/J6.md).

"what did Rahul say?" reads the last messages out word for word (no model). "summarise
Rahul's chat" asks the summary model, with the messages as untrusted data and no tools.
"reply to him ..." hands *your* words to the normal send flow: read back, yes / no, Outbox.

This module only reads. It never drafts a reply from what a contact wrote, and what a contact
wrote never enters the conversation history (the core stores a placeholder instead).
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

import aiosqlite
from pydantic import BaseModel, ConfigDict

from flins.core.audit import AuditLog
from flins.core.intents import ContactMatch, find_contacts
from flins.llm.context import Line, neutralise
from flins.llm.gateway import LLMError, LLMGateway
from flins.llm.prompts import Prompt

REF_TTL = timedelta(minutes=10)  # how long "him / her / them" keeps meaning the last person
CLIP = 280
VOICE_MAX = 4

_CHAT = r"(?:chat|chart|messages?|texts?|conversation)"  # speech-to-text hears "chart"
_LEAD = re.compile(
    r"^\W*(?:(?:hey|hi|ok|okay)[\s,]+)?(?:flins[\s,]+)?"
    r"(?:(?:can|could|will|would) you\s+)?(?:please\s+)?",
    re.IGNORECASE,
)
_TAIL = re.compile(r"(?:[\s,]+(?:for me|please|now))*[\s.?!]*$", re.IGNORECASE)
_READ = [
    re.compile(p, re.IGNORECASE)
    for p in (
        rf"(?:show|read|open|check)(?: me)?(?: out)? (?P<name>.+?)['’]s "
        rf"(?:last |latest |recent |new )?{_CHAT}",
        rf"(?:show|read|open|check)(?: me)?(?: out)? (?:the |my )?(?:last |latest |recent |new )?"
        rf"{_CHAT} (?:with|from|of|by) (?P<name>.+)",
        r"(?:any|are there any|do i have(?: any)?|is there any)(?: new| recent)? "
        r"(?:messages?|texts?) from (?P<name>.+)",
    )
]
# "what did X say" could also be a general question ("what did Einstein say about time"):
# these only count when X is one of your contacts.
_READ_LOOSE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"what did (?P<name>.+?) (?:say|send|text|write|message|want|tell me|reply)"
        r"(?: (?:to me|today|now|lately|recently|just now|about .+))?",
        r"what(?:['’]s| is| has| was) (?P<name>.+?) (?:saying|said|sent|texting|sending)"
        r"(?: (?:to me|today|now|lately|recently|about .+))?",
    )
]
_SUM = [
    re.compile(p, re.IGNORECASE)
    for p in (
        rf"summari[sz]e (?P<name>.+?)['’]s {_CHAT}",
        rf"summari[sz]e (?:the |my )?{_CHAT} (?:with|from|of|by) (?P<name>.+)",
        rf"(?:give me |i want |get me )?(?:a |the )?summary of (?P<name>.+?)['’]s {_CHAT}",
        rf"(?:give me |i want |get me )?(?:a |the )?summary of (?:the |my )?{_CHAT} "
        rf"(?:with|from|of|by) (?P<name>.+)",
        r"summari[sz]e what (?P<name>.+?) (?:said|sent|wrote|has said)",
    )
]
# "what does she want?" right after a read-out: answer from the chat, don't let the
# conversation model guess (it never sees the messages). Only for your contacts.
_SUM_LOOSE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"what (?:does|did|is|was) (?P<name>.+?) (?:want|need|mean|asking(?: for)?|ask(?: for)?|"
        r"talking about|on about)(?: (?:exactly|from me|by that|now|again))*",
        r"what (?:is|was) (?P<name>.+?)['’]s (?:message|chat|text) about",
    )
]
_REPLY = [
    re.compile(p, re.IGNORECASE | re.DOTALL)
    for p in (
        r"(?:reply|respond|answer|write back|text back)(?: to)? (?P<who>him|her|them)\b"
        r"(?:[\s,:]+(?:that|saying|with))?[\s,:]*(?P<text>.*)",
        r"(?:tell|text|message) (?P<who>him|her|them)\b(?:[\s,:]+that)?[\s,:]+(?P<text>.+)",
        r"(?:reply|respond|write back) to (?P<name>.+?)[\s,:]+(?:that|saying|with)[\s,:]+"
        r"(?P<text>.+)",
        r"(?:reply|respond|write back) to (?P<name>[^,:]+)",
    )
]
_PRONOUN = {"him", "her", "them", "he", "she", "they"}
_NOT_A_PERSON = {
    "you", "i", "we", "it", "that", "this", "flins", "everyone", "everybody", "anyone",
    "someone", "people", "the model", "the assistant", "me", "us",
}  # fmt: skip
_CODE_LINE = re.compile(r"\b(?:otp|pin|password|passcode|cvv|code)\b", re.IGNORECASE)
_DIGITS = re.compile(r"\d{3,}")
_CAREFUL = re.compile(
    r"\b(?:otp|pin|password|passcode|cvv|verification code|bank|transfer|upi|gpay|phonepe|"
    r"paytm|account number|ifsc|rupees|rs\.?|loan)\b|₹",
    re.IGNORECASE,
)
CAREFUL = "Careful: this chat mentions money or codes. Read the messages yourself before acting."
# In the conversation history, right where the model reads it (small models skip prompt rules).
UNSEEN = "The assistant was not shown these messages and cannot answer questions about them."
WHO = "Who do you mean? Say the name, for example 'what did Rahul say'."

Kind = Literal["read", "summary", "reply"]


class Sender(Protocol):
    async def send(self, spoken: str | None, text: str | None, source: str) -> str: ...


@dataclass(frozen=True)
class ChatAnswer:
    text: str  # what you see or hear
    stored: str | None = None  # what goes in the conversation history instead (rule 4)


class ThreadOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str
    wants: str | None = None
    suspicious: bool = False


THREAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "wants": {"type": ["string", "null"]},
        "suspicious": {"type": "boolean"},
    },
    "required": ["summary", "wants", "suspicious"],
    "additionalProperties": False,
}


def clean(text: str) -> str:
    """Drop "Hey FLINS, can you ..." and "... for me?" so the phrase itself is left."""
    return _TAIL.sub("", _LEAD.sub("", text.strip(), count=1), count=1).strip()


def _when(ts: datetime, now: datetime, tz: ZoneInfo) -> str:
    local = ts.astimezone(tz)
    days = (now.astimezone(tz).date() - local.date()).days
    if days == 0:
        return f"{local:%H:%M}"
    if days == 1:
        return f"yesterday {local:%H:%M}"
    return f"{local:%a %d %b %H:%M}"


def _count(lines: list[Line]) -> str:
    return "1 recent message" if len(lines) == 1 else f"{len(lines)} recent messages"


def _body(line: Line) -> str:
    text = " ".join((line.text or "").split())
    if line.msg_type != "text":
        text = f"[{line.msg_type}] {text}".strip()
    return text if len(text) <= CLIP else text[: CLIP - 1].rstrip() + "…"


class ChatReader:
    """The last messages of one chat. SELECT only."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def contact_id(self, jid: str) -> int | None:
        async with self._conn.execute("SELECT id FROM contacts WHERE wa_jid = ?", (jid,)) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else None

    async def recent(self, jid: str, now: datetime, *, limit: int, days: int) -> list[Line]:
        async with self._conn.execute("SELECT wa_lid FROM contacts WHERE wa_jid = ?", (jid,)) as c:
            row = await c.fetchone()
        ids = [jid] + ([str(row[0])] if row and row[0] else [])
        marks = ",".join("?" for _ in ids)
        async with self._conn.execute(
            f"SELECT direction, type, text, ts FROM messages WHERE chat_jid IN ({marks}) "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (*ids, limit),
        ) as cur:
            rows = await cur.fetchall()
        since = now - timedelta(days=days)
        lines = []
        for direction, kind, text, ts in reversed(list(rows)):
            try:
                when = datetime.fromisoformat(str(ts))
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=now.tzinfo)
            if when >= since and kind != "reaction":
                lines.append(Line(when, direction == "OUT", text, str(kind)))
        return lines


def render_thread(lines: list[Line], name: str, now: datetime, tz: ZoneInfo) -> str:
    """The chat for the summary model: untrusted, neutralised, codes removed."""
    out = []
    for line in lines:
        body = neutralise(_body(line))
        if _CODE_LINE.search(body):
            body = _DIGITS.sub("[number]", body)
        who = "Me" if line.from_me else neutralise(name) or "Contact"
        out.append(f"[{_when(line.when, now, tz)}] {who}: {body}")
    return (
        f"<contact>{neutralise(name) or 'Contact'}</contact>\n"
        "<untrusted_thread>\n" + "\n".join(out) + "\n</untrusted_thread>"
    )


class ThreadSummariser:
    def __init__(self, gateway: LLMGateway, prompt: Prompt) -> None:
        self._gw = gateway
        self._prompt = prompt

    async def ready(self) -> bool:
        return await self._gw.budget_ok()

    async def summarise(
        self, lines: list[Line], name: str, now: datetime, tz: ZoneInfo
    ) -> ThreadOut:
        return await self._gw.json(
            prompt=self._prompt,
            user=render_thread(lines, name, now, tz),
            schema=THREAD_SCHEMA,
            out=ThreadOut,
            role="summary",
        )


class ChatCommands:
    def __init__(
        self,
        conn: aiosqlite.Connection,
        tz: ZoneInfo,
        audit: AuditLog,
        *,
        sender: Sender | None = None,
        summariser: ThreadSummariser | None = None,
        max_messages: int = 12,
        days: int = 7,
    ) -> None:
        self._conn = conn
        self._reader = ChatReader(conn)
        self._tz = tz
        self._audit = audit
        self._sender = sender
        self._summariser = summariser
        self._max = max_messages
        self._days = days
        self._last: dict[str, tuple[ContactMatch, datetime]] = {}  # per channel

    # ------------------------------------------------------------------ entry

    async def handle(
        self,
        text: str,
        source: str,
        now: datetime,
        *,
        addressed: bool,
        wait: Callable[[], Awaitable[None]] | None = None,
    ) -> ChatAnswer | None:
        if not addressed:
            return None
        said = clean(text)
        for pattern in _REPLY:
            m = pattern.fullmatch(said)
            if m is not None:
                groups = m.groupdict()
                who = groups.get("who") or groups.get("name") or ""
                return await self._reply(who, (groups.get("text") or "").strip(), source, now)
        for kind, patterns, loose in (
            ("summary", _SUM, False),
            ("summary", _SUM_LOOSE, True),
            ("read", _READ, False),
            ("read", _READ_LOOSE, True),
        ):
            for pattern in patterns:
                m = pattern.fullmatch(said)
                if m is None:
                    continue
                who = await self._resolve(m.group("name"), source, now, loose=loose)
                if who is None:
                    return None  # not about one of your chats: the conversation answers
                if isinstance(who, str):
                    return ChatAnswer(who)
                self._last[source] = (who, now)
                if kind == "summary":
                    return await self._summary(who, source, now, wait)
                return await self._read(who, source, now)
        return None

    # ------------------------------------------------------------------ who

    def _remembered(self, source: str, now: datetime) -> ContactMatch | None:
        last = self._last.get(source)
        return last[0] if last is not None and now - last[1] <= REF_TTL else None

    async def _resolve(
        self, spoken: str, source: str, now: datetime, *, loose: bool
    ) -> ContactMatch | str | None:
        """A contact, a message for you (not found / which one), or None (not a person)."""
        name = " ".join(spoken.strip().strip(".,?!'\"").split())
        low = name.lower()
        if low.startswith("my "):
            name, low = name[3:], low[3:]
        if low in _PRONOUN:
            return self._remembered(source, now) or WHO
        if not name or low in _NOT_A_PERSON or len(name) > 40:
            return None
        matches = await find_contacts(self._conn, name)
        if not matches:
            return None if loose else f"I couldn't find a contact called {name}."
        if len(matches) > 1:
            names = ", ".join(m.name for m in matches[:4])
            return f"Which one: {names}? Say it again with the full name."
        return matches[0]

    # ------------------------------------------------------------------ actions

    async def _note(self, kind: Kind, who: ContactMatch, source: str, count: int) -> None:
        await self._audit.record(
            "assistant",
            "assistant.chat_read",
            {
                "kind": kind,
                "contact_id": await self._reader.contact_id(who.jid),
                "messages": count,
                "source": source,
            },
        )

    async def _lines(self, who: ContactMatch, source: str, now: datetime) -> list[Line]:
        limit = min(self._max, VOICE_MAX) if source == "voice" else self._max
        return await self._reader.recent(who.jid, now, limit=limit, days=self._days)

    def _readout(self, who: ContactMatch, lines: list[Line], now: datetime) -> str:
        count = _count(lines).replace("recent ", "")
        rows = [
            f"[{_when(x.when, now, self._tz)}] {'You' if x.from_me else who.name}: {_body(x)}"
            for x in lines
        ]
        return "\n".join([f"Chat with {who.name}, last {count}:", *rows])

    async def _read(self, who: ContactMatch, source: str, now: datetime) -> ChatAnswer:
        lines = await self._lines(who, source, now)
        await self._note("read", who, source, len(lines))
        if not lines:
            return ChatAnswer(f"No messages with {who.name} in the last {self._days} days.")
        return ChatAnswer(
            self._readout(who, lines, now),
            f"[Read out {_count(lines)} from the chat with {who.name}. {UNSEEN}]",
        )

    async def _summary(
        self,
        who: ContactMatch,
        source: str,
        now: datetime,
        wait: Callable[[], Awaitable[None]] | None,
    ) -> ChatAnswer:
        lines = await self._reader.recent(who.jid, now, limit=self._max, days=self._days)
        if not lines:
            await self._note("summary", who, source, 0)
            return ChatAnswer(f"No messages with {who.name} in the last {self._days} days.")
        stored = f"[Summarised {_count(lines)} from the chat with {who.name}. {UNSEEN}]"
        shown = lines[-VOICE_MAX:] if source == "voice" else lines
        if self._summariser is None or not await self._summariser.ready():
            await self._note("read", who, source, len(shown))
            note = "I can't summarise right now, so here are the messages."
            return ChatAnswer(f"{note}\n{self._readout(who, shown, now)}", stored)
        if wait is not None:
            with contextlib.suppress(Exception):  # a courtesy only
                await wait()
        try:
            out = await self._summariser.summarise(lines, who.name, now, self._tz)
        except LLMError:
            await self._note("read", who, source, len(shown))
            note = "I couldn't get a summary, so here are the messages."
            return ChatAnswer(f"{note}\n{self._readout(who, shown, now)}", stored)
        await self._note("summary", who, source, len(lines))
        summary = " ".join(out.summary.split())[:700] or "(the model returned nothing)"
        parts = [f"Chat with {who.name} ({_count(lines)}): {summary}"]
        wants = " ".join((out.wants or "").split())[:200]
        if wants:
            parts.append(f"Waiting on you: {wants}")
        if out.suspicious or any(_CAREFUL.search(x.text or "") for x in lines if not x.from_me):
            parts.append(CAREFUL)
        return ChatAnswer("\n".join(parts), stored)

    async def _reply(self, who: str, text: str, source: str, now: datetime) -> ChatAnswer:
        """Your words to the normal send flow. FLINS never writes the reply itself."""
        if self._sender is None:
            return ChatAnswer("Sending messages only works while `flins run` is running.")
        low = who.strip().lower()
        if low in _PRONOUN:
            target = self._remembered(source, now)
            if target is None:
                return ChatAnswer(
                    "Who do you mean? Say the name, for example 'tell Rahul I'll call at 8'."
                )
            name = target.name
        else:
            name = " ".join(who.split())
            if not name or low in _NOT_A_PERSON:
                return ChatAnswer(WHO)
            words = name.split()
            if not text and len(words) > 1:
                # "reply to Rahul I'll call at 8": the name is the longest start of it that
                # is exactly a contact's name (else its first word); the rest is your message.
                split = 0
                for k in (3, 2, 1):
                    start = " ".join(words[:k]).lower()
                    found = await find_contacts(self._conn, start) if k < len(words) else []
                    if any(m.name.lower() == start for m in found):
                        split = k
                        break
                if not split and await find_contacts(self._conn, words[0]):
                    split = 1
                if split:
                    name, text = " ".join(words[:split]), " ".join(words[split:])
        return ChatAnswer(await self._sender.send(name, text or None, source))
