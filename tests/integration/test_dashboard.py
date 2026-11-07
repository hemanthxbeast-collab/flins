"""Dashboard pages and write protection, on a database filled by the working-mood replay."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flins.core.policy_schema import Policy
from flins.replay import load_script, run_script
from flins.web.app import create_app
from tests.conftest import REPO_ROOT

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="module")
def filled_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    import yaml

    data = yaml.safe_load(
        (REPO_ROOT / "config" / "policy.example.yaml").read_text(encoding="utf-8")
    )
    db = tmp_path_factory.mktemp("dash") / "flins.db"
    asyncio.run(
        run_script(
            load_script(REPO_ROOT / "tests" / "replay" / "working_mood.jsonl"),
            db,
            Policy.model_validate(data),
        )
    )
    return db


@pytest.fixture
def client(filled_db: Path, example_policy_dict: dict[str, Any]) -> TestClient:
    app = create_app(filled_db, Policy.model_validate(example_policy_dict), port=PORT, token="T")
    return TestClient(app, base_url=BASE)


def test_inbox_shows_decisions_drafts_and_traces(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Rahul" in html and "Mom" in html
    assert "would reply" in html and "would send holding reply" in html
    assert "URGENT" in html
    assert "dry run: nothing is sent" in html
    assert "Approve &amp; send" in html  # held drafts can be approved
    assert "Content-Security-Policy" in r.headers


def test_message_text_is_escaped(client: TestClient, filled_db: Path) -> None:
    with sqlite3.connect(filled_db) as c:
        c.execute("UPDATE messages SET text = '<script>alert(1)</script>' WHERE wa_msg_id = 'r1'")
    html = client.get("/").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_decision_detail(client: TestClient) -> None:
    html = client.get("/").text
    first_id = html.split('href="/decision/')[1].split('"')[0]
    r = client.get(f"/decision/{first_id}")
    assert r.status_code == 200 and "Why this level" in r.text
    assert client.get("/decision/999999").status_code == 404


def test_unsaved_names_are_marked(client: TestClient) -> None:
    """The OTP scammer calls themselves "Hemanth". They must not look like a saved contact."""
    assert "~Hemanth" in client.get("/").text
    page = client.get("/contacts").text
    assert "~Hemanth" in page and "not in your contacts" in page


def test_contacts_page(client: TestClient) -> None:
    r = client.get("/contacts?q=Rah")
    assert r.status_code == 200 and "Rahul" in r.text and "Mom" not in r.text


def test_wrong_host_is_rejected(filled_db: Path, example_policy_dict: dict[str, Any]) -> None:
    app = create_app(filled_db, Policy.model_validate(example_policy_dict), port=PORT, token="T")
    evil = TestClient(app, base_url="http://attacker.example")
    assert evil.get("/").status_code == 400


def test_writes_need_the_token(client: TestClient, filled_db: Path) -> None:
    with sqlite3.connect(filled_db) as c:
        cid = c.execute("SELECT id FROM contacts WHERE display_name = 'Mom'").fetchone()[0]
    assert (
        client.post(f"/contacts/{cid}/tier", data={"tier": "trusted", "token": "nope"}).status_code
        == 403
    )
    assert client.post("/mode", data={"text": "stop", "token": "nope"}).status_code == 403
    r = client.post(
        f"/contacts/{cid}/tier", data={"tier": "trusted", "token": "T"}, follow_redirects=False
    )
    assert r.status_code == 303
    with sqlite3.connect(filled_db) as c:
        assert (
            c.execute("SELECT tier FROM contacts WHERE id = ?", (cid,)).fetchone()[0] == "trusted"
        )
        audit = c.execute(
            "SELECT COUNT(*) FROM audit_log WHERE kind='contact.tier_changed'"
        ).fetchone()
        assert audit[0] == 1
    bad = client.post(f"/contacts/{cid}/tier", data={"tier": "god", "token": "T"})
    assert bad.status_code == 400


def test_stop_button(client: TestClient, filled_db: Path) -> None:
    r = client.post("/mode", data={"text": "working mood for 1h", "token": "T"})
    assert r.status_code == 200 and "Working mode on" in r.text
    r = client.post("/mode", data={"text": "stop", "token": "T"})
    assert "Paused" in r.text
    with sqlite3.connect(filled_db) as c:
        assert c.execute("SELECT mode FROM mode_state").fetchone()[0] == "PAUSED"
    client.post("/mode", data={"text": "resume", "token": "T"})
