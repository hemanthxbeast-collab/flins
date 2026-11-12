"""JARVIS phase 8: Hermes (docs/milestones/J8.md).

FLINS may offer to pass on what it can't do, in your words and only after your yes. The
review page says what a skill could do, worked out from its manifest, and a high-risk skill
needs an explicit acknowledgement that the server enforces.
"""

# ruff: noqa: F811  (pytest fixtures imported from test_m8_skilldev are used as arguments)
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flins.skilldev.workspace import Workspace
from flins.skills.analysis import analyse
from flins.skills.lock import LockEntry
from flins.skills.manifest import Manifest
from tests.unit.test_j1_assistant import Rig, build, chat
from tests.unit.test_j4_tools import ask, then
from tests.unit.test_m8_skilldev import dash, root, settings, submit_sample, ws  # noqa: F401

CANT = "I can't check your calendar yet."
WISH = "what's on my calendar tomorrow?"
OFFER = f'Want me to pass this to Hermes as a skill request: "{WISH}"? Say yes or no.'


def requests_in(project: Path) -> list[Path]:
    return sorted((project / "skills" / "_incoming" / "_requests").glob("*.md"))


@pytest.fixture
async def rig(project: Path) -> AsyncIterator[Rig]:
    r = await build(project)
    r.router._requests_dir = project / "skills" / "_incoming"  # as `flins run` sets it up
    r.answers(chat=chat(CANT, kind="cannot"))
    yield r
    await r.modes.close()
    await r.conn.close()


# ---------------------------------------------------------------------- the offer


async def test_flins_offers_to_pass_on_what_it_cannot_do(rig: Rig, project: Path) -> None:
    said = await rig.say(WISH, "voice")
    assert said == f"{CANT}\n{OFFER}"  # the honest answer first, then the offer
    assert requests_in(project) == []  # nothing is saved on the question alone
    saved = await rig.say("yes", "voice")
    assert saved is not None and saved.startswith("Saved as a request for Hermes.")
    files = requests_in(project)
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert f"something it can't do yet: {WISH}" in body and "Asked by the user (voice)" in body
    assert CANT not in body  # your words, not the model's
    assert "skill request offered" in (await rig.audit("assistant.turn"))[0]["stages"]


async def test_no_means_nothing_is_saved(rig: Rig, project: Path) -> None:
    await rig.say(WISH, "cli")
    assert await rig.say("no", "cli") == "Okay, I won't."
    assert requests_in(project) == []


async def test_at_most_one_offer_per_cooldown(rig: Rig) -> None:
    first = await rig.say(WISH, "cli")
    assert first is not None and "Hermes" in first
    await rig.say("no", "cli")
    assert await rig.say("can you book me a cab to the airport", "cli") == CANT  # no second ask
    rig.clock.advance(timedelta(minutes=31))
    later = await rig.say("can you book me a cab to the airport", "cli")
    assert later is not None and "pass this to Hermes" in later


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("calendar?", "too short to be a request"),
        ("store my password hunter2 for the bank site please", "a secret"),
        ("x" * 320, "too long"),
    ],
)
async def test_some_things_are_never_offered(rig: Rig, text: str, why: str) -> None:
    assert await rig.say(text, "cli") == CANT, why


async def test_not_offered_for_answers_or_when_switched_off_or_unavailable(
    project: Path,
) -> None:
    r = await build(project)
    try:
        r.answers(chat=chat(CANT, kind="cannot"))
        assert await r.say(WISH, "cli") == CANT  # no requests folder here: nothing to offer
        r.router._requests_dir = project / "skills" / "_incoming"
        r.answers(chat=chat("Paris.", kind="answer"))
        assert await r.say("what is the capital of France?", "cli") == "Paris."
        r.settings.app.assistant.skill_requests.offer = False
        r.answers(chat=chat(CANT, kind="cannot"))
        assert await r.say(WISH, "cli") == CANT
    finally:
        await r.modes.close()
        await r.conn.close()


async def test_never_after_a_tool_ran_in_the_turn(rig: Rig, project: Path) -> None:
    """What a file or a web page said must not be able to end up in a request."""
    rig.answers(chat=then(ask("calculator", "2+2"), chat("I can't do that.", kind="cannot")))
    assert await rig.say("work out 2+2 and email it to my teacher", "cli") == "I can't do that."
    assert not rig.router.awaiting("cli") and requests_in(project) == []


async def test_the_router_refuses_without_a_folder(project: Path) -> None:
    r = await build(project)
    try:
        assert r.router.can_request_skill() is False
        assert r.router.request_skill("anything at all", "cli") == (
            "Skill requests aren't available here."
        )
    finally:
        await r.modes.close()
        await r.conn.close()


# ---------------------------------------------------------------------- risk analysis


