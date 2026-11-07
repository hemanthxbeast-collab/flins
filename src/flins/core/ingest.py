"""Store what the WhatsApp adapter reports: messages, contacts, connection changes.

Ingest never decides anything. It records, deduplicates, and publishes bus events
(`wa.message`, `wa.connection`) that later milestones act on.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import aiosqlite

from flins.adapters.base import (
    AdapterEvent,
    ConnectionChanged,
    ContactInfo,
    ContactsUpsert,
    InboundMessage,
    SidecarHello,
)
from flins.core.audit import AuditLog
from flins.core.db import utcnow_iso
from flins.core.events import EventBus

log = logging.getLogger(__name__)

PN_SUFFIX = "@s.whatsapp.net"
LID_SUFFIX = "@lid"
IGNORED_SUFFIXES = ("@broadcast", "@newsletter")

ContactSource = Literal["address_book", "message", "manual"]

# Tiers ingest may assign by itself. Anything else was set by the user.
AUTO_TIERS = frozenset({"unknown", "known"})
# Contact fields only the user sets. A row with any of these is never merged away.
USER_FIELDS = ("notes", "max_level", "tone_profile", "language", "allowed_categories")


def is_pn(jid: str | None) -> bool:
    return bool(jid) and jid.endswith(PN_SUFFIX)  # type: ignore[union-attr]


def is_lid(jid: str | None) -> bool:
    return bool(jid) and jid.endswith(LID_SUFFIX)  # type: ignore[union-attr]


def is_ignored_chat(jid: str) -> bool:
    return jid.endswith(IGNORED_SUFFIXES)


def split_identity(jid: str, alt: str | None) -> tuple[str | None, str | None]:
    """Return (phone-number JID, LID) from a JID and its alternative, where known."""
    ids = [j for j in (jid, alt) if j]
    pn = next((j for j in ids if is_pn(j)), None)
    lid = next((j for j in ids if is_lid(j)), None)
    return pn, lid


def normalize_jid(jid: str) -> str:
    """Drop the device part: '9190...:12@s.whatsapp.net' -> '9190...@s.whatsapp.net'."""
    user, sep, server = jid.partition("@")
    return user.split(":", 1)[0] + sep + server


def canonical(jid: str, alt: str | None) -> str:
    """Prefer the phone-number JID. Groups and other JIDs pass through unchanged."""
    pn, lid = split_identity(jid, alt)
    return pn or lid or jid


@dataclass(frozen=True)
class StoredMessage:
    id: int
    wa_msg_id: str
    chat_jid: str
    direction: Literal["IN", "OUT"]
    is_group: bool
    type: str
    text: str | None
    ts: str
    contact_id: int | None
    contact_name: str | None
    self_chat: bool = False
    mentions_me: bool = False


@dataclass(frozen=True)
class ContactsResult:
    created: int
    updated: int


class Ingestor:
    def __init__(self, conn: aiosqlite.Connection, bus: EventBus, audit: AuditLog) -> None:
        self._conn = conn
        self._bus = bus
        self._audit = audit
        self.me: str | None = None  # the user's own phone-number JID, without device part
        self.me_lid: str | None = None
        self.wa_state: str = "unknown"

    def set_me(self, pn: str | None, lid: str | None = None) -> None:
        if pn:
            self.me = normalize_jid(pn)
        if lid:
            self.me_lid = normalize_jid(lid)

    def is_me(self, jid: str | None) -> bool:
        return bool(jid) and normalize_jid(jid) in {self.me, self.me_lid} - {None}  # type: ignore[arg-type]

    async def handle(self, event: AdapterEvent) -> StoredMessage | ContactsResult | None:
        if isinstance(event, InboundMessage):
            return await self.on_message(event)
        if isinstance(event, ContactsUpsert):
            return await self.on_contacts(event.contacts)
        if isinstance(event, ConnectionChanged | SidecarHello):
            await self.on_connection(event)
        return None

    # ------------------------------------------------------------------ connection

    async def on_connection(self, event: ConnectionChanged | SidecarHello) -> None:
        if event.me:
            self.set_me(event.me.id, event.me.lid)
        if event.state == self.wa_state:
            return
        self.wa_state = event.state
        data = event.model_dump(exclude_none=True)
        await self._audit.record("wa", "wa.connection", data)
        await self._bus.publish("wa.connection", data)

    # ------------------------------------------------------------------ contacts

    async def on_contacts(self, contacts: list[ContactInfo]) -> ContactsResult:
        """Address-book entries (with a saved name) create contacts. Entries without a
        name (ID mappings, people's self-chosen names) only fill in details on contacts
        that already exist, so people you've never chatted with don't become contacts."""
        created = updated = 0
        for c in contacts:
            if not c.pn and not c.lid:
                continue
            _, was_created, was_updated = await self._resolve_contact(
                c.pn,
                c.lid,
                name=c.name,
                push_name=c.notify,
                source="address_book" if c.name else "message",
                create=bool(c.name),
            )
            created += was_created
            updated += was_updated
        await self._conn.commit()
        return ContactsResult(created, updated)

    async def _find(self, column: str, value: str) -> aiosqlite.Row | None:
        assert column in {"wa_jid", "wa_lid"}
        async with self._conn.execute(
            f"SELECT * FROM contacts WHERE {column} = ?", (value,)
        ) as cur:
            return await cur.fetchone()

    async def _user_edited(self, row: aiosqlite.Row) -> bool:
        """True if the user has set anything on this contact that a merge could lose."""
        if row["tier"] not in AUTO_TIERS or row["never_mention"] not in (None, "[]"):
            return True
        if any(row[f] is not None for f in USER_FIELDS):
            return True
        async with self._conn.execute(
            "SELECT 1 FROM tone_examples WHERE contact_id = ? LIMIT 1", (row["id"],)
        ) as cur:
            return await cur.fetchone() is not None

    async def _rewrite_message_jids(self, old: list[str], new: str) -> None:
        for jid in old:
            if jid and jid != new:
                await self._conn.execute(
                    "UPDATE messages SET chat_jid = ? WHERE chat_jid = ?", (new, jid)
                )
                await self._conn.execute(
                    "UPDATE messages SET sender_jid = ? WHERE sender_jid = ?", (new, jid)
                )

    async def _merge(self, row_pn: aiosqlite.Row, row_lid: aiosqlite.Row, pn: str, lid: str) -> int:
        """Merge two rows for one person, if that can't lose anything the user set.

        Returns the id of the surviving row. If both rows have user edits, nothing is merged
        and a `contacts.merge_needed` audit row asks the user to decide.
        """
        pn_edited = await self._user_edited(row_pn)
        lid_edited = await self._user_edited(row_lid)
        if pn_edited and lid_edited:
            await self._audit.record(
                "ingest",
                "contacts.merge_needed",
                {"pn_row": row_pn["id"], "lid_row": row_lid["id"]},
            )
            return int(row_pn["id"])

        keep, drop = (row_lid, row_pn) if lid_edited else (row_pn, row_lid)
        tier = keep["tier"]
        if tier == "unknown" and drop["tier"] == "known":
            tier = "known"
        source = (
            "address_book" if "address_book" in (keep["source"], drop["source"]) else keep["source"]
        )
        await self._conn.execute("DELETE FROM contacts WHERE id = ?", (drop["id"],))
        await self._conn.execute(
            "UPDATE contacts SET wa_jid = ?, wa_lid = ?, tier = ?, source = ?, "
            "display_name = COALESCE(display_name, ?), push_name = COALESCE(push_name, ?), "
            "updated_at = ? WHERE id = ?",
            (
                pn,
                lid,
                tier,
                source,
                drop["display_name"],
                drop["push_name"],
                utcnow_iso(),
                keep["id"],
            ),
        )
        await self._rewrite_message_jids([lid, drop["wa_jid"], keep["wa_jid"]], pn)
        await self._audit.record(
            "ingest", "contacts.merged", {"kept": keep["id"], "dropped": drop["id"]}
        )
        return int(keep["id"])

    async def _resolve_contact(
        self,
        pn: str | None,
        lid: str | None,
        *,
        name: str | None,
        push_name: str | None,
        source: ContactSource,
        create: bool = True,
    ) -> tuple[int | None, bool, bool]:
        """Find (and, if `create`, create) the contact for this identity.

        Returns (id or None, created, updated). Never changes a tier the user set. The one
        automatic tier change is `unknown` → `known` when the person is in the address book.
        """
        now = utcnow_iso()
        row_pn = await self._find("wa_jid", pn) if pn else None
        row_lid = None
        if lid:
            row_lid = await self._find("wa_lid", lid) or await self._find("wa_jid", lid)

        row: aiosqlite.Row | None
        if row_pn is not None and row_lid is not None and row_pn["id"] != row_lid["id"]:
            assert pn is not None and lid is not None
            kept_id = await self._merge(row_pn, row_lid, pn, lid)
            async with self._conn.execute("SELECT * FROM contacts WHERE id = ?", (kept_id,)) as cur:
                row = await cur.fetchone()
            async with self._conn.execute(
                "SELECT COUNT(*) FROM contacts WHERE id IN (?, ?)", (row_pn["id"], row_lid["id"])
            ) as cur:
                remaining = await cur.fetchone()
            if remaining is not None and remaining[0] == 1:  # merged: one row now holds both ids
                row_pn = row_lid = row
            # else: both rows kept (flagged). row_lid still names the other row, so the
            # LID is not copied onto this one.
        else:
            row = row_pn if row_pn is not None else row_lid

        if row is None and not create:
            return None, False, False

        if row is None:
            tier = "known" if source == "address_book" else "unknown"
            cur = await self._conn.execute(
                "INSERT INTO contacts (wa_jid, wa_lid, display_name, push_name, tier, source, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pn or lid, lid, name, push_name, tier, source, now, now),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid, True, False

        changes: dict[str, object] = {}
        if pn and is_lid(row["wa_jid"]) and row_pn is None:
            changes["wa_jid"] = pn  # learned the phone number for a LID-only contact
            await self._rewrite_message_jids([row["wa_jid"]], pn)
        if lid and row["wa_lid"] is None and (row_lid is None or row_lid["id"] == row["id"]):
            changes["wa_lid"] = lid
        if name and name != row["display_name"]:
            changes["display_name"] = name
        if push_name and push_name != row["push_name"]:
            changes["push_name"] = push_name
        if source == "address_book" and name:
            if row["tier"] == "unknown":
                changes["tier"] = "known"
            if row["source"] == "message":
                changes["source"] = "address_book"
        if not changes:
            return row["id"], False, False
        changes["updated_at"] = now
        assignments = ", ".join(f"{k} = ?" for k in changes)
        await self._conn.execute(
            f"UPDATE contacts SET {assignments} WHERE id = ?", (*changes.values(), row["id"])
        )
        return row["id"], False, True

    # ------------------------------------------------------------------ messages

    async def on_message(self, m: InboundMessage) -> StoredMessage | None:
        if is_ignored_chat(m.chat_jid):
            return None

        direction: Literal["IN", "OUT"] = "OUT" if m.from_me else "IN"
        # "Message yourself" chat: FLINS's control channel (SPEC §5.1). Only the user can write
        # there, so only from_me messages count.
        self_chat = (
            not m.is_group and m.from_me and (self.is_me(m.chat_jid) or self.is_me(m.chat_jid_alt))
        )
        if self_chat:
            chat = self.me or normalize_jid(m.chat_jid)
        else:
            chat = m.chat_jid if m.is_group else canonical(m.chat_jid, m.chat_jid_alt)
        sender = (
            (self.me or m.sender_jid) if m.from_me else canonical(m.sender_jid, m.sender_jid_alt)
        )
        mentions_me = any(self.is_me(j) for j in m.mentions)

        contact_id: int | None = None
        contact_name: str | None = None
        if not m.is_group and not self_chat:
            if m.from_me:
                pn, lid = split_identity(m.chat_jid, m.chat_jid_alt)
                push = None
            else:
                pn, lid = split_identity(m.sender_jid, m.sender_jid_alt)
                push = m.push_name
            contact_id, _, _ = await self._resolve_contact(
                pn, lid, name=None, push_name=push, source="message", create=True
            )
            assert contact_id is not None
            async with self._conn.execute(
                "SELECT COALESCE(display_name, push_name, wa_jid) FROM contacts WHERE id = ?",
                (contact_id,),
            ) as cur:
                row = await cur.fetchone()
            contact_name = row[0] if row else None

        ts = datetime.fromtimestamp(m.ts_ms / 1000, UTC).isoformat(timespec="milliseconds")
        cur = await self._conn.execute(
            "INSERT OR IGNORE INTO messages (wa_msg_id, chat_jid, sender_jid, direction, type, "
            "text, quoted_id, ts, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                m.wa_msg_id,
                chat,
                sender,
                direction,
                m.type,
                m.text,
                m.quoted_id,
                ts,
                json.dumps(m.model_dump(), ensure_ascii=False),
            ),
        )
        await self._conn.commit()
        if cur.rowcount == 0:
            return None  # duplicate

        assert cur.lastrowid is not None
        stored = StoredMessage(
            id=cur.lastrowid,
            wa_msg_id=m.wa_msg_id,
            chat_jid=chat,
            direction=direction,
            is_group=m.is_group,
            type=m.type,
            text=m.text,
            ts=ts,
            contact_id=contact_id,
            contact_name=contact_name or ("You (self-chat)" if self_chat else None),
            self_chat=self_chat,
            mentions_me=mentions_me,
        )
        # Bus events carry ids only. Handlers read text from the DB.
        await self._bus.publish(
            "wa.message",
            {
                "message_id": stored.id,
                "wa_msg_id": stored.wa_msg_id,
                "chat_jid": chat,
                "direction": direction,
                "is_group": m.is_group,
                "contact_id": contact_id,
                "upsert_type": m.upsert_type,
                "self_chat": self_chat,
                "mentions_me": mentions_me,
            },
        )
        return stored
