"""In-memory adapter for tests and replays. Records sends instead of sending."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from flins.adapters.base import AdapterEvent, AdapterHealth, SendResult, WAState


class FakeAdapter:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[AdapterEvent | None] = asyncio.Queue()
        self.sent: list[dict[str, str | None]] = []
        self.wa_state: WAState = "open"

    async def push(self, *events: AdapterEvent) -> None:
        for e in events:
            await self._queue.put(e)

    async def events(self) -> AsyncIterator[AdapterEvent]:
        while (event := await self._queue.get()) is not None:
            yield event

    async def send_text(
        self, chat_id: str, text: str, *, quote: str | None, client_msg_id: str
    ) -> SendResult:
        self.sent.append(
            {"chat_id": chat_id, "text": text, "quote": quote, "client_msg_id": client_msg_id}
        )
        return SendResult(ok=True, wa_msg_id=f"fake-{len(self.sent)}")

    async def health(self) -> AdapterHealth:
        return AdapterHealth(sidecar_connected=True, wa_state=self.wa_state)

    async def close(self) -> None:
        await self._queue.put(None)
