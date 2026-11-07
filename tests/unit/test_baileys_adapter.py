"""BaileysAdapter against a fake sidecar written in Python."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from websockets.asyncio.server import ServerConnection, serve

from flins.adapters.baileys import BaileysAdapter, SidecarAuthError
from flins.adapters.base import AdapterEvent, ConnectionChanged, InboundMessage, SidecarHello
from flins.adapters.protocol import FrameVerifier, ProtocolError, sign_frame

SECRET = "s" * 40
Handler = Callable[[ServerConnection], Awaitable[None]]


async def start_server(handler: Handler) -> tuple[int, Callable[[], Awaitable[None]]]:
    server = await serve(handler, "127.0.0.1", 0)
    port = next(iter(server.sockets)).getsockname()[1]

    async def stop() -> None:
        server.close()
        await server.wait_closed()

    return port, stop


async def take(it: AsyncIterator[AdapterEvent], n: int) -> list[AdapterEvent]:
    out: list[AdapterEvent] = []
    async with asyncio.timeout(5):
        async for e in it:
            out.append(e)
            if len(out) == n:
                break
    return out


def inbound_payload(msg_id: str) -> dict[str, object]:
    return {
        "wa_msg_id": msg_id,
        "chat_jid": "1@s.whatsapp.net",
        "sender_jid": "1@s.whatsapp.net",
        "ts_ms": 1,
        "type": "text",
        "text": "hi",
    }


async def test_handshake_events_and_forged_frames_dropped() -> None:
    hellos: list[str] = []

    async def sidecar(ws: ServerConnection) -> None:
        body = FrameVerifier(SECRET).verify(await ws.recv())
        hellos.append(body.kind)
        await ws.send(sign_frame(SECRET, "hello.ok", {"sidecar_version": "0.1.0", "state": "open"}))
        await ws.send(sign_frame("wrong-secret" * 4, "message.received", inbound_payload("FORGED")))
        await ws.send(json.dumps({"body": "{}", "sig": "00"}))
        await ws.send(sign_frame(SECRET, "message.received", inbound_payload("REAL")))
        await ws.send(sign_frame(SECRET, "something.new", {}))
        await ws.send(sign_frame(SECRET, "connection", {"state": "close", "reason_code": 428}))
        await ws.wait_closed()

    port, stop = await start_server(sidecar)
    adapter = BaileysAdapter(port, SECRET)
    try:
        events = await take(adapter.events(), 3)
    finally:
        await adapter.close()
        await stop()

    assert hellos == ["hello"]
    assert isinstance(events[0], SidecarHello) and events[0].state == "open"
    assert isinstance(events[1], InboundMessage) and events[1].wa_msg_id == "REAL"
    assert isinstance(events[2], ConnectionChanged) and events[2].reason_code == 428


async def test_reconnects_after_sidecar_drops() -> None:
    connections = 0

    async def sidecar(ws: ServerConnection) -> None:
        nonlocal connections
        connections += 1
        FrameVerifier(SECRET).verify(await ws.recv())
        await ws.send(sign_frame(SECRET, "hello.ok", {"sidecar_version": "0.1.0", "state": "open"}))
        if connections == 1:
            await ws.close()
        else:
            await ws.wait_closed()

    port, stop = await start_server(sidecar)
    adapter = BaileysAdapter(port, SECRET)
    try:
        events = await take(adapter.events(), 3)
    finally:
        await adapter.close()
        await stop()

    assert [type(e).__name__ for e in events] == [
        "SidecarHello",
        "ConnectionChanged",
        "SidecarHello",
    ]
    assert isinstance(events[1], ConnectionChanged) and events[1].state == "sidecar_down"
    assert connections == 2


async def test_wrong_secret_is_fatal() -> None:
    async def sidecar(ws: ServerConnection) -> None:
        try:
            FrameVerifier(SECRET).verify(await ws.recv())
        except ProtocolError:
            await ws.close(4401, "bad signature")

    port, stop = await start_server(sidecar)
    adapter = BaileysAdapter(port, "w" * 40)
    try:
        with pytest.raises(SidecarAuthError):
            await take(adapter.events(), 1)
    finally:
        await adapter.close()
        await stop()


async def test_send_text_refuses_until_m4() -> None:
    adapter = BaileysAdapter(1, SECRET)
    with pytest.raises(NotImplementedError, match="M4"):
        await adapter.send_text("x", "y", quote=None, client_msg_id="c")
