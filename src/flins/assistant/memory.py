"""What FLINS knows about you. Phase 1 is the interface plus the facts you already wrote in
about_me.yaml; the real store (remember / forget / why do you know that) comes in phase 2.

Every item carries its source, so FLINS can say where it learned something and never has to
invent one.
"""

from __future__ import annotations

import re
from typing import Protocol

from flins.assistant.models import MemoryItem
from flins.settings import AboutMe

_WORD = re.compile(r"[a-z0-9']{3,}")


class MemoryStore(Protocol):
    async def relevant(self, query: str, limit: int) -> list[MemoryItem]:
        """Things worth putting in front of the model for this message, best first."""
        ...


class NoMemory:
    async def relevant(self, query: str, limit: int) -> list[MemoryItem]:
        return []


class ProfileMemory:
    """Read-only: the facts and ready answers in about_me.yaml. You are the one asking, so
    every fact is available whatever its `share_with` (that setting is about other people)."""

    SOURCE = "about_me.yaml"

    def __init__(self, about: AboutMe) -> None:
        self._items = [f.fact for f in about.facts] + [
            f"If asked {a.question!r}: {a.answer}" for a in about.answers
        ]

    async def relevant(self, query: str, limit: int) -> list[MemoryItem]:
        """All of them if they fit; otherwise the ones sharing most words with the message."""
        items = self._items
        if len(items) > limit:
            asked = set(_WORD.findall(query.lower()))
            scored = sorted(
                enumerate(items),
                key=lambda p: (-len(asked & set(_WORD.findall(p[1].lower()))), p[0]),
            )
            items = [text for _, text in scored[:limit]]
        return [MemoryItem(text, self.SOURCE) for text in items]
