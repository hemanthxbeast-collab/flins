from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from flins.core.audit import AuditLog
from flins.core.db import connect, migrate, table_names

EXPECTED_TABLES = {
    "contacts",
    "bursts",
    "messages",
    "decisions",
    "drafts",
    "approvals",
    "outbox",
    "mode_state",
    "mode_history",
    "skills",
    "skill_runs",
    "tone_examples",
    "audit_log",
    "schema_migrations",
}


async def test_migrate_creates_all_tables_and_is_idempotent(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "data" / "t.db")
    try:
        applied = await migrate(conn)
        assert applied[0] == "0001_init"
        assert applied == sorted(applied)
        assert await table_names(conn) >= EXPECTED_TABLES
        assert await migrate(conn) == []
    finally:
        await conn.close()


async def test_prune_migration_keeps_anything_meaningful(tmp_path: Path) -> None:
    """0003 removes only auto-created, nameless, untouched contacts with no messages."""
    conn = await connect(tmp_path / "t.db")
    try:
        await migrate(conn)
        await conn.execute(
            "DELETE FROM schema_migrations WHERE version = '0003_prune_auto_contacts'"
        )
        rows = [
            # (wa_jid, tier, source, display_name, notes)  -> expected to survive?
            ("junk@lid", "unknown", "message", None, None),  # removed
            ("book@s.whatsapp.net", "known", "address_book", "Book", None),  # kept
            ("wrote@s.whatsapp.net", "unknown", "message", None, None),  # kept: has a message
            ("noted@s.whatsapp.net", "unknown", "message", None, "met at work"),  # kept
            ("trusted@s.whatsapp.net", "trusted", "message", None, None),  # kept
        ]
        for jid, tier, source, name, notes in rows:
            await conn.execute(
                "INSERT INTO contacts (wa_jid, tier, source, display_name, notes, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, 'now', 'now')",
                (jid, tier, source, name, notes),
            )
        await conn.execute(
            "INSERT INTO messages (wa_msg_id, chat_jid, sender_jid, direction, type, ts) "
            "VALUES ('m1', 'wrote@s.whatsapp.net', 'wrote@s.whatsapp.net', 'IN', 'text', 'now')"
        )
        await conn.commit()
        assert await migrate(conn) == ["0003_prune_auto_contacts"]
        async with conn.execute("SELECT wa_jid FROM contacts ORDER BY wa_jid") as cur:
            left = [r[0] for r in await cur.fetchall()]
        assert left == sorted(r[0] for r in rows[1:])
    finally:
        await conn.close()


async def test_wal_and_foreign_keys(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "t.db")
    try:
        async with conn.execute("PRAGMA journal_mode") as cur:
            assert (await cur.fetchone())[0].lower() == "wal"  # type: ignore[index]
        async with conn.execute("PRAGMA foreign_keys") as cur:
            assert (await cur.fetchone())[0] == 1  # type: ignore[index]
    finally:
        await conn.close()


async def test_audit_is_append_only(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "t.db")
    try:
        await migrate(conn)
        audit = AuditLog(conn)
        row_id = await audit.record("test", "unit.check", {"n": 1, "text": "héllo"})
        rows = await audit.recent()
        assert rows[0]["id"] == row_id
        assert rows[0]["payload"] == {"n": 1, "text": "héllo"}
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            await conn.execute("UPDATE audit_log SET kind='tampered' WHERE id=?", (row_id,))
    finally:
        await conn.close()


async def test_constraints_reject_bad_values(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "t.db")
    try:
        await migrate(conn)
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                "INSERT INTO contacts (wa_jid, tier, created_at, updated_at) "
                "VALUES ('x@s.whatsapp.net', 'vip', 'now', 'now')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                "INSERT INTO mode_state (id, mode, started_at, started_by) "
                "VALUES (2, 'NORMAL', 'now', 'test')"
            )
    finally:
        await conn.close()
