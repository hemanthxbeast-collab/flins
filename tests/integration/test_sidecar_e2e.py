"""Start the real Node sidecar (unpaired) and do a real signed handshake with it."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from flins.adapters.baileys import BaileysAdapter, SidecarAuthError
from flins.adapters.base import SidecarHello
from flins.adapters.sidecar_proc import Sidecar
from tests.conftest import REPO_ROOT

SECRET = "e2e-secret-" + "z" * 32
SCRIPT = REPO_ROOT / "wa-sidecar" / "dist" / "index.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not SCRIPT.is_file(),
    reason="node or built sidecar not available",
)


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_real_sidecar_handshake_unpaired(tmp_path: Path) -> None:
    sidecar = Sidecar(REPO_ROOT, tmp_path, free_port())
    await sidecar.start(SECRET)
    adapter = BaileysAdapter(sidecar.port, SECRET)
    try:
        async with asyncio.timeout(10):
            async for event in adapter.events():
                assert isinstance(event, SidecarHello)
                assert event.state == "not_paired"
                break
        await adapter.request_sidecar_shutdown()
    finally:
        await adapter.close()
        await sidecar.stop()
    assert not (tmp_path / "wa-auth" / "creds.json").exists()


async def test_real_sidecar_rejects_wrong_secret(tmp_path: Path) -> None:
    sidecar = Sidecar(REPO_ROOT, tmp_path, free_port())
    await sidecar.start(SECRET)
    adapter = BaileysAdapter(sidecar.port, "not-the-secret-" + "q" * 32)
    good = BaileysAdapter(sidecar.port, SECRET)
    try:
        with pytest.raises(SidecarAuthError):
            async with asyncio.timeout(10):
                async for _ in adapter.events():
                    pass
        async with asyncio.timeout(10):
            async for _ in good.events():
                break
        await good.request_sidecar_shutdown()
    finally:
        await adapter.close()
        await good.close()
        await sidecar.stop()
