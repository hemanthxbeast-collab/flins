"""Outbox (SPEC §5.3, CLAUDE.md invariant 2): the only code that may send a WhatsApp message.

Three kinds of message:
  approved  a draft you approved (M4)
  auto      an automatic holding reply (M5): one of your templates, AUTO_SAFE only
  self      a note from FLINS to your own self-chat (M5)

`enqueue()` / `enqueue_self()` add rows after their own checks (any process may call them).
`OutboxWorker` runs inside `flins run` and re-checks everything right before each send.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Literal

import aiosqlite

from flins.adapters.base import MessagingAdapter
from flins.core.audit import AuditLog
from flins.core.clock import Clock, from_iso, to_iso
from flins.core.events import Event, EventBus
from flins.core.levels import Mode
from flins.core.policy_schema import TIMED_MODES, Policy

Kind = Literal["approved", "auto", "self"]
CONTACT_WINDOW = timedelta(hours=24)  # only reply in chats where they wrote to us recently
APPROVED_DELAY = timedelta(seconds=3)  # a human already looked at it; send almost at once
SELF_DELAY = timedelta(seconds=1)
MAX_TEXT = 2000
SELF_PREFIX = "🤖 "  # every note to your self-chat starts with this; FLINS ignores such messages


class OutboxError(ValueError):
    """The message can't be queued. The text is safe to show the user."""


async def _one(
    conn: aiosqlite.Connection, sql: str, args: tuple[object, ...]
) -> aiosqlite.Row | None:
    async with conn.execute(sql, args) as cur:
        return await cur.fetchone()


async def contact_wrote_recently(conn: aiosqlite.Connection, chat: str, now: datetime) -> bool:
    row = await _one(
        conn,
        "SELECT 1 FROM messages WHERE chat_jid = ? AND direction = 'IN' AND ts >= ? LIMIT 1",
        (chat, to_iso(now - CONTACT_WINDOW)),
    )
    return row is not None


async def _insert(
    conn: aiosqlite.Connection,
    *,
    kind: Kind,
    chat_jid: str,
    text: str,
    level: int,
    now: datetime,
    delay: timedelta,
    approval_id: int | None = None,
    decision_id: int | None = None,
) -> int:
    cur = await conn.execute(
        "INSERT INTO outbox (client_msg_id, chat_jid, text, level, not_before, status, "
        "created_at, approval_id, decision_id, kind) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
        (
            f"flins-{uuid.uuid4().hex}",
            chat_jid,
            text,
            level,
            to_iso(now + delay),
            to_iso(now),
            approval_id,
            decision_id,
            kind,
        ),
    )
    await conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


async def enqueue(
    conn: aiosqlite.Connection,
    *,
    chat_jid: str,
    text: str,
    level: int,
    now: datetime,
    approval_id: int | None = None,
    decision_id: int | None = None,
    delay: timedelta = APPROVED_DELAY,
    kind: Kind = "approved",
) -> int:
    """Queue a reply to a contact. Raises OutboxError with a user-facing reason if not allowed."""
    if kind == "self":
        raise ValueError("use enqueue_self for self-chat notes")
    text = text.strip()
    if not text:
        raise OutboxError("The message is empty.")
    if len(text) > MAX_TEXT:
        raise OutboxError("The message is too long.")
    if chat_jid.endswith(("@g.us", "@broadcast", "@newsletter")):
        raise OutboxError("FLINS never sends to groups, broadcasts or channels.")
    if not await contact_wrote_recently(conn, chat_jid, now):
        raise OutboxError("FLINS only replies to people who wrote to you in the last 24 hours.")
    if approval_id is not None and await _one(
        conn, "SELECT 1 FROM outbox WHERE approval_id = ?", (approval_id,)
    ):
        raise OutboxError("This draft was already sent or queued.")
    return await _insert(
        conn,
        kind=kind,
        chat_jid=chat_jid,
        text=text,
        level=level,
        now=now,
        delay=delay,
        approval_id=approval_id,
        decision_id=decision_id,
    )


async def enqueue_self(conn: aiosqlite.Connection, *, me: str, text: str, now: datetime) -> int:
    """A note from FLINS to your own self-chat. Always starts with SELF_PREFIX."""
    body = text.strip()
    if not body.startswith(SELF_PREFIX.strip()):
        body = SELF_PREFIX + body
    return await _insert(
        conn, kind="self", chat_jid=me, text=body[:MAX_TEXT], level=0, now=now, delay=SELF_DELAY
    )


async def holding_reply_sent_this_session(
    conn: aiosqlite.Connection, chat: str, since: datetime
) -> bool:
    row = await _one(
        conn,
        "SELECT 1 FROM outbox WHERE chat_jid = ? AND kind = 'auto' "
        "AND status IN ('queued', 'sent', 'dry_run') AND created_at >= ? LIMIT 1",
        (chat, to_iso(since)),
    )
    return row is not None


async def cancel_queued(
    conn: aiosqlite.Connection, reason: str, kinds: tuple[Kind, ...] = ("approved", "auto")
) -> int:
    """Kill switch: cancel queued messages of these kinds. Returns how many were cancelled."""
    marks = ",".join("?" * len(kinds))
    cur = await conn.execute(
        f"UPDATE outbox SET status = 'cancelled', error = ? "
        f"WHERE status = 'queued' AND kind IN ({marks})",
        (reason, *kinds),
    )
    await conn.commit()
    return cur.rowcount


