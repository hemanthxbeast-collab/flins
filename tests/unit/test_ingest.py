from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from flins.adapters.base import (
    ConnectionChanged,
    ContactInfo,
    ContactsUpsert,
    InboundMessage,
    MeInfo,
)
from flins.core.audit import AuditLog
from flins.core.db import connect, migrate
from flins.core.events import Event, EventBus
from flins.core.ingest import ContactsResult, Ingestor, StoredMessage, canonical

ME = "919000000000@s.whatsapp.net"
RAHUL_PN = "919111111111@s.whatsapp.net"
RAHUL_LID = "123456789012345@lid"
MOM_PN = "919222222222@s.whatsapp.net"
GROUP = "120363000000000000@g.us"


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    c = await connect(tmp_path / "t.db")
    await migrate(c)
    yield c
    await c.close()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def ingest(conn: aiosqlite.Connection, bus: EventBus) -> Ingestor:
    return Ingestor(conn, bus, AuditLog(conn))


def msg(**kw: Any) -> InboundMessage:
    base: dict[str, Any] = {
        "wa_msg_id": "M1",
        "chat_jid": RAHUL_PN,
        "sender_jid": RAHUL_PN,
        "ts_ms": 1_762_500_000_000,
        "type": "text",
        "text": "hey",
        "push_name": "Rahul",
    }
    base.update(kw)
    return InboundMessage(**base)


async def contact(conn: aiosqlite.Connection, jid: str) -> aiosqlite.Row | None:
    async with conn.execute("SELECT * FROM contacts WHERE wa_jid = ?", (jid,)) as cur:
        return await cur.fetchone()


async def count(conn: aiosqlite.Connection, table: str) -> int:
    async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def test_canonical_prefers_phone_number() -> None:
    assert canonical(RAHUL_LID, RAHUL_PN) == RAHUL_PN
    assert canonical(RAHUL_PN, RAHUL_LID) == RAHUL_PN
    assert canonical(RAHUL_LID, None) == RAHUL_LID
    assert canonical(GROUP, None) == GROUP


async def test_stores_message_creates_unknown_contact_and_publishes(
    ingest: Ingestor, conn: aiosqlite.Connection, bus: EventBus
) -> None:
    seen: list[Event] = []

    async def on_msg(e: Event) -> None:
        seen.append(e)

    bus.subscribe("wa.message", on_msg)
    stored = await ingest.handle(msg())
    assert isinstance(stored, StoredMessage)
    assert stored.direction == "IN"
    assert stored.contact_name == "Rahul"
    row = await contact(conn, RAHUL_PN)
    assert row is not None and row["tier"] == "unknown" and row["push_name"] == "Rahul"
    assert len(seen) == 1
    assert "text" not in seen[0].data  # bus events carry ids, not message text


async def test_duplicate_is_ignored(
    ingest: Ingestor, conn: aiosqlite.Connection, bus: EventBus
) -> None:
    seen: list[Event] = []

    async def on_msg(e: Event) -> None:
        seen.append(e)

    bus.subscribe("wa.message", on_msg)
    assert await ingest.handle(msg()) is not None
    assert await ingest.handle(msg()) is None
    assert await count(conn, "messages") == 1
    assert len(seen) == 1


@pytest.mark.parametrize("chat", ["status@broadcast", "123@broadcast", "1203@newsletter"])
async def test_ignored_chats(ingest: Ingestor, conn: aiosqlite.Connection, chat: str) -> None:
    assert await ingest.handle(msg(chat_jid=chat)) is None
    assert await count(conn, "messages") == 0


async def test_own_message_is_out_and_uses_me(ingest: Ingestor, conn: aiosqlite.Connection) -> None:
    await ingest.handle(ConnectionChanged(state="open", me=MeInfo(id=ME)))
    stored = await ingest.handle(msg(wa_msg_id="O1", from_me=True, sender_jid="me", push_name=None))
    assert isinstance(stored, StoredMessage)
    assert stored.direction == "OUT"
    async with conn.execute("SELECT sender_jid FROM messages WHERE wa_msg_id='O1'") as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == ME


async def test_group_message_creates_no_contact(
    ingest: Ingestor, conn: aiosqlite.Connection
) -> None:
    stored = await ingest.handle(
        msg(chat_jid=GROUP, is_group=True, sender_jid=RAHUL_LID, sender_jid_alt=RAHUL_PN)
    )
    assert isinstance(stored, StoredMessage)
    assert stored.chat_jid == GROUP
    assert await count(conn, "contacts") == 0


async def test_address_book_import_and_upgrade(
    ingest: Ingestor, conn: aiosqlite.Connection
) -> None:
    await ingest.handle(msg())  # Rahul arrives first as unknown
    result = await ingest.handle(
        ContactsUpsert(
            contacts=[
                ContactInfo(pn=RAHUL_PN, name="Rahul Work"),
                ContactInfo(pn=MOM_PN, name="Mom"),
                ContactInfo(lid=RAHUL_LID, pn=RAHUL_PN),
            ]
        )
    )
    assert result == ContactsResult(created=1, updated=2)
    rahul = await contact(conn, RAHUL_PN)
    assert rahul is not None
    assert (rahul["tier"], rahul["display_name"], rahul["wa_lid"]) == (
        "known",
        "Rahul Work",
        RAHUL_LID,
    )
    mom = await contact(conn, MOM_PN)
    assert mom is not None and mom["tier"] == "known"


