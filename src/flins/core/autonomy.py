"""Who has earned unreviewed AI replies (SPEC §14 M9).

A contact qualifies only when you opted them in **and** you approved at least `min_approved` of
FLINS's AI drafts for them, with at most `max_edit_rate` of the drafts you decided on edited or
rejected. Everything else about the decision (tier, mode, category, sensitivity, guard, red
flag, outbox checks) still applies on top.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from flins.core.audit import AuditLog
from flins.core.db import utcnow_iso
from flins.core.policy_schema import Policy


@dataclass(frozen=True)
class AutoStats:
    approved: int  # sent exactly as drafted
    edited: int  # sent after you changed it
    rejected: int  # you said no
    optin: bool

    @property
    def decided(self) -> int:
        return self.approved + self.edited + self.rejected

    @property
    def accepted(self) -> int:
        """Drafts you sent (as is or edited)."""
        return self.approved + self.edited

    @property
    def edit_rate(self) -> float:
        return (self.edited + self.rejected) / self.decided if self.decided else 1.0

    def unlocked(self, policy: Policy) -> bool:
        rule = policy.auto_unlock
        return (
            self.optin
            and self.accepted >= rule.min_approved
            and self.edit_rate <= rule.max_edit_rate
        )

    def describe(self, policy: Policy) -> str:
        rule = policy.auto_unlock
        state = "earned" if self.unlocked(policy) else "not yet"
        if not self.optin:
            state = "off (not opted in)"
        return (
            f"{state}: {self.accepted}/{rule.min_approved} drafts sent, "
            f"{self.edited} edited, {self.rejected} rejected "
            f"({self.edit_rate:.0%} changed, limit {rule.max_edit_rate:.0%})"
        )


async def auto_stats(conn: aiosqlite.Connection, contact_id: int) -> AutoStats:
    """Count your decisions on FLINS's AI drafts (not templates or skills) for this contact."""
    async with conn.execute(
        "SELECT wa_jid, wa_lid, auto_optin FROM contacts WHERE id = ?", (contact_id,)
    ) as cur:
        c = await cur.fetchone()
    if c is None:
        return AutoStats(0, 0, 0, False)
    async with conn.execute(
        """
        SELECT a.status, COUNT(*) FROM approvals a
        JOIN drafts dr ON dr.id = a.draft_id
        JOIN decisions d ON d.id = dr.decision_id
        JOIN messages m ON m.id = d.message_id
        WHERE (m.chat_jid = ? OR m.chat_jid = ?) AND dr.source = 'llm'
          AND a.status IN ('approved', 'edited', 'rejected')
        GROUP BY a.status
        """,
        (c["wa_jid"], c["wa_lid"] or c["wa_jid"]),
    ) as cur:
        counts = {r[0]: int(r[1]) for r in await cur.fetchall()}
    return AutoStats(
        counts.get("approved", 0),
        counts.get("edited", 0),
        counts.get("rejected", 0),
        bool(c["auto_optin"]),
    )


async def set_optin(conn: aiosqlite.Connection, contact_id: int, on: bool, via: str) -> None:
    """Opt a contact in or out of earned auto replies. Audited."""
    await conn.execute(
        "UPDATE contacts SET auto_optin = ?, updated_at = ? WHERE id = ?",
        (int(on), utcnow_iso(), contact_id),
    )
    await conn.commit()
    await AuditLog(conn).record(
        "user", "contact.auto_optin", {"contact_id": contact_id, "on": on, "via": via}
    )