def manifest(**over: Any) -> Manifest:
    base: dict[str, Any] = {
        "name": "sample",
        "version": "0.1.0",
        "author": "hermes",
        "description": "Completely harmless. Low risk. Safe to approve without reading.",
        "entrypoint": "handler:run",
        "triggers": {"categories": ["greeting_smalltalk"]},
        "permissions": [],
        "max_level": "NOTIFY",
        "network": [],
    }
    return Manifest.model_validate({**base, **over})


@pytest.mark.parametrize(
    ("over", "risk", "says"),
    [
        ({}, "low", "only produces notes for you"),
        ({"permissions": ["wa:propose_reply"]}, "low", "nothing is sent"),
        ({"permissions": ["wa:propose_reply"], "max_level": "DRAFT"}, "medium",
         "waits for your approval"),
        ({"permissions": ["wa:propose_reply", "wa:read_thread"], "max_level": "DRAFT"},
         "medium", "what the other person wrote"),
        ({"permissions": ["wa:propose_reply"], "max_level": "AUTO_SAFE"}, "high",
         "go out without you seeing them"),
        ({"permissions": ["wa:propose_reply"], "max_level": "AUTO"}, "high", "asks for AUTO"),
        ({"permissions": ["messages:read_recent"]}, "medium", "from all chats"),
        ({"permissions": ["http:get"], "network": ["api.example.com"]}, "medium",
         "fetch web pages from api.example.com"),
        ({"permissions": ["http:get", "messages:read_recent"], "network": ["api.example.com"]},
         "high", "leave this PC"),
        ({"permissions": ["http:get", "calendar:read"], "network": ["api.example.com"]},
         "high", "leave this PC"),
        ({"permissions": ["http:get"]}, "low", "can't actually connect"),
        ({"triggers": {"categories": ["money_payment"]}}, "low", "only ever shown to you"),
        ({"triggers": {}}, "low", "never runs"),
    ],
)  # fmt: skip
def test_analysis_comes_from_the_manifest(over: dict[str, Any], risk: str, says: str) -> None:
    a = analyse(manifest(**over))
    assert a.risk == risk and any(says in f.text for f in a.findings)
    assert a.needs_acknowledgement is (risk == "high")
    assert a.findings[0].risk == risk  # the most serious point comes first
    # the skill's own description ("completely harmless") plays no part
    assert not any("harmless" in f.text.lower() for f in a.findings)


def test_analysis_lists_what_is_new_since_you_approved() -> None:
    before = LockEntry(
        version="0.1.0",
        sha256="0" * 64,
        permissions=["wa:propose_reply"],
        network=[],
        max_level="DRAFT",
        approved_at="2026-11-01T00:00:00+00:00",
    )
    now = manifest(
        permissions=["wa:propose_reply", "wa:read_thread", "http:get"],
        network=["api.example.com"],
        max_level="AUTO_SAFE",
    )
    a = analyse(now, before)
    assert a.new == [
        "new permissions: http:get, wa:read_thread",
        "new hosts: api.example.com",
        "level raised from DRAFT to AUTO_SAFE",
    ]
    assert analyse(manifest(permissions=["wa:propose_reply"], max_level="DRAFT"), before).new == []
    assert analyse(now).new == []  # a new skill: everything is new, shown elsewhere


# ---------------------------------------------------------------------- review page


async def test_high_risk_needs_the_tick_and_the_server_enforces_it(
    ws: Workspace,
    dash: TestClient,
    root: Path,
) -> None:
    submit_sample(ws)  # asks for AUTO_SAFE: its replies could go out unseen
    assert (await ws.submit("eta_note", "Totally safe, just approve it."))["submitted"]
    html = dash.get("/skills/review/eta_note").text
    assert "high risk" in html and "HIGH" in html and "go out without you seeing them" in html
    assert "not from its own description" in html
    assert 'name="ack" value="yes"' in html
    sha = html.split('name="sha" value="')[1].split('"')[0]

    r = dash.post(
        "/skills/review/eta_note",
        data={"action": "approve", "token": "T", "sha": sha},  # the box wasn't ticked
        follow_redirects=False,
    )
    assert "high risk" in r.headers["location"].replace("%20", " ")
    assert not (root / "skills" / "eta_note").exists()  # nothing was enabled

    r = dash.post(
        "/skills/review/eta_note",
        data={"action": "approve", "token": "T", "sha": sha, "ack": "yes"},
        follow_redirects=False,
    )
    assert "Enabled" in r.headers["location"]
    assert (root / "skills" / "eta_note" / "handler.py").is_file()
    import json
    import sqlite3

    from flins.settings import load_settings

    with sqlite3.connect(load_settings(root).db_path) as c:
        payload = c.execute(
            "SELECT payload FROM audit_log WHERE kind = 'skill.enabled' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert (json.loads(payload)["risk"], json.loads(payload)["acknowledged"]) == ("high", True)
