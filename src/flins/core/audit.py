"""Append-only audit log (CLAUDE.md invariant 10)."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from flins.core.db import utcnow_iso


class AuditLog:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def record(self, actor: str, kind: str, payload: dict[str, Any]) -> int:
        """Write one audit row and return its id. `payload` must be JSON-serialisable."""
        cur = await self._conn.execute(
            "INSERT INTO audit_log (ts, actor, kind, payload) VALUES (?, ?, ?, ?)",
            (utcnow_iso(), actor, kind, json.dumps(payload, default=str, ensure_ascii=False)),
        )
        await self._conn.commit()
        row_id = cur.lastrowid
        assert row_id is not None
        return row_id

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self._conn.execute(
            "SELECT id, ts, actor, kind, payload FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"id": r[0], "ts": r[1], "actor": r[2], "kind": r[3], "payload": json.loads(r[4])}
            for r in rows
        ]
