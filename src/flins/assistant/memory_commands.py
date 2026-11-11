"""Memory commands: fixed phrases, handled without any AI model.

    remember that <fact>          save it (secrets are refused)
    remember this / that          save what you just said, after you confirm
    forget that                   drop the memory just saved (or the one just used: asks first)
    forget (everything) about X   asks first, showing what would go
    forget everything             asks first
    forget this conversation      asks first
    what do you remember [about X]
    why do you know that          the memories behind the last answer, with their sources
    memory on / memory off

Every answer here is built from stored rows. Nothing is worded by a model, so a source or a
date can't be made up.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Protocol

from flins.assistant.conversation import ConversationStore
from flins.assistant.memory import MemoryRecord, SqlMemory, tokens
from flins.assistant.models import MemoryItem
from flins.core.audit import AuditLog
from flins.core.grammar import strip_address
from flins.settings import AssistantConfig

I = re.IGNORECASE  # noqa: E741
_SWITCH = re.compile(
    r"^(?:(?:turn |switch )?memory (?P<a>on|off)|(?P<b>stop|start) remembering(?: things)?"
    r"|(?P<c>disable|enable) memory)$",
    I,
)
_WHY = re.compile(
    r"^(?:(?:why|how) do you know (?:that|this)|how did you know(?: that| this)?"
    r"|where did you (?:get|learn|hear) that(?: from)?|why do you remember that)$",
    I,
)
_RECALL = re.compile(
    r"^(?:what (?:do|did) you remember(?: about (?P<x>.+))?|what do you know about me"
    r"|what have you remembered|(?:show|list)(?: me)? (?:my |your |the )?memor(?:y|ies))$",
    I,
)
_FORGET_CHAT = re.compile(
    r"^(?:forget (?:this|our|the) (?:conversation|chat)"
    r"|(?:clear|delete|wipe) (?:this |our |the )?(?:conversation|chat)(?: history)?)$",
    I,
)
_FORGET_ALL = re.compile(
    r"^(?:forget everything(?: you (?:know|remember))?"
    r"|(?:clear|wipe|delete|erase) (?:all )?(?:of )?(?:your |my |the )?memor(?:y|ies))$",
    I,
)
_FORGET_LAST = re.compile(
    r"^(?:forget (?:that|this|it)|(?:delete|remove) that(?: memory)?|don'?t remember that)$", I
)
_FORGET_ABOUT = re.compile(
    r"^forget (?:(?:everything|anything|all|what you know|what i said) )?"
    r"(?:about |that )?(?P<x>.+)$",
    I,
)
_REMEMBER_BARE = re.compile(r"^(?:remember|save|note) (?:this|that|it)$", I)
_REMINDER = re.compile(r"^(?:remember|don'?t forget) to\b", I)
_REMEMBER = re.compile(
    r"^(?:remember(?: that)?|note that|keep in mind(?: that)?|don'?t forget that)[:,]?\s+"
    r"(?P<x>.{3,})$",
    I | re.DOTALL,
)

RECENT = timedelta(minutes=15)  # "forget that" right after saving needs no confirmation
MEMORY_OFF = "Memory is off, so I'm not saving anything. Say 'memory on' to turn it back on."
REMINDER = (
    "That sounds like a reminder, and I can't set reminders yet. "
    "I can remember facts: say 'remember that …'."
)


class Confirmer(Protocol):
    """The command router's yes/no machinery."""

    def awaiting(self, source: str) -> bool: ...
    def confirm(self, source: str, question: str, run: Callable[[], Awaitable[str]]) -> str: ...


def _quote(text: str, n: int = 120) -> str:
    t = " ".join(text.split())
    return f'"{t if len(t) <= n else t[: n - 1] + "…"}"'


