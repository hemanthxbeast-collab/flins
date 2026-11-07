"""M8: Hermes builds skills through the MCP server; only you can enable them.

Acceptance (SPEC §14): Hermes builds a sample skill end to end, can't write outside _incoming,
and can't enable it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters

from flins.cli import wsl_networking_warning
from flins.core.clock import FrozenClock
from flins.core.commands import CommandRouter
from flins.core.db import connect, migrate
from flins.core.events import EventBus
from flins.core.modes import ModeManager
from flins.settings import Settings, load_settings
from flins.skilldev.server import TOOLS, build_server
from flins.skilldev.workspace import Workspace, WorkspaceError
from flins.skills.lock import LOCK_FILE, SkillLock
from flins.web.app import create_app
from tests.conftest import REPO_ROOT

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"

MANIFEST = """name: eta_note
version: 0.1.0
author: hermes
description: "While away, tells people asking 'where are you' when you'll be back."
entrypoint: handler:run
triggers:
  categories: [logistics_eta]
  modes: [AWAY]
permissions:
  - wa:propose_reply
  - contacts:read_card
max_level: AUTO_SAFE
timeout_s: 5
network: []
"""
HANDLER = '''"""Away ETA note."""


def run(inp, ctx):
    if inp["mode"] != "AWAY":
        return {"proposals": []}
    name = (inp.get("contact") or {}).get("first_name") or "there"
    back = inp.get("until_local") or "later"
    text = f"Hi {name}, {inp['user_name']} is out and should be back around {back}."
    if inp.get("signature"):
        text = f"{text} {inp['signature']}"
    return {"proposals": [{"kind": "reply", "text": text}]}
'''
FIXTURE = {
    "input": {"messages": ["where are you?"], "mode": "AWAY", "until_local": "19:30"},
    "contact": {"first_name": "Anita", "tier": "inner"},
    "expect": {"reply_contains": ["Anita", "19:30"]},
}


@pytest.fixture
def root(project: Path) -> Path:
    """A throwaway FLINS project with the repo's starter skills, SPEC and eval cases."""
    shutil.copytree(
        REPO_ROOT / "skills",
        project / "skills",
        ignore=shutil.ignore_patterns("_incoming", "skills.lock", "__pycache__"),
    )
    (project / "docs").mkdir()
    shutil.copy(REPO_ROOT / "docs" / "SPEC.md", project / "docs" / "SPEC.md")
    (project / "tests" / "eval").mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "tests" / "eval" / "whatsapp_cases.jsonl",
        project / "tests" / "eval" / "whatsapp_cases.jsonl",
    )
    return project


@pytest.fixture
def ws(root: Path) -> Workspace:
    return Workspace.for_root(root)


@pytest.fixture
def settings(root: Path) -> Settings:
    return load_settings(root)


def text_of(result: Any) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


# ---------------------------------------------------------------------- confinement


@pytest.mark.parametrize(
    "path",
    [
        "../escape.py",
        "fixtures/../../escape.py",
        "/etc/passwd.py",
        "C:/Windows/evil.py",
        "C:evil.py",
        "fixtures\\..\\..\\evil.py",
        ".hidden.py",
        "fixtures/.x.json",
        "evil.exe",
        "evil.pth",
        "a/b/c/d.py",
        "",
        "con.py\0",
    ],
)
def test_writes_cannot_leave_the_skill_folder(ws: Workspace, root: Path, path: str) -> None:
    ws.scaffold("probe", "test")
    before = sorted(p.as_posix() for p in root.rglob("*"))
    with pytest.raises(WorkspaceError):
        ws.write_file("probe", path, "x = 1\n")
    assert sorted(p.as_posix() for p in root.rglob("*")) == before


@pytest.mark.parametrize("name", ["../skills", "Probe", "_requests", "_submissions", "a", "x/y"])
def test_bad_skill_names_are_refused(ws: Workspace, name: str) -> None:
    with pytest.raises(WorkspaceError):
        ws.scaffold(name, "test")


