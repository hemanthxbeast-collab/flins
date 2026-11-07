"""Decision pipeline (SPEC §6), M2 version: shadow only.

For each stored incoming message: load context, run hard rules + decide(), and record the
decision with its full trace. Nothing is drafted (M3) and nothing is sent (M4).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import aiosqlite

from flins.core.audit import AuditLog
from flins.core.clock import Clock, from_iso, to_iso
from flins.core.events import Event, EventBus
from flins.core.levels import Level
from flins.core.modes import ModeManager, effective_mode
from flins.core.policy import Decision, MessageFacts, RuleContext, decide_message
from flins.core.policy_schema import Policy
from flins.core.triage import TriageResult

PROMPT_VERSION = None  # no LLM prompts in M2
IDENTICAL_WINDOW = timedelta(minutes=10)

# Given the message and contact rows, return a triage result or None. M3 plugs the LLM in here.
TriageProvider = Callable[[aiosqlite.Row, aiosqlite.Row | None], Awaitable[TriageResult | None]]


async def no_triage(message: aiosqlite.Row, contact: aiosqlite.Row | None) -> TriageResult | None:
    return None


class Pipeline:
    def __init__(
        self,
        conn: aiosqlite.Connection,
        bus: EventBus,
        policy: Policy,
        modes: ModeManager,
        clock: Clock,
        triage: TriageProvider = no_triage,
    ) -> None:
        self._conn = conn
        self._bus = bus
        self._audit = AuditLog(conn)
        self._policy = policy
        self._modes = modes
        self._clock = clock
        self._triage = triage
        bus.subscribe("wa.message", self._on_message)

    async def _on_message(self, event: Event) -> None:
        d = event.data
        if d.get("direction") != "IN" or d.get("self_chat"):
            return  # our own messages and self-chat commands never trigger replies
        await self.process(int(d["message_id"]), mentions_me=bool(d.get("mentions_me")))

    async def _one(self, sql: str, args: tuple[object, ...]) -> aiosqlite.Row | None:
        async with self._conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def _count(self, sql: str, args: tuple[object, ...]) -> int:
        row = await self._one(sql, args)
        return int(row[0]) if row else 0

    async def _fast_replies(self, chat: str, window_s: int, limit: int) -> int:
        """How many of our latest auto replies to this chat were answered within `window_s`.

        Until M4 adds sending, the outbox is empty and this is always 0.
        """
        async with self._conn.execute(
            "SELECT not_before FROM outbox WHERE chat_jid = ? AND status = 'sent' "
            "AND level >= ? ORDER BY not_before DESC LIMIT ?",
            (chat, int(Level.AUTO_SAFE), limit),
        ) as cur:
            sent = [from_iso(r[0]) for r in await cur.fetchall()]
        fast = 0
        for t in sent:
            end = t + timedelta(seconds=window_s)
            fast += (
                await self._count(
                    "SELECT COUNT(*) FROM messages WHERE chat_jid = ? AND direction = 'IN' "
                    "AND ts > ? AND ts <= ?",
                    (chat, to_iso(t), to_iso(end)),
                )
                > 0
            )
        return fast

    async def process(self, message_id: int, *, mentions_me: bool = False) -> Decision | None:
        msg = await self._one("SELECT * FROM messages WHERE id = ?", (message_id,))
        if msg is None or msg["direction"] != "IN":
            return None
        now = self._clock.now()
        chat = msg["chat_jid"]
        is_group = chat.endswith("@g.us")
        contact = None
        if not is_group:
            contact = await self._one(
                "SELECT * FROM contacts WHERE wa_jid = ? OR wa_lid = ?", (chat, chat)
            )
        tier = contact["tier"] if contact is not None else "unknown"

        await self._modes.refresh()  # pick up `flins stop` etc. from other processes
        state = self._modes.state
        mode = effective_mode(state, now)
        hour_ago = to_iso(now - timedelta(hours=1))
        loop = self._policy.hard_rules.loop_detection
        ctx = RuleContext(
            mode=mode,
            mode_started_at=state.started_at,
            tier=tier,
            message=MessageFacts(
                text=msg["text"],
                msg_type=msg["type"],
                is_group=is_group,
                chat_jid=chat,
                mentions_me=mentions_me,
                ts=from_iso(msg["ts"]),
            ),
            auto_sent_last_hour=await self._count(
                "SELECT COUNT(*) FROM outbox WHERE chat_jid = ? AND status = 'sent' "
                "AND level >= ? AND not_before >= ?",
                (chat, int(Level.AUTO_SAFE), hour_ago),
            ),
            identical_recent=await self._count(
                "SELECT COUNT(*) FROM messages WHERE chat_jid = ? AND direction = 'IN' "
                "AND text IS ? AND ts >= ?",
                (chat, msg["text"], to_iso(from_iso(msg["ts"]) - IDENTICAL_WINDOW)),
            )
            if msg["text"]
            else 1,
            fast_replies=await self._fast_replies(
                chat, loop.fast_reply_seconds, loop.fast_reply_count
            ),
        )
        triage = await self._triage(msg, contact)
        max_level = contact["max_level"] if contact is not None else None
        allowed = contact["allowed_categories"] if contact is not None else None
        decision = decide_message(
            ctx,
            self._policy,
            profile=state.profile if mode == state.mode else None,
            contact_max_level=Level(max_level) if max_level is not None else None,
            allowed_categories=json.loads(allowed) if allowed else None,
            triage=triage,
        )
        await self._record(msg, contact, mode.value, tier, decision, triage, now)
        return decision

    async def _record(
        self,
        msg: aiosqlite.Row,
        contact: aiosqlite.Row | None,
        mode: str,
        tier: str,
        decision: Decision,
        triage: TriageResult | None,
        now: datetime,
    ) -> None:
        ts = to_iso(now)
        cur = await self._conn.execute(
            "INSERT INTO bursts (chat_jid, first_ts, last_ts, status) "
            "VALUES (?, ?, ?, 'processed')",
            (msg["chat_jid"], msg["ts"], msg["ts"]),
        )
        burst_id = cur.lastrowid
        await self._conn.execute(
            "UPDATE messages SET burst_id = ? WHERE id = ?", (burst_id, msg["id"])
        )
        hard = ",".join(decision.hard_rules) or None
        cur = await self._conn.execute(
            "INSERT INTO decisions (burst_id, message_id, mode, contact_tier, hard_rule, triage, "
            "ceilings, final_level, action, category, flags, escalate, interrupt, prompt_version, "
            "model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                burst_id,
                msg["id"],
                mode,
                tier,
                hard,
                triage.model_dump_json() if triage else None,
                json.dumps(decision.trace()["ceilings"]),
                int(decision.level),
                decision.action,
                decision.category,
                json.dumps(sorted(decision.flags)),
                int(decision.escalate),
                int(decision.interrupt),
                PROMPT_VERSION,
                None,
                ts,
            ),
        )
        decision_id = cur.lastrowid
        await self._conn.commit()
        await self._audit.record(
            "pipeline",
            "decision",
            {
                "decision_id": decision_id,
                "message_id": msg["id"],
                "mode": mode,
                "tier": tier,
                **decision.trace(),
            },
        )
        await self._bus.publish(
            "decision.made",
            {
                "decision_id": decision_id,
                "message_id": msg["id"],
                "chat_jid": msg["chat_jid"],
                "contact_name": (contact["display_name"] or contact["push_name"])
                if contact is not None
                else None,
                **decision.trace(),
            },
        )
