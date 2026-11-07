"""SQLite access and migrations (SPEC §11)."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import aiosqlite

MIGRATIONS_PACKAGE = "flins.migrations"


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _migration_files() -> list[tuple[str, str]]:
    """(version, sql) pairs, sorted by file name, e.g. ('0001_init', '...')."""
    pkg = resources.files(MIGRATIONS_PACKAGE)
    files = sorted(
        (f for f in pkg.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name
    )
    return [(f.name.removesuffix(".sql"), f.read_text(encoding="utf-8")) for f in files]


async def connect(db_path: Path) -> aiosqlite.Connection:
    """Open the database with WAL and foreign keys on. Creates the parent folder."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.execute("PRAGMA busy_timeout=5000")
    return conn


async def migrate(conn: aiosqlite.Connection) -> list[str]:
    """Apply pending migrations in order. Returns the versions applied this call."""
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    async with conn.execute("SELECT version FROM schema_migrations") as cur:
        done = {row[0] for row in await cur.fetchall()}

    applied: list[str] = []
    for version, sql in _migration_files():
        if version in done:
            continue
        # executescript commits first, so wrap the migration in its own transaction.
        await conn.executescript(
            "BEGIN;\n"
            + sql
            + f"\nINSERT INTO schema_migrations (version, applied_at) "
            f"VALUES ('{version}', '{utcnow_iso()}');\nCOMMIT;"
        )
        applied.append(version)
    return applied


async def table_names(conn: aiosqlite.Connection) -> set[str]:
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ) as cur:
        return {row[0] for row in await cur.fetchall()}