def test_links_inside_a_skill_are_refused(ws: Workspace, root: Path) -> None:
    ws.scaffold("linky", "test")
    outside = root / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, ws.skill_path("linky") / "fixtures2", target_is_directory=True)
    except OSError:
        pytest.skip("this Windows account can't create symlinks")
    with pytest.raises(WorkspaceError, match="link"):
        ws.write_file("linky", "fixtures2/x.json", "{}")
    assert list(outside.iterdir()) == []


def test_size_limits(ws: Workspace) -> None:
    ws.scaffold("big", "test")
    with pytest.raises(WorkspaceError, match="too big"):
        ws.write_file("big", "handler.py", "x" * 100_001)


def test_requests_and_reads_are_confined(ws: Workspace) -> None:
    with pytest.raises(WorkspaceError):
        ws.read_request("../../.env")
    with pytest.raises(WorkspaceError):
        ws.read_skill("holding_reply", where="data")
    with pytest.raises(WorkspaceError):
        ws.read_skill("../config")


# ---------------------------------------------------------------------- the MCP tools


async def test_tool_list_has_no_way_to_enable_or_reach_data(ws: Workspace) -> None:
    async with Client(build_server(ws)) as c:
        names = [t.name for t in (await c.list_tools()).tools]
    assert sorted(names) == sorted(TOOLS)
    for bad in ("enable", "approve", "policy", "contact", "send", "message", "data", "lock"):
        assert not any(bad in n for n in names), bad


async def test_hermes_builds_and_submits_a_skill_but_cannot_enable_it(
    ws: Workspace, root: Path, settings: Settings
) -> None:
    """The M8 acceptance test, with an MCP client playing Hermes."""
    async with Client(build_server(ws)) as hermes:
        spec = await hermes.call_tool("get_skill_spec", {})
        assert "Rules FLINS enforces" in text_of(spec) and "holding_reply" in text_of(spec)
        caps = await hermes.call_tool("list_capabilities", {})
        assert "wa:send" in json.dumps(caps.structured_content)  # listed as refused
        assert not (
            await hermes.call_tool("scaffold_skill", {"name": "eta_note", "description": "x"})
        ).is_error
        for path, content in [
            ("flins.skill.yaml", MANIFEST),
            ("handler.py", HANDLER),
            ("fixtures/away.json", json.dumps(FIXTURE)),
        ]:
            r = await hermes.call_tool(
                "write_skill_file", {"name": "eta_note", "path": path, "content": content}
            )
            assert not r.is_error, text_of(r)
        await hermes.call_tool(
            "delete_skill_file", {"name": "eta_note", "path": "fixtures/basic.json"}
        )

        # It can't write outside _incoming or call tools that don't exist.
        r = await hermes.call_tool(
            "write_skill_file",
            {"name": "eta_note", "path": "../../config/policy.yaml", "content": "x"},
        )
        assert r.is_error and "refused" in text_of(r)
        r = await hermes.call_tool("enable_skill", {"name": "eta_note"})
        assert r.is_error

        tested = await hermes.call_tool("test_skill", {"name": "eta_note"})
        report = tested.structured_content
        assert report is not None and report["ok"], report
        assert report["eval"]["cases"] == 162 and report["eval"]["unsafe"] == []
        submitted = await hermes.call_tool(
            "submit_skill", {"name": "eta_note", "summary": "Tells people when you're back."}
        )
        assert submitted.structured_content["submitted"] is True  # type: ignore[index]
        listed = (await hermes.call_tool("list_skills", {})).structured_content
        assert {"name": "eta_note", "submitted": True, "review": None} in listed["incoming"]  # type: ignore[index]

    # Nothing is enabled or installed by Hermes.
    assert not (root / "skills" / "eta_note").exists()
    assert not (root / "skills" / LOCK_FILE).exists()
    assert not (root / "config" / "policy.yaml").exists()


