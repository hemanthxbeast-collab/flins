"""In-process async event bus.

Handlers subscribe to a topic (e.g. "mode.changed") or to "*" for everything.
A handler that raises is logged and skipped. It never stops other handlers.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

WILDCARD = "*"


@dataclass(frozen=True)
class Event:
    topic: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: defaultdict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> Callable[[], None]:
        """Register a handler. Returns a function that unsubscribes it."""
        self._handlers[topic].append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers[topic]:
                self._handlers[topic].remove(handler)

        return unsubscribe

    async def publish(self, topic: str, data: dict[str, Any] | None = None) -> Event:
        event = Event(topic=topic, data=data or {})
        handlers = [*self._handlers.get(topic, []), *self._handlers.get(WILDCARD, [])]
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                # Log the topic only, never event data (it may contain message text).
                log.exception("event handler %r failed for topic %s", handler, topic)
        return event
