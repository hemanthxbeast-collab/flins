"""Code-level checks for the safety invariants in CLAUDE.md."""

from __future__ import annotations

import re

from tests.conftest import REPO_ROOT

SRC = REPO_ROOT / "src" / "flins"
SIDECAR_SRC = REPO_ROOT / "wa-sidecar" / "src"

# Invariant 2: only the Outbox (from M4) may call an adapter's send method.
ALLOWED_SEND_CALLERS = {SRC / "core" / "outbox.py"}


def test_nothing_outside_adapters_calls_send_text() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.is_relative_to(SRC / "adapters") or path in ALLOWED_SEND_CALLERS:
            continue
        if re.search(r"\.send_text\s*\(", path.read_text(encoding="utf-8")):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == []


def test_sidecar_sends_only_plain_text_from_one_place() -> None:
    """M4: exactly one sendMessage call, in the send_text handler, text only (no media)."""
    hits = []
    for path in SIDECAR_SRC.rglob("*.ts"):
        if "test" in path.parts:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if "sendMessage(" in line:
                hits.append((path.name, line.strip()))
    assert len(hits) == 1, hits
    name, line = hits[0]
    assert name == "index.ts" and "{ text }" in line


def test_sidecar_refuses_groups_and_duplicates() -> None:
    index = (SIDECAR_SRC / "index.ts").read_text(encoding="utf-8")
    assert "isJidGroup(jid)" in index and "duplicate client_msg_id" in index


def test_sidecar_keeps_phone_notifications_and_no_read_receipts() -> None:
    index = (SIDECAR_SRC / "index.ts").read_text(encoding="utf-8")
    assert "markOnlineOnConnect: false" in index
    assert "readMessages" not in index
