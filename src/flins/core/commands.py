"""Turns Tier-A commands (CLI, WhatsApp self-chat, later voice) into actions and reply text."""

from __future__ import annotations

from zoneinfo import ZoneInfo

import aiosqlite

from flins.core.audit import AuditLog
from flins.core.clock import Clock
from flins.core.events import Event, EventBus
from flins.core.grammar import ApprovalCommand, ModeCommand, ParseError, StatusCommand, parse
from flins.core.modes import ModeManager, TransitionError
from flins.core.policy_schema import Policy

APPROVALS_LATER = "Approvals arrive in M4. Nothing is waiting, because nothing is drafted yet."


class CommandRouter:
    def __init__(self, modes: ModeManager, policy: Policy, clock: Clock) -> None:
        self._modes = modes
        self._clock = clock
        self._tz = ZoneInfo(policy.user.timezone)

    async def handle_text(self, text: str, *, source: str) -> str | None:
        """Run a command. Returns reply text, or None if `text` isn't a command."""
        cmd = parse(text, now=self._clock.now(), tz=self._tz)
        if cmd is None:
            return None
        if isinstance(cmd, ParseError):
            return cmd.message
        if isinstance(cmd, StatusCommand):
            await self._modes.refresh()
            return await self._modes.status_text()
        if isinstance(cmd, ApprovalCommand):
            return APPROVALS_LATER
        assert isinstance(cmd, ModeCommand)
        try:
            result = await self._modes.apply(
                cmd.event, source=source, until=cmd.until, profile=cmd.profile
            )
        except TransitionError as exc:
            return str(exc)
        return result.readback


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
        reply = await self._router.handle_text(row[0], source="self_chat")
        if reply is None:
            return
        await self._audit.record(
            "commands", "command.self_chat", {"message_id": d["message_id"], "reply": reply}
        )
        # M4 sends this back into the self-chat. Until then it's shown where FLINS runs.
        await self._bus.publish("command.reply", {"source": "self_chat", "text": reply})
