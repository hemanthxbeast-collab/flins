from __future__ import annotations

import json

import pytest

from flins.adapters.protocol import (
    MAX_SKEW_MS,
    FrameVerifier,
    ProtocolError,
    hmac_hex,
    sign_frame,
)
from tests.conftest import REPO_ROOT

SECRET = "x" * 40
NOW = 1_762_500_000_000


def test_shared_vectors_match_node() -> None:
    vectors = json.loads(
        (REPO_ROOT / "tests" / "fixtures" / "protocol_vectors.json").read_text(encoding="utf-8")
    )
    for case in vectors["cases"]:
        assert hmac_hex(vectors["secret"], case["body"]) == case["sig"]


def test_round_trip_with_unicode() -> None:
    raw = sign_frame(SECRET, "message.received", {"text": "héllo 👋"}, ts=NOW)
    body = FrameVerifier(SECRET).verify(raw, now=NOW)
    assert body.kind == "message.received"
    assert body.payload == {"text": "héllo 👋"}


def test_rejects_tampered_body() -> None:
    frame = json.loads(sign_frame(SECRET, "ping", {}, ts=NOW))
    frame["body"] = frame["body"].replace("ping", "pong")
    with pytest.raises(ProtocolError, match="bad signature"):
        FrameVerifier(SECRET).verify(json.dumps(frame), now=NOW)


def test_rejects_wrong_secret() -> None:
    raw = sign_frame(SECRET, "ping", {}, ts=NOW)
    with pytest.raises(ProtocolError, match="bad signature"):
        FrameVerifier("y" * 40).verify(raw, now=NOW)


@pytest.mark.parametrize("skew", [MAX_SKEW_MS + 1, -(MAX_SKEW_MS + 1)])
def test_rejects_stale_or_future(skew: int) -> None:
    raw = sign_frame(SECRET, "ping", {}, ts=NOW)
    with pytest.raises(ProtocolError, match="stale"):
        FrameVerifier(SECRET).verify(raw, now=NOW + skew)


def test_rejects_replay() -> None:
    raw = sign_frame(SECRET, "ping", {}, ts=NOW)
    v = FrameVerifier(SECRET)
    v.verify(raw, now=NOW)
    with pytest.raises(ProtocolError, match="replayed"):
        v.verify(raw, now=NOW)


def test_rejects_other_version_and_garbage() -> None:
    body = json.dumps({"v": 2, "kind": "ping", "id": "a", "ts": NOW, "payload": {}})
    raw = json.dumps({"body": body, "sig": hmac_hex(SECRET, body)})
    with pytest.raises(ProtocolError, match="version"):
        FrameVerifier(SECRET).verify(raw, now=NOW)
    for junk in ["nope", "[]", '{"body": 1, "sig": "x"}']:
        with pytest.raises(ProtocolError):
            FrameVerifier(SECRET).verify(junk, now=NOW)