async def test_editing_withdraws_a_submission(ws: Workspace) -> None:
    ws.scaffold("eta_note", "x")
    ws.write_file("eta_note", "flins.skill.yaml", MANIFEST)
    ws.write_file("eta_note", "handler.py", HANDLER)
    ws.write_file("eta_note", "fixtures/basic.json", json.dumps(FIXTURE))
    assert (await ws.submit("eta_note", "s"))["submitted"]
    assert ws.list_skills()["incoming"][0]["submitted"]
    ws.write_file("eta_note", "SKILL.md", "---\nname: eta_note\ndescription: changed\n---\n")
    assert not ws.list_skills()["incoming"][0]["submitted"]


async def test_failing_skill_cannot_be_submitted(ws: Workspace) -> None:
    ws.scaffold("broken", "x")
    ws.write_file("broken", "handler.py", "import socket\n\ndef run(i, c):\n    return {}\n")
    out = await ws.submit("broken", "s")
    assert out["submitted"] is False and "import 'socket'" in json.dumps(out)


async def test_stdio_server_speaks_only_mcp(root: Path) -> None:
    """`flins skilldev` as Hermes would start it: stdout carries nothing but the protocol."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "flins", "--root", str(root), "skilldev"]
    )
    async with Client(params) as c:
        names = [t.name for t in (await c.list_tools()).tools]
        r = await c.call_tool("list_skills", {})
    assert "submit_skill" in names
    assert "holding_reply" in r.structured_content["installed"]  # type: ignore[index]


# ---------------------------------------------------------------------- your review


def submit_sample(ws: Workspace) -> None:
    ws.scaffold("eta_note", "x")
    ws.write_file("eta_note", "flins.skill.yaml", MANIFEST)
    ws.write_file("eta_note", "handler.py", HANDLER)
    ws.write_file("eta_note", "fixtures/basic.json", json.dumps(FIXTURE))


@pytest.fixture
def dash(settings: Settings) -> Iterator[TestClient]:
    import asyncio

    async def init() -> None:
        conn = await connect(settings.db_path)
        await migrate(conn)
        await conn.close()

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(init())
    app = create_app(settings.db_path, settings.policy, port=PORT, token="T", settings=settings)
    yield TestClient(app, base_url=BASE)


async def test_review_and_approve_in_the_dashboard(
    ws: Workspace, settings: Settings, dash: TestClient, root: Path
) -> None:
    submit_sample(ws)
    assert (await ws.submit("eta_note", "Tells people when you're back."))["submitted"]

    page = dash.get("/skills")
    assert "1 to review" in page.text and "eta_note" in page.text
    review = dash.get("/skills/review/eta_note")
    assert review.status_code == 200
    html = review.text
    assert "wa:propose_reply" in html and "Approve and enable" in html
    assert "new/handler.py" in html and "written by Hermes, not checked" in html
    sha = html.split('name="sha" value="')[1].split('"')[0]

    # Wrong token, or a hash you didn't look at: refused.
    r = dash.post("/skills/review/eta_note", data={"action": "approve", "token": "x", "sha": sha})
    assert r.status_code == 403
    r = dash.post(
        "/skills/review/eta_note",
        data={"action": "approve", "token": "T", "sha": "0" * 64},
        follow_redirects=False,
    )
    assert "changed after you opened it" in r.headers["location"].replace("%20", " ")
    assert not (root / "skills" / "eta_note").exists()

    r = dash.post(
        "/skills/review/eta_note",
        data={"action": "approve", "token": "T", "sha": sha},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "Enabled" in r.headers["location"]
    assert (root / "skills" / "eta_note" / "handler.py").is_file()
    assert not (root / "skills" / "_incoming" / "eta_note").exists()
    entry = SkillLock(root / "skills" / LOCK_FILE).entries["eta_note"]
    assert entry.enabled and entry.sha256 == sha
    import sqlite3

    with sqlite3.connect(settings.db_path) as c:
        kind, payload = c.execute(
            "SELECT kind, payload FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert kind == "skill.enabled" and json.loads(payload)["via"] == "dashboard review"
    assert "eta_note" in ws.list_skills()["installed"]
    assert "enabled" in dash.get("/skills").text


async def test_changed_files_after_review_are_refused(
    ws: Workspace, dash: TestClient, root: Path
) -> None:
    submit_sample(ws)
    html = dash.get("/skills/review/eta_note").text
    sha = html.split('name="sha" value="')[1].split('"')[0]
    # Hermes edits the file directly in the shared folder after you opened the page.
    (root / "skills" / "_incoming" / "eta_note" / "handler.py").write_text(
        HANDLER + "\n# sneaky\n", encoding="utf-8"
    )
    r = dash.post(
        "/skills/review/eta_note",
        data={"action": "approve", "token": "T", "sha": sha},
        follow_redirects=False,
    )
    assert "changed" in r.headers["location"]
    assert not (root / "skills" / "eta_note").exists()


async def test_reject_leaves_a_note_for_hermes(ws: Workspace, dash: TestClient, root: Path) -> None:
    submit_sample(ws)
    assert (await ws.submit("eta_note", "s"))["submitted"]
    r = dash.post(
        "/skills/review/eta_note",
        data={"action": "reject", "token": "T", "note": "say 'back by' instead"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    incoming = ws.list_skills()["incoming"]
    assert incoming[0]["review"]["status"] == "rejected"
    assert incoming[0]["review"]["note"] == "say 'back by' instead"
    assert not (root / "skills" / "eta_note").exists()


async def test_failing_skill_shows_no_approve_button(ws: Workspace, dash: TestClient) -> None:
    ws.scaffold("broken", "x")
    ws.write_file("broken", "handler.py", "import socket\n\ndef run(i, c):\n    return {}\n")
    html = dash.get("/skills/review/broken").text
    assert "import &#39;socket&#39; is not allowed" in html or "socket" in html
    assert "Approve and enable" not in html


def test_bad_names_in_urls_404(dash: TestClient) -> None:
    assert dash.get("/skills/review/..%2F..%2Fconfig").status_code == 404
    r = dash.post("/skills/review/..", data={"action": "approve", "token": "T", "sha": "x"})
    assert r.status_code in (404, 405)


# ---------------------------------------------------------------------- requests


async def test_make_a_skill_request_reaches_hermes(
    ws: Workspace, root: Path, example_policy_dict: dict[str, Any]
) -> None:
    from flins.core.policy_schema import Policy

    policy = Policy.model_validate(example_policy_dict)
    clock = FrozenClock(datetime(2026, 11, 8, 10, 0, tzinfo=UTC))
    conn = await connect(root / "m.db")
    await migrate(conn)
    await conn.close()
    modes = await ModeManager.open(root / "m.db", EventBus(), policy, clock)
    try:
        router = CommandRouter(modes, policy, clock, skill_requests=root / "skills" / "_incoming")
        reply = await router.handle_text(
            "Make a skill that replies to 'where are you' with when I'll be back",
            source="voice",
        )
    finally:
        await modes.close()
    assert reply is not None and "Saved a skill request for Hermes" in reply
    reqs = ws.requests()
    assert len(reqs) == 1 and "where are you" in reqs[0]["title"]
    assert "Asked by the user (voice)" in ws.read_request(reqs[0]["id"])


# ---------------------------------------------------------------------- WSL setup check


def test_doctor_warns_about_mirrored_networking(tmp_path: Path) -> None:
    cfg = tmp_path / ".wslconfig"
    assert wsl_networking_warning(cfg) is None
    cfg.write_text("[wsl2]\nnetworkingMode=NAT\n", encoding="utf-8")
    assert wsl_networking_warning(cfg) is None
    cfg.write_text("[wsl2]\nnetworkingMode = mirrored # fast\n", encoding="utf-8")
    assert "mirrored" in (wsl_networking_warning(cfg) or "")
