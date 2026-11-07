"""Wire protocol v1 between flins-core and the WhatsApp sidecar.

Mirror of wa-sidecar/src/protocol.ts.

Frame: {"body": "<json string>", "sig": "<hex HMAC-SHA256(secret, body)>"}
Body:  {"v": 1, "kind": str, "id": str, "ts": epoch_ms, "payload": {...}}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections import deque
from typing import Any

from pydantic import BaseModel, ValidationError

PROTOCOL_VERSION = 1
MAX_SKEW_MS = 120_000
SEEN_IDS = 2000
MIN_SECRET_LENGTH = 32


class ProtocolError(Exception):
    """A frame failed verification. Never act on it."""


class Body(BaseModel):
    v: int
    kind: str
    id: str
    ts: int
    payload: dict[str, Any]


def now_ms() -> int:
    return int(time.time() * 1000)


def hmac_hex(secret: str, body: str) -> str:
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_frame(
    secret: str,
    kind: str,
    payload: dict[str, Any],
    *,
    ts: int | None = None,
    frame_id: str | None = None,
) -> str:
    body = json.dumps(
        {
            "v": PROTOCOL_VERSION,
            "kind": kind,
            "id": frame_id or str(uuid.uuid4()),
            "ts": now_ms() if ts is None else ts,
            "payload": payload,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return json.dumps({"body": body, "sig": hmac_hex(secret, body)}, ensure_ascii=False)


class FrameVerifier:
    """Checks signature, version, clock skew and replays. One instance per connection."""

    def __init__(self, secret: str) -> None:
        self._secret = secret
        self._seen: deque[str] = deque()
        self._seen_set: set[str] = set()

    def verify(self, raw: str | bytes, now: int | None = None) -> Body:
        now = now_ms() if now is None else now
        try:
            frame = json.loads(raw)
        except ValueError:
            raise ProtocolError("frame is not JSON") from None
        if (
            not isinstance(frame, dict)
            or not isinstance(frame.get("body"), str)
            or not isinstance(frame.get("sig"), str)
        ):
            raise ProtocolError("frame must be {body: string, sig: string}")
        expected = hmac_hex(self._secret, frame["body"])
        if not hmac.compare_digest(expected, frame["sig"]):
            raise ProtocolError("bad signature")
        try:
            body = Body.model_validate_json(frame["body"])
        except ValidationError:
            raise ProtocolError("malformed body") from None
        if body.v != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol version {body.v}")
        if abs(now - body.ts) > MAX_SKEW_MS:
            raise ProtocolError("stale or future frame")
        if body.id in self._seen_set:
            raise ProtocolError("replayed frame id")
        self._seen.append(body.id)
        self._seen_set.add(body.id)
        if len(self._seen) > SEEN_IDS:
            self._seen_set.discard(self._seen.popleft())
        return body
