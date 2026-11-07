"""FLINS writes to your own "Message yourself" chat (M5): command answers, approval prompts,
urgent alerts, expiry warnings and the end-of-session summary.

Every note starts with SELF_PREFIX ("🤖 "). SelfChatCommands ignores self-chat messages that start
with it, or that the outbox sent, so FLINS can never react to its own notes.
"""

from __future__ import annotations

from collections.abc import Callable

import aiosqlite

from flins.core.clock import Clock
from flins.core.events import Event, EventBus
from flins.core.outbox import enqueue_self
from flins.core.policy_schema import Policy


def _short(text: str | None, n: int = 90) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


class SelfChatNotifier:
    def __init__(
        self,
        conn: aiosqlite.Connection,
        bus: EventBus,
        policy: Policy,
        clock: Clock,
        get_me: Callable[[], str | None],
    ) -> None:
        self._conn = conn
        self._policy = policy
        self._clock = clock
        self._get_me = get_me
        bus.subscribe("command.reply", self._on_reply)
        bus.subscribe("decision.made", self._on_decision)
        bus.subscribe("mode.expiring", self._on_expiring)

    async def note(self, text: str) -> int | None:
        """Queue a note to your self-chat. Returns the outbox id, or None if notes are off."""
        me = self._get_me()
        if not self._policy.self_chat_notes or not me or not text.strip():
            return None
        return await enqueue_self(self._conn, me=me, text=text, now=self._clock.now())

    async def _on_reply(self, e: Event) -> None:
        if e.data.get("source") == "self_chat":
            await self.note(str(e.data["text"]))

    async def _on_expiring(self, e: Event) -> None:
        await self.note(str(e.data["text"]))

    async def _incoming(self, message_id: int) -> str:
        async with self._conn.execute(
            "SELECT group_concat(COALESCE(x.text, '[' || x.type || ']'), ' / ') FROM messages x "
            "WHERE x.burst_id = (SELECT burst_id FROM messages WHERE id = ?)",
            (message_id,),
        ) as cur:
            row = await cur.fetchone()
        return str(row[0]) if row and row[0] else ""

    async def _on_decision(self, e: Event) -> None:
        d = e.data
        who = d.get("contact_name") or "Someone"
        said = _short(await self._incoming(int(d["message_id"])))
        if d.get("interrupt") or d.get("escalate"):
            extra = " I sent them the urgent acknowledgement." if d.get("auto_queued") else ""
            await self.note(f'⚠ URGENT from {who}: "{said}"{extra}')
        n = d.get("approval")
        if n and d.get("draft"):
            await self.note(
                f'#{n} {who}: "{said}"\n'
                f'Draft: "{_short(d["draft"], 300)}"\n'
                f"Reply y{n} to send, e{n} <your text> to change it, n{n} to drop it."
            )