class MemoryCommands:
    def __init__(
        self,
        memory: SqlMemory,
        store: ConversationStore,
        confirmer: Confirmer,
        cfg: AssistantConfig,
        audit: AuditLog,
    ) -> None:
        self._memory = memory
        self._store = store
        self._confirmer = confirmer
        self._cfg = cfg
        self._audit = audit

    def _line(self, r: MemoryRecord) -> str:
        tag = " [sensitive]" if r.privacy == "sensitive" else ""
        return f"{r.content} ({r.where(self._memory.tz)}){tag}"

    async def handle(
        self, text: str, source: str, now: datetime, last_used: list[MemoryItem]
    ) -> str | None:
        """Reply text if `text` is a memory command, else None."""
        if not self._cfg.memory.enabled:
            return None
        t = strip_address(text)
        m = _SWITCH.match(t)
        if m:
            word = (m.group("a") or m.group("b") or m.group("c") or "").lower()
            return await self._switch(word in ("on", "start", "enable"))
        if _WHY.match(t):
            return self._why(last_used)
        m = _RECALL.match(t)
        if m:
            return await self._recall(m.group("x"), now)
        if _FORGET_CHAT.match(t):
            return self._forget_chat(source)
        if _FORGET_ALL.match(t):
            return await self._forget_all(source)
        if _FORGET_LAST.match(t):
            return await self._forget_last(source, now, last_used)
        m = _FORGET_ABOUT.match(t)
        if m:
            return await self._forget_about(m.group("x").strip(" .?!\"'"), source, now)
        if _REMEMBER_BARE.match(t):
            return await self._remember_previous(source, now)
        if _REMINDER.match(t):  # only reached when task commands aren't available
            return REMINDER
        m = _REMEMBER.match(t)
        if m:
            return await self._remember(m.group("x"), source, now, "told")
        return None

    # ------------------------------------------------------------------ switch / why / recall

    async def _switch(self, on: bool) -> str:
        await self._memory.set_enabled(on)
        if on:
            return "Memory is on. I only save what you ask me to, or agree to."
        n = await self._memory.count()
        kept = f" The {n} I already have are kept, but I won't use them." if n else ""
        return f"Memory is off. I won't save or use memories until you say 'memory on'.{kept}"

    def _why(self, last_used: list[MemoryItem]) -> str:
        used = [m for m in last_used if m.matched]
        if not used:
            return (
                "Nothing from my memory went into that answer. It came from general knowledge "
                "or from what we said in this conversation."
            )
        lines = [f"• {m.content} ({m.source})" for m in used[:5]]
        return "For that answer I had these in front of me:\n" + "\n".join(lines)

    async def _recall(self, about: str | None, now: datetime) -> str:
        if not await self._memory.enabled():
            return MEMORY_OFF
        if about:
            topic = about.strip(" .?!\"'")
            if topic.lower() in ("me", "myself"):
                about = None
            else:
                found = await self._memory.search(topic, now)
                if not found:
                    return f"I don't remember anything about {topic}."
                lines = [f"• {self._line(r)}" for r in found[:8]]
                more = f"\n…and {len(found) - 8} more." if len(found) > 8 else ""
                return f"About {topic}:\n" + "\n".join(lines) + more
        records = await self._memory.all(now)
        if not records:
            return "I don't remember anything yet. Tell me things with 'remember that …'."
        top = sorted(records, key=lambda r: (-r.importance, -r.id))[:8]
        lines = [f"• {self._line(r)}" for r in top]
        more = (
            f"\n…and {len(records) - 8} more (see the dashboard's Memory page)."
            if len(records) > 8
            else ""
        )
        n = len(records)
        return f"I remember {n} thing{'s' if n != 1 else ''}:\n" + "\n".join(lines) + more

    # ------------------------------------------------------------------ remember

    async def _remember(self, content: str, source: str, now: datetime, how: str) -> str:
        if not await self._memory.enabled():
            return MEMORY_OFF
        result = await self._memory.save(content.strip(), source=how, channel=source, now=now)
        if result.status == "refused":
            return f"I won't save that: {result.reason}."
        assert result.record is not None
        if result.status == "duplicate":
            return f"I already had that: {_quote(result.record.content)}."
        return f"I'll remember that: {_quote(result.record.content)}."

    async def _remember_previous(self, source: str, now: datetime) -> str:
        """'Remember this': the last thing you said that wasn't itself a command."""
        if not await self._memory.enabled():
            return MEMORY_OFF
        turns = await self._store.history(12)
        for i in range(len(turns) - 1, 0, -1):
            said, answer = turns[i - 1], turns[i]
            if (
                said.role == "user"
                and answer.role == "assistant"
                and answer.route == "chat"
                and now - said.ts <= RECENT
            ):
                text = said.text

                async def run(text: str = text) -> str:
                    return await self._remember(text, source, now, "told")

                return self._confirmer.confirm(source, f"Remember {_quote(text)}?", run)
        return "Remember what? Say 'remember that …' with the thing to remember."

    # ------------------------------------------------------------------ forget

    async def _chat_note(self, contents: list[str]) -> str:
        """Forgetting a memory doesn't erase what was said in the recent conversation, which
        FLINS can still see. Say so, instead of pretending it's gone everywhere."""
        recent = " ".join(t.text for t in await self._store.history(40)).lower()
        for content in contents:
            words = tokens(content)
            if words and sum(w in recent for w in words) >= max(1, round(0.6 * len(words))):
                return (
                    " It's still in our recent conversation, though: say 'forget this "
                    "conversation' to clear that too."
                )
        return ""

    async def _forget_last(self, source: str, now: datetime, last_used: list[MemoryItem]) -> str:
        records = await self._memory.all(now)
        if records and now - records[0].created_at <= RECENT:
            latest = records[0]
            await self._memory.forget([latest.id], via=source)
            note = await self._chat_note([latest.content])
            return f"Forgotten: {_quote(latest.content)}.{note}"
        used = [m for m in last_used if m.matched and m.id is not None]
        if len(used) == 1 and used[0].id is not None:
            target = used[0]
            ids = [used[0].id]

            async def run() -> str:
                await self._memory.forget(ids, via=source)
                note = await self._chat_note([target.content])
                return f"Forgotten: {_quote(target.content)}.{note}"

            return self._confirmer.confirm(source, f"Forget {_quote(target.content)}?", run)
        return "Which one? Say 'forget about <topic>', or 'what do you remember' to see them all."

    async def _forget_about(self, topic: str, source: str, now: datetime) -> str:
        found = await self._memory.search(topic, now)
        if not found:
            return f"I don't have anything about {topic}."
        ids = [r.id for r in found]
        n = len(found)

        async def run() -> str:
            deleted = await self._memory.forget(ids, via=source)
            note = await self._chat_note([r.content for r in found])
            return f"Forgotten {deleted} memor{'y' if deleted == 1 else 'ies'} about {topic}.{note}"

        if n == 1:
            return self._confirmer.confirm(source, f"Forget {_quote(found[0].content)}?", run)
        shown = "; ".join(_quote(r.content, 60) for r in found[:4])
        more = f" and {n - 4} more" if n > 4 else ""
        return self._confirmer.confirm(
            source, f"Forget these {n} memories about {topic}: {shown}{more}?", run
        )

    async def _forget_all(self, source: str) -> str:
        n = await self._memory.count()
        if not n:
            return "I don't have any memories to forget."

        async def run() -> str:
            deleted = await self._memory.forget_all(via=source)
            return f"Done. All {deleted} memories are deleted."

        return self._confirmer.confirm(
            source, f"Delete all {n} memories? This can't be undone.", run
        )

    def _forget_chat(self, source: str) -> str:
        async def run() -> str:
            n = await self._store.clear()
            await self._audit.record("user", "assistant.conversation_cleared", {"turns": n})
            return "Done. Our conversation history is deleted. Your memories are untouched."

        return self._confirmer.confirm(
            source, "Delete our whole conversation history? Memories stay.", run
        )
