"""JARVIS phase 9: dashboard polish (docs/milestones/J9.md).

Home (what FLINS is doing now), Activity (the audit log) and Automations (everything that
happens without you asking). Read-only, except releasing a hold, which needs the page token.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flins.assistant.tasks import TaskStore
from flins.core.audit import AuditLog
from flins.core.clock import FrozenClock
from flins.core.db import connect, migrate
from flins.core.holds import HoldStore
from flins.settings import Settings, load_settings
from flins.web.app import create_app
from tests.unit.test_j1_assistant import T0, Rig, build
from tests.unit.test_j6_chats import MOM, RAHUL, contact
from tests.unit.test_j7_proactive import TZ

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def settings(project: Path) -> Settings:
    s = load_settings(project)

    async def seed() -> None:
        conn = await connect(s.db_path)
        await migrate(conn)
        await contact(conn, RAHUL, "Rahul")
        await contact(conn, MOM, "Mom")
        tasks = TaskStore(conn, TZ)
        at_seven = T0 + timedelta(hours=5)  # 19:00 local
        await tasks.add("call the bank", source="told", channel="cli", now=T0, at=at_seven)
        await tasks.add(
            "stand-up notes", source="told", channel="cli", now=T0,
            at=T0 + timedelta(hours=19), repeat="weekdays",
        )  # fmt: skip
        await tasks.add(
            "lab report", source="told", channel="cli", now=T0 - timedelta(days=2),
            at=T0 - timedelta(hours=3),
        )  # fmt: skip
        await HoldStore(conn).set(RAHUL, T0 + timedelta(hours=2), source="voice", now=T0)
        await conn.execute(
            "UPDATE contacts SET allowed_categories = ? WHERE wa_jid = ?",
            (json.dumps(["greeting_smalltalk"]), MOM),
        )
        await conn.commit()
        audit = AuditLog(conn)
        await audit.record(
            "assistant", "assistant.turn", {"source": "voice", "route": "chat", "ms": 812}
        )
        await audit.record("assistant", "assistant.tool", {"tool": "calculator", "ok": True})
        await audit.record("user", "hold.set", {"contact_id": 1, "via": "voice"})
        await conn.close()

    s.data_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(seed())
    return s


@pytest.fixture
def dash(settings: Settings) -> Iterator[TestClient]:
    app = create_app(
        settings.db_path, settings.policy, clock=FrozenClock(T0), port=8765, token="T",
        settings=settings,
    )  # fmt: skip
    with TestClient(app, base_url=BASE) as client:
        yield client


def test_home_shows_what_flins_is_doing(dash: TestClient) -> None:
    r = dash.get("/home")
    assert r.status_code == 200
    html = r.text
    assert "FLINS right now" in html and "Normal mode" in html
    assert "2 tasks today" in html and "1 overdue" in html and "1 on hold" in html
    assert "call the bank (today 19:00)" in html and "lab report" in html
    assert "stand-up notes" not in html  # tomorrow's
    assert "Rahul until 16:00" in html
    for feature in ("Sending", "Briefings and heads-ups", "Online tools", "Asking about chats"):
        assert feature in html
    assert "dry run: nothing is sent" in html and "calculator" in html
    assert "hold.set" in html and "<script" not in html.lower()


def test_every_page_has_the_new_menu(dash: TestClient) -> None:
    for path in ("/", "/home", "/tasks", "/activity", "/automations"):
        html = dash.get(path).text
        assert 'href="/home"' in html and 'href="/automations"' in html
        assert 'href="/activity"' in html
    assert dash.get("/").status_code == 200  # the inbox stays where it was


def test_activity_lists_the_audit_log_and_filters(dash: TestClient) -> None:
    html = dash.get("/activity").text
    assert "assistant.turn" in html and "assistant.tool" in html and "hold.set" in html
    assert "tool: calculator" in html and "ms: 812" in html
    only = dash.get("/activity?kind=assistant.tool").text
    assert "tool: calculator" in only and "ms: 812" not in only
    prefix = dash.get("/activity?kind=assistant").text
    assert "tool: calculator" in prefix and "ms: 812" in prefix and "via: voice" not in prefix
    # a filter that isn't a plain kind name is ignored, never put into the query or the page
    odd = dash.get("/activity", params={"kind": "x' OR 1=1 --<b>"})
    assert odd.status_code == 200 and "OR 1=1" not in odd.text and "ms: 812" in odd.text
    assert "Nothing recorded for this kind" in dash.get("/activity?kind=zzz").text


def test_automations_shows_everything_that_runs_by_itself(dash: TestClient) -> None:
    html = dash.get("/automations").text
    assert "Rahul" in html and "until Tue 16:00" in html
    assert "stand-up notes" in html and "weekdays" in html and "call the bank" not in html
    assert "Morning briefing" in html and "08:00" in html and ">off<" in html
    assert "sleep" in html and "&#34;sleep mode&#34;" in html and "8 h" in html
    assert "Working" in html and "AUTO_SAFE" in html
    assert "Mom" in html and "greeting_smalltalk" in html
    assert "asleep and will see this in the morning" in html  # the reply people would get


def test_releasing_a_hold_needs_the_token(dash: TestClient, settings: Settings) -> None:
    def held() -> int:
        with sqlite3.connect(settings.db_path) as c:
            return int(c.execute("SELECT COUNT(*) FROM contact_holds").fetchone()[0])

    assert dash.post("/holds/release", data={"jid": RAHUL}).status_code == 422
    assert dash.post("/holds/release", data={"jid": RAHUL, "token": "wrong"}).status_code == 403
    assert held() == 1
    r = dash.post("/holds/release", data={"jid": RAHUL, "token": "T"}, follow_redirects=False)
    assert r.status_code == 303 and "Released%20Rahul" in r.headers["location"]
    assert held() == 0
    with sqlite3.connect(settings.db_path) as c:
        kind, payload = c.execute(
            "SELECT kind, payload FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert kind == "hold.released" and json.loads(payload)["via"] == "dashboard"
    assert "Rahul" not in payload  # an id, not a name
    again = dash.post("/holds/release", data={"jid": RAHUL, "token": "T"}, follow_redirects=False)
    assert "already%20ended" in again.headers["location"]
    assert "Nobody." in dash.get("/automations").text


def test_pages_refuse_other_hosts_and_carry_the_security_headers(settings: Settings) -> None:
    app = create_app(settings.db_path, settings.policy, port=8765, token="T", settings=settings)
    with TestClient(app, base_url="http://evil.example") as other:
        for path in ("/home", "/activity", "/automations"):
            assert other.get(path).status_code == 400
    with TestClient(app, base_url=BASE) as client:
        headers = client.get("/home").headers
        assert headers["x-frame-options"] == "DENY"
        assert "default-src 'none'" in headers["content-security-policy"]


async def test_holds_set_and_released_by_voice_are_audited(project: Path) -> None:
    rig: Rig = await build(project)
    try:
        await contact(rig.conn, RAHUL, "Rahul")
        await rig.say("hold Rahul till 6pm", "voice")
        await rig.say("release Rahul", "voice")
        set_rows = await rig.audit("hold.set")
        released = await rig.audit("hold.released")
        assert len(set_rows) == 1 and len(released) == 1
        assert set_rows[0]["via"] == "voice" and isinstance(set_rows[0]["contact_id"], int)
        assert "Rahul" not in json.dumps(set_rows + released)
    finally:
        await rig.modes.close()
        await rig.conn.close()
