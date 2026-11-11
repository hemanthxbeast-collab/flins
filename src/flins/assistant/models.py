"""Shapes shared by the assistant core and its channels."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# Channels the owner talks to FLINS on. Contacts' messages are not a Source: they go through
# core/pipeline.py and never reach the assistant core.
Source = Literal["voice", "self_chat", "cli", "dashboard"]
SOURCES: tuple[str, ...] = ("voice", "self_chat", "cli", "dashboard")

# How a turn was handled.
#   command  a built-in command ran (or asked you to confirm)
#   chat     the conversation model answered
#   error    FLINS couldn't answer and said so
#   silent   not for FLINS (an un-addressed note in your self-chat): no reply, nothing stored
Route = Literal["command", "chat", "error", "silent"]


@dataclass(frozen=True)
class Request:
    text: str
    source: str
    at: datetime


@dataclass(frozen=True)
class Reply:
    text: str | None  # None only for route "silent"
    route: Route
    stages: list[str] = field(default_factory=list)  # what the core did, in order
    model_role: str | None = None  # "chat" | "reason" when a model answered
    flagged: str | None = None  # why the model's reply was replaced, if it was


@dataclass(frozen=True)
class Turn:
    id: int
    ts: datetime
    source: str
    role: Literal["user", "assistant"]
    route: str | None
    text: str


@dataclass(frozen=True)
class MemoryItem:
    """One thing FLINS knows, with where it came from (never invented)."""

    content: str
    source: str  # e.g. "about_me.yaml"
    kind: Literal["semantic", "episodic", "task", "relationship"] = "semantic"