async def test_user_tier_is_never_overwritten(ingest: Ingestor, conn: aiosqlite.Connection) -> None:
    await ingest.handle(ContactsUpsert(contacts=[ContactInfo(pn=MOM_PN, name="Mom")]))
    await conn.execute("UPDATE contacts SET tier='inner' WHERE wa_jid=?", (MOM_PN,))
    await conn.commit()
    await ingest.handle(ContactsUpsert(contacts=[ContactInfo(pn=MOM_PN, name="Amma")]))
    await ingest.handle(msg(wa_msg_id="X", chat_jid=MOM_PN, sender_jid=MOM_PN, push_name="mom"))
    row = await contact(conn, MOM_PN)
    assert row is not None and row["tier"] == "inner" and row["display_name"] == "Amma"


async def test_lid_only_contact_learns_phone_number(
    ingest: Ingestor, conn: aiosqlite.Connection
) -> None:
    await ingest.handle(msg(chat_jid=RAHUL_LID, sender_jid=RAHUL_LID))
    assert await contact(conn, RAHUL_LID) is not None
    await ingest.handle(ContactsUpsert(contacts=[ContactInfo(pn=RAHUL_PN, lid=RAHUL_LID)]))
    assert await contact(conn, RAHUL_LID) is None
    row = await contact(conn, RAHUL_PN)
    assert row is not None and row["wa_lid"] == RAHUL_LID
    # A later LID-addressed message lands on the same contact.
    stored = await ingest.handle(msg(wa_msg_id="M2", chat_jid=RAHUL_LID, sender_jid=RAHUL_LID))
    assert isinstance(stored, StoredMessage) and stored.contact_id == row["id"]
    assert await count(conn, "contacts") == 1


async def audit_count(conn: aiosqlite.Connection, kind: str) -> int:
    async with conn.execute("SELECT COUNT(*) FROM audit_log WHERE kind = ?", (kind,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def two_rows_for_rahul(ingest: Ingestor, conn: aiosqlite.Connection) -> None:
    await ingest.handle(msg(wa_msg_id="A", chat_jid=RAHUL_LID, sender_jid=RAHUL_LID))
    await ingest.handle(msg(wa_msg_id="B", chat_jid=RAHUL_PN, sender_jid=RAHUL_PN))
    assert await count(conn, "contacts") == 2


async def test_nameless_entries_never_create_contacts(
    ingest: Ingestor, conn: aiosqlite.Connection
) -> None:
    result = await ingest.handle(
        ContactsUpsert(
            contacts=[
                ContactInfo(pn=RAHUL_PN, lid=RAHUL_LID),  # bare ID mapping
                ContactInfo(pn=MOM_PN, notify="Mommy"),  # self-chosen name only
            ]
        )
    )
    assert result == ContactsResult(created=0, updated=0)
    assert await count(conn, "contacts") == 0
    # They still fill in details once the person exists.
    await ingest.handle(msg(chat_jid=MOM_PN, sender_jid=MOM_PN, push_name=None))
    await ingest.handle(ContactsUpsert(contacts=[ContactInfo(pn=MOM_PN, notify="Mommy")]))
    row = await contact(conn, MOM_PN)
    assert row is not None and row["push_name"] == "Mommy" and row["tier"] == "unknown"


async def test_untouched_duplicate_is_merged_and_messages_follow(
    ingest: Ingestor, conn: aiosqlite.Connection
) -> None:
    await two_rows_for_rahul(ingest, conn)
    await ingest.handle(ContactsUpsert(contacts=[ContactInfo(pn=RAHUL_PN, lid=RAHUL_LID)]))
    assert await count(conn, "contacts") == 1
    row = await contact(conn, RAHUL_PN)
    assert row is not None and row["wa_lid"] == RAHUL_LID
    async with conn.execute("SELECT DISTINCT chat_jid FROM messages") as cur:
        assert [r[0] for r in await cur.fetchall()] == [RAHUL_PN]
    assert await audit_count(conn, "contacts.merged") == 1
    assert await audit_count(conn, "contacts.merge_needed") == 0


async def test_user_edited_row_survives_merge(ingest: Ingestor, conn: aiosqlite.Connection) -> None:
    await two_rows_for_rahul(ingest, conn)
    await conn.execute("UPDATE contacts SET tier='trusted' WHERE wa_jid=?", (RAHUL_LID,))
    await ingest.handle(ContactsUpsert(contacts=[ContactInfo(pn=RAHUL_PN, lid=RAHUL_LID)]))
    assert await count(conn, "contacts") == 1
    row = await contact(conn, RAHUL_PN)
    assert row is not None and row["tier"] == "trusted" and row["wa_lid"] == RAHUL_LID


async def test_both_rows_edited_are_flagged_not_merged(
    ingest: Ingestor, conn: aiosqlite.Connection
) -> None:
    await two_rows_for_rahul(ingest, conn)
    await conn.execute("UPDATE contacts SET tier='trusted' WHERE wa_jid=?", (RAHUL_LID,))
    await conn.execute("UPDATE contacts SET notes='work' WHERE wa_jid=?", (RAHUL_PN,))
    await ingest.handle(ContactsUpsert(contacts=[ContactInfo(pn=RAHUL_PN, lid=RAHUL_LID)]))
    assert await count(conn, "contacts") == 2
    assert await audit_count(conn, "contacts.merge_needed") == 1
    assert await audit_count(conn, "contacts.merged") == 0


async def test_connection_changes_are_audited_once(
    ingest: Ingestor, conn: aiosqlite.Connection
) -> None:
    await ingest.handle(ConnectionChanged(state="open"))
    await ingest.handle(ConnectionChanged(state="open"))
    await ingest.handle(ConnectionChanged(state="logged_out", reason_code=401))
    async with conn.execute("SELECT COUNT(*) FROM audit_log WHERE kind='wa.connection'") as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == 2
