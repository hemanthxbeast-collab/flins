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


def test_sidecar_has_no_send_command_yet() -> None:
    # M1 is read-only: the sidecar must not call Baileys' sendMessage anywhere.
    for path in SIDECAR_SRC.rglob("*.ts"):
        if "test" in path.parts:
            continue
        assert "sendMessage" not in path.read_text(encoding="utf-8"), path.name


def test_sidecar_keeps_phone_notifications_and_no_read_receipts() -> None:
    index = (SIDECAR_SRC / "index.ts").read_text(encoding="utf-8")
    assert "markOnlineOnConnect: false" in index
    assert "readMessages" not in index