class OutboxWorker:
    def __init__(
        self,
        conn: aiosqlite.Connection,
        policy: Policy,
        adapter: MessagingAdapter,
        clock: Clock,
        bus: EventBus,
    ) -> None:
        self._conn = conn
        self._policy = policy
        self._adapter = adapter
        self._clock = clock
        self._bus = bus
        self._audit = AuditLog(conn)
        bus.subscribe("mode.changed", self._on_mode)

    async def _on_mode(self, event: Event) -> None:
        to = event.data.get("to")
        if to == "PAUSED":
            n = await cancel_queued(self._conn, "FLINS was paused")
        elif to == "NORMAL":
            n = await cancel_queued(self._conn, "the busy mode ended", kinds=("auto",))
        else:
            return
        if n:
            await self._audit.record("outbox", "outbox.cancelled", {"count": n, "mode": to})
            await self._bus.publish("outbox.cancelled", {"count": n, "mode": to})

    async def _mode(self) -> tuple[Mode, datetime | None, datetime | None]:
        # Read the DB, not a cached state: `flins stop` may come from another process.
        row = await _one(
            self._conn, "SELECT mode, until, started_at FROM mode_state WHERE id = 1", ()
        )
        if row is None:
            return Mode.NORMAL, None, None
        until = from_iso(row["until"]) if row["until"] else None
        return Mode(row["mode"]), until, from_iso(row["started_at"])

    async def _count(self, sql: str, args: tuple[object, ...]) -> int:
        row = await _one(self._conn, sql, args)
        return int(row[0]) if row else 0

    async def _sent_since(self, since: datetime, kinds: tuple[str, ...]) -> int:
        marks = ",".join("?" * len(kinds))
        return await self._count(
            f"SELECT COUNT(*) FROM outbox WHERE status = 'sent' AND sent_at >= ? "
            f"AND kind IN ({marks})",
            (to_iso(since), *kinds),
        )

    async def _finish(self, row: aiosqlite.Row, status: str, **fields: object) -> None:
        sets = ", ".join(["status = ?", *[f"{k} = ?" for k in fields]])
        await self._conn.execute(
            f"UPDATE outbox SET {sets} WHERE id = ?", (status, *fields.values(), row["id"])
        )
        await self._conn.commit()
        data = {
            "outbox_id": row["id"],
            "chat_jid": row["chat_jid"],
            "kind": row["kind"],
            "status": status,
            **fields,
        }
        await self._audit.record("outbox", f"outbox.{status}", data)
        await self._bus.publish("outbox.update", data)

    async def _blocked(self, row: aiosqlite.Row, now: datetime) -> tuple[str, str] | None:
        """(status, reason) if this row must not be sent now, else None."""
        kind = row["kind"]
        lim = self._policy.limits
        if self._policy.dry_run:
            return "dry_run", "dry run is on: nothing was sent"
        if kind == "self":
            return None  # a note to yourself: no contact or mode checks, sent even while paused
        mode, until, _ = await self._mode()
        if mode == Mode.PAUSED:
            return "cancelled", "FLINS is paused"
        if not await contact_wrote_recently(self._conn, row["chat_jid"], now):
            return "failed", "contact hasn't written in 24 hours"
        if kind == "auto":
            if mode not in TIMED_MODES or (until is not None and until <= now):
                return "cancelled", "the busy mode ended"
            if self._policy.auto_replies == "off":
                return "cancelled", "automatic replies are off"
            replied = await self._count(
                "SELECT COUNT(*) FROM messages WHERE chat_jid = ? AND direction = 'OUT' "
                "AND ts >= ?",
                (row["chat_jid"], row["created_at"]),
            )
            if replied:
                return "cancelled", "you replied yourself"
            per_contact = await self._count(
                "SELECT COUNT(*) FROM outbox WHERE chat_jid = ? AND kind = 'auto' "
                "AND status = 'sent' AND sent_at >= ?",
                (row["chat_jid"], to_iso(now - timedelta(hours=1))),
            )
            if per_contact >= lim.per_contact_max_auto_per_hour:
                return "failed", "auto-reply limit for this contact reached"
        if await self._sent_since(now - timedelta(hours=1), ("approved", "auto")) >= (
            lim.global_max_auto_per_hour
        ):
            return "failed", "hourly send limit reached"
        if await self._sent_since(now - timedelta(days=1), ("approved", "auto")) >= (
            lim.global_max_auto_per_day
        ):
            return "failed", "daily send limit reached"
        return None

    async def run_due(self) -> int:
        """Send what's due. Call about once a second. Returns how many rows were handled."""
        now = self._clock.now()
        async with self._conn.execute(
            "SELECT * FROM outbox WHERE status = 'queued' AND not_before <= ? ORDER BY id",
            (to_iso(now),),
        ) as cur:
            due = list(await cur.fetchall())
        for row in due:
            blocked = await self._blocked(row, now)
            if blocked is not None:
                status, reason = blocked
                await self._finish(row, status, error=reason)
                continue
            # Mark first, so a crash mid-send can never cause a second attempt.
            await self._conn.execute(
                "UPDATE outbox SET status = 'failed', error = 'sending (no result yet)' "
                "WHERE id = ?",
                (row["id"],),
            )
            await self._conn.commit()
            result = await self._adapter.send_text(
                row["chat_jid"], row["text"], quote=None, client_msg_id=row["client_msg_id"]
            )
            if result.ok:
                await self._finish(
                    row, "sent", wa_msg_id=result.wa_msg_id, sent_at=to_iso(now), error=None
                )
            else:
                await self._finish(row, "failed", error=(result.error or "send failed")[:200])
        return len(due)
