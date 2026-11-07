"""Approvals (SPEC §7.4): you approve, edit or reject a draft; approving queues it in the Outbox.

Each draft gets an approval with a short number (#1, #2, ... restarting daily) so it can be handled
from the dashboard or the WhatsApp self-chat ("y3", "n3", "e3 new text").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import aiosqlite

from flins.core.audit import AuditLog
from flins.core.clock import from_iso, to_iso
from flins.core.outbox import OutboxError, enqueue
from flins.core.policy_schema import Policy


async def create_for_draft(
    conn: aiosqlite.Connection,
    *,
    draft_id: int,
    decision_id: int,
    now: datetime,
    expiry: timedelta,
) -> tuple[int, int]:
    """Create a pending approval. Returns (approval id, short number for today)."""
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with conn.execute(
        "SELECT COALESCE(MAX(short_id), 0) + 1 FROM approvals WHERE created_at >= ?",
        (to_iso(day_start),),
    ) as cur:
        row = await cur.fetchone()
    short_id = int(row[0]) if row else 1
    cur = await conn.execute(
        "INSERT INTO approvals (short_id, draft_id, decision_id, status, expires_at, created_at) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (short_id, draft_id, decision_id, to_iso(now + expiry), to_iso(now)),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid, short_id


@dataclass(frozen=True)
class Pending:
    approval_id: int
    short_id: int
    who: str
    incoming: str
    draft: str
    expires_at: datetime


class ApprovalService:
    def __init__(self, conn: aiosqlite.Connection, policy: Policy) -> None:
        self._conn = conn
        self._policy = policy
        self._audit = AuditLog(conn)

    async def _row(
        self, *, approval_id: int | None, short_id: int | None, now: datetime
    ) -> aiosqlite.Row | None:
        base = (
            "SELECT a.*, d.text AS draft_text, de.message_id, de.final_level, m.chat_jid, "
            "c.disclose_ai FROM approvals a JOIN drafts d ON d.id = a.draft_id "
            "JOIN decisions de ON de.id = a.decision_id JOIN messages m ON m.id = de.message_id "
            "LEFT JOIN contacts c ON c.wa_jid = m.chat_jid OR c.wa_lid = m.chat_jid "
        )
        if approval_id is not None:
            sql, args = base + "WHERE a.id = ?", (approval_id,)
        else:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            sql = base + "WHERE a.short_id = ? AND a.created_at >= ? ORDER BY a.id DESC LIMIT 1"
            args = (short_id, to_iso(day_start))  # type: ignore[assignment]
        async with self._conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def pending(self, now: datetime) -> list[Pending]:
        async with self._conn.execute(
            "SELECT a.id, a.short_id, a.expires_at, d.text AS draft, "
            "COALESCE(c.display_name, '~' || c.push_name, m.chat_jid) AS who, "
            "(SELECT group_concat(x.text, ' / ') FROM messages x WHERE x.burst_id = de.burst_id) "
            "AS incoming FROM approvals a JOIN drafts d ON d.id = a.draft_id "
            "JOIN decisions de ON de.id = a.decision_id JOIN messages m ON m.id = de.message_id "
            "LEFT JOIN contacts c ON c.wa_jid = m.chat_jid OR c.wa_lid = m.chat_jid "
            "WHERE a.status = 'pending' AND a.expires_at > ? ORDER BY a.id",
            (to_iso(now),),
        ) as cur:
            rows = await cur.fetchall()
        return [
            Pending(
                r["id"],
                r["short_id"],
                r["who"],
                r["incoming"] or "",
                r["draft"],
                from_iso(r["expires_at"]),
            )
            for r in rows
        ]

    async def decide(
        self,
        action: str,
        *,
        now: datetime,
        channel: str,
        approval_id: int | None = None,
        short_id: int | None = None,
        text: str | None = None,
    ) -> str:
        """approve | edit | reject. Returns a user-facing message."""
        if action not in ("approve", "edit", "reject"):
            raise ValueError(action)
        row = await self._row(approval_id=approval_id, short_id=short_id, now=now)
        label = f"#{short_id}" if short_id is not None else "that draft"
        if row is None:
            return f"There's no draft {label} today."
        label = f"#{row['short_id']}"
        if row["status"] != "pending":
            return f"Draft {label} is already {row['status']}."
        if from_iso(row["expires_at"]) <= now:
            await self._set(row, "expired", channel, None, now)
            return f"Draft {label} has expired. Reply yourself if it still matters."
        if action == "reject":
            await self._set(row, "rejected", channel, None, now)
            return f"Rejected {label}. Nothing will be sent."

        if action == "edit":
            final = (text or "").strip()
            if not final:
                return "The edited text is empty."
        else:
            final = row["draft_text"].strip()
            signature = self._policy.user.signature
            disclose = row["disclose_ai"] is None or bool(row["disclose_ai"])
            if disclose and signature and signature not in final:
                final = f"{final} {signature}"
        try:
            await enqueue(
                self._conn,
                chat_jid=row["chat_jid"],
                text=final,
                level=row["final_level"],
                now=now,
                approval_id=row["id"],
                decision_id=row["decision_id"],
            )
        except OutboxError as exc:
            return f"Can't send {label}: {exc}"
        status = "edited" if action == "edit" else "approved"
        await self._set(row, status, channel, final, now)
        if action == "edit" and row["draft_text"].strip() != final:
            await self._learn(row, final, now)
        note = " (dry run is on: it will be recorded, not sent)" if self._policy.dry_run else ""
        return f"Sending {label} in a few seconds{note}."

    async def _set(
        self, row: aiosqlite.Row, status: str, channel: str, final: str | None, now: datetime
    ) -> None:
        await self._conn.execute(
            "UPDATE approvals SET status = ?, channel = ?, final_text = ?, decided_at = ? "
            "WHERE id = ?",
            (status, channel, final, to_iso(now), row["id"]),
        )
        await self._conn.commit()
        await self._audit.record(
            "approvals",
            f"approval.{status}",
            {"approval_id": row["id"], "short_id": row["short_id"], "channel": channel},
        )

    async def _learn(self, row: aiosqlite.Row, final: str, now: datetime) -> None:
        """Your edits become tone examples for this contact (used from M9)."""
        async with self._conn.execute(
            "SELECT id FROM contacts WHERE wa_jid = ? OR wa_lid = ?",
            (row["chat_jid"], row["chat_jid"]),
        ) as cur:
            contact = await cur.fetchone()
        if contact is not None:
            await self._conn.execute(
                "INSERT INTO tone_examples (contact_id, draft, final, ts) VALUES (?, ?, ?, ?)",
                (contact[0], row["draft_text"], final, to_iso(now)),
            )
            await self._conn.commit()

    async def expire_due(self, now: datetime) -> int:
        cur = await self._conn.execute(
            "UPDATE approvals SET status = 'expired', decided_at = ? "
            "WHERE status = 'pending' AND expires_at <= ?",
            (to_iso(now), to_iso(now)),
        )
        await self._conn.commit()
        return cur.rowcount


def summarise(p: list[Pending]) -> str:
    if not p:
        return "Nothing is waiting for approval."
    lines = [f"{len(p)} waiting:"]
    for x in p:
        lines.append(f"#{x.short_id} {x.who}: {x.incoming[:60]!r} -> draft: {x.draft[:80]!r}")
    lines.append(
        "Reply y<number> to send, n<number> to drop, e<number> <text> to send your own words."
    )
    return "\n".join(lines)


__all__ = ["ApprovalService", "Pending", "create_for_draft", "summarise"]
