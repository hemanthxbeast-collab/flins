"""Messaging adapter interface and the events adapters produce (SPEC §5.2)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

MsgType = Literal[
    "text",
    "image",
    "video",
    "audio",
    "doc",
    "sticker",
    "reaction",
    "location",
    "contact",
    "poll",
    "other",
]
WAState = Literal[
    "connecting",
    "open",
    "close",
    "not_paired",
    "logged_out",
    "conflict",
    "sidecar_down",
]


class _Event(BaseModel):
    # Newer sidecars may add fields; ignore what we don't know yet.
    model_config = ConfigDict(extra="ignore")


class InboundMessage(_Event):
    kind: Literal["message.received"] = "message.received"
    wa_msg_id: str
    chat_jid: str
    chat_jid_alt: str | None = None
    sender_jid: str
    sender_jid_alt: str | None = None
    from_me: bool = False
    is_group: bool = False
    push_name: str | None = None
    ts_ms: int
    type: MsgType = "other"
    text: str | None = None
    quoted_id: str | None = None
    mentions: list[str] = Field(default_factory=list)
    upsert_type: Literal["notify", "append"] = "notify"


class ContactInfo(_Event):
    pn: str | None = None
    lid: str | None = None
    name: str | None = None
    notify: str | None = None


class ContactsUpsert(_Event):
    kind: Literal["contacts.upsert"] = "contacts.upsert"
    contacts: list[ContactInfo]


class MeInfo(_Event):
    id: str | None = None
    lid: str | None = None
    name: str | None = None


class ConnectionChanged(_Event):
    kind: Literal["connection"] = "connection"
    state: WAState
    reason_code: int | None = None
    me: MeInfo | None = None
    moved_auth_to: str | None = None
    retry_in_ms: int | None = None


class SidecarHello(_Event):
    kind: Literal["hello.ok"] = "hello.ok"
    sidecar_version: str
    baileys_version: str | None = None
    state: WAState
    me: MeInfo | None = None


AdapterEvent = InboundMessage | ContactsUpsert | ConnectionChanged | SidecarHello


class SendResult(BaseModel):
    ok: bool
    wa_msg_id: str | None = None
    error: str | None = None


class AdapterHealth(BaseModel):
    sidecar_connected: bool
    wa_state: WAState
    detail: dict[str, Any] = Field(default_factory=dict)


class MessagingAdapter(Protocol):
    def events(self) -> AsyncIterator[AdapterEvent]: ...

    async def send_text(
        self, chat_id: str, text: str, *, quote: str | None, client_msg_id: str
    ) -> SendResult: ...

    async def health(self) -> AdapterHealth: ...

    async def close(self) -> None: ...


def parse_event(kind: str, payload: dict[str, Any]) -> AdapterEvent | None:
    """Turn a verified frame into an event. Unknown kinds return None."""
    if kind == "message.received":
        return InboundMessage.model_validate(payload)
    if kind == "contacts.upsert":
        return ContactsUpsert.model_validate(payload)
    if kind == "connection":
        return ConnectionChanged.model_validate(payload)
    if kind == "hello.ok":
        return SidecarHello.model_validate(payload)
    return None
