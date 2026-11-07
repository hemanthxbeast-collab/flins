"""Core-side client for the Baileys sidecar. READ-ONLY until M4."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from flins import __version__
from flins.adapters.base import (
    AdapterEvent,
    AdapterHealth,
    ConnectionChanged,
    SendResult,
    SidecarHello,
    WAState,
    parse_event,
)
from flins.adapters.protocol import FrameVerifier, ProtocolError, sign_frame

log = logging.getLogger(__name__)

CLOSE_UNAUTHORIZED = 4401
CLOSE_BUSY = 4409
BUSY_RETRIES = 5


class SidecarAuthError(RuntimeError):
    """The sidecar rejected our handshake (wrong FLINS_WA_SECRET)."""


class SidecarBusyError(RuntimeError):
    """Another FLINS process is already connected to the sidecar."""


class BaileysAdapter:
    def __init__(
        self,
        port: int,
        secret: str,
        *,
        host: str = "127.0.0.1",
        reconnect_max_s: float = 30.0,
        open_timeout_s: float = 5.0,
    ) -> None:
        self._uri = f"ws://{host}:{port}/"
        self._secret = secret
        self._reconnect_max = reconnect_max_s
        self._open_timeout = open_timeout_s
        self._ws: ClientConnection | None = None
        self._closed = False
        self._wa_state: WAState = "sidecar_down"

    async def events(self) -> AsyncIterator[AdapterEvent]:
        """Yield verified events forever, reconnecting to the sidecar if it drops."""
        backoff = 0.5
        busy_count = 0
        while not self._closed:
            try:
                async with connect(
                    self._uri, open_timeout=self._open_timeout, max_size=4 * 2**20
                ) as ws:
                    self._ws = ws
                    verifier = FrameVerifier(self._secret)
                    await ws.send(
                        sign_frame(
                            self._secret, "hello", {"client": "flins-core", "version": __version__}
                        )
                    )
                    backoff = 0.5
                    async for raw in ws:
                        busy_count = 0
                        try:
                            body = verifier.verify(raw)
                            event = parse_event(body.kind, body.payload)
                        except (ProtocolError, ValidationError) as exc:
                            # Never act on a frame we can't verify. Log the reason, not the content.
                            log.warning(
                                "dropped sidecar frame: %s",
                                type(exc).__name__ + ": " + str(exc)[:120],
                            )
                            continue
                        if event is None:
                            continue
                        if isinstance(event, ConnectionChanged | SidecarHello):
                            self._wa_state = event.state
                        yield event
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
                if code == CLOSE_UNAUTHORIZED:
                    raise SidecarAuthError(
                        "sidecar rejected the handshake: FLINS_WA_SECRET differs"
                    ) from exc
                if code == CLOSE_BUSY:
                    # Can be brief (our previous connection still closing). Fatal if it persists.
                    busy_count += 1
                    if busy_count >= BUSY_RETRIES:
                        raise SidecarBusyError(
                            "another FLINS process is connected to the sidecar"
                        ) from exc
            except (OSError, TimeoutError, InvalidHandshake):
                pass
            finally:
                self._ws = None
            if self._closed:
                break
            if self._wa_state != "sidecar_down":
                self._wa_state = "sidecar_down"
                yield ConnectionChanged(state="sidecar_down")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._reconnect_max)

    async def send_text(
        self, chat_id: str, text: str, *, quote: str | None, client_msg_id: str
    ) -> SendResult:
        raise NotImplementedError("sending arrives in M4, and only through the Outbox")

    async def ping(self) -> None:
        if self._ws is not None:
            await self._ws.send(sign_frame(self._secret, "ping", {}))

    async def request_sidecar_shutdown(self) -> None:
        if self._ws is not None:
            await self._ws.send(sign_frame(self._secret, "shutdown", {}))

    async def health(self) -> AdapterHealth:
        return AdapterHealth(sidecar_connected=self._ws is not None, wa_state=self._wa_state)

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            await self._ws.close()
