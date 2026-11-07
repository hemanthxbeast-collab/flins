"""Group a quick series of messages from one chat into a single burst (SPEC §6 step 3).

People often send "hey" / "are you free" / "for a call?" as three messages. FLINS waits for
`quiet` seconds without a new message (but at most `max_wait` after the first one) and then
handles them together, so it answers once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class _Pending:
    first: datetime
    last: datetime
    ids: list[int] = field(default_factory=list)


class Debouncer:
    def __init__(self, quiet: timedelta, max_wait: timedelta) -> None:
        self._quiet = quiet
        self._max = max_wait
        self._chats: dict[str, _Pending] = {}

    def add(self, chat: str, message_id: int, now: datetime) -> None:
        p = self._chats.get(chat)
        if p is None:
            self._chats[chat] = _Pending(first=now, last=now, ids=[message_id])
        else:
            p.last = now
            p.ids.append(message_id)

    def flush(self, chat: str) -> list[int] | None:
        p = self._chats.pop(chat, None)
        return p.ids if p else None

    def due(self, now: datetime) -> list[list[int]]:
        ready = [
            chat
            for chat, p in self._chats.items()
            if now - p.last >= self._quiet or now - p.first >= self._max
        ]
        return [self._chats.pop(chat).ids for chat in ready]

    @property
    def pending(self) -> int:
        return sum(len(p.ids) for p in self._chats.values())
