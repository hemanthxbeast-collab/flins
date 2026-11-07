"""Autonomy levels and modes (SPEC §3, §6.1)."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Level(IntEnum):
    """How much FLINS may do with an incoming message. Higher = more autonomy."""

    IGNORE = 0
    NOTIFY = 1
    DRAFT = 2
    AUTO_SAFE = 3
    AUTO = 4

    @classmethod
    def parse(cls, value: str | int | Level) -> Level:
        if isinstance(value, Level):
            return value
        if isinstance(value, int):
            return cls(value)
        try:
            return cls[value.strip().upper()]
        except KeyError:
            names = ", ".join(m.name for m in cls)
            raise ValueError(f"unknown level {value!r}; expected one of {names}") from None


class Mode(StrEnum):
    NORMAL = "NORMAL"
    WORKING = "WORKING"
    FOCUS = "FOCUS"
    AWAY = "AWAY"
    PAUSED = "PAUSED"


def min_level(*ceilings: Level) -> Level:
    """The final level is the lowest of all ceilings. With no ceilings, be safe: IGNORE."""
    return min(ceilings, default=Level.IGNORE)
