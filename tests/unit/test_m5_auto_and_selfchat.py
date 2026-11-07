"""M5: automatic holding replies, FLINS notes in your self-chat, the session summary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

from flins.adapters.base import ContactInfo, ContactsUpsert, InboundMessage, MeInfo, SidecarHello
from flins.adapters.fake import FakeAdapter
from flins.core.approvals import ApprovalService
from flins.core.audit import AuditLog
from flins.core.clock import FrozenClock
from flins.core.commands import CommandRouter, SelfChatCommands
from flins.core.db import connect, migrate
from flins.core.events import EventBus
from flins.core.ingest import Ingestor
from flins.core.modes import ModeEvent, ModeManager
from flins.core.outbox import SELF_PREFIX, OutboxWorker
from flins.core.pipeline import Brain, Pipeline
from flins.core.policy_schema import Policy
from flins.core.selfchat import SelfChatNotifier
from flins.llm.gateway import FakeBackend, LLMGateway
from flins.llm.prompts import load_prompt
from flins.llm.services import Drafter, RedFlagChecker, Triager
from tests.conftest import REPO_ROOT

T0 = datetime(2026, 11, 9, 4, 30, tzinfo=UTC)  # 10:00 IST
ME = "919000000000@s.whatsapp.net"
MOM = "919222222222@s.whatsapp.net"
KAVYA = "919777777777@s.whatsapp.net"
RAHUL = "919111111111@s.whatsapp.net"
TRIAGE = {
    "category": "greeting_smalltalk",
    "urgency": 0,
    "flags": [],
    "needs_human": False,
    "injection_suspected": False,
    "language": "en",
    "summary": "small talk",
}
DRAFT = {
    "reply": "He's busy right now and will reply after 13:00.",
    "confidence": 0.9,
    "makes_commitment": False,
    "mentions_facts_not_provided": False,
    "reason": "ok",
}


@dataclass
class Rig:
    conn: aiosqlite.Connection
    clock: FrozenClock
    modes: ModeManager
    adapter: FakeAdapter
    worker: OutboxWorker
    ingest: Ingestor
    policy: Policy
    n: int = 0

    async def msg(
        self, sender: str, text: str, from_me: bool = False, upsert: str = "notify"
    ) -> None:
        self.n += 1
        await self.ingest.handle(
            InboundMessage(
                wa_msg_id=f"m{self.n}",
                chat_jid=sender,
                sender_jid=ME if from_me else sender,
                from_me=from_me,
                text=text,
                ts_ms=int(self.clock.now().timestamp() * 1000),
                type="text",
                upsert_type=upsert,  # type: ignore[arg-type]
            )
        )

    async def self_chat(self, text: str) -> None:
        await self.msg(ME, text, from_me=True)

    async def rows(self, kind: str | None = None) -> list[aiosqlite.Row]:
        sql, args = "SELECT * FROM outbox ORDER BY id", ()
        if kind:
            sql, args = "SELECT * FROM outbox WHERE kind = ? ORDER BY id", (kind,)
        async with self.conn.execute(sql, args) as cur:
            return list(await cur.fetchall())

    async def flush(self, seconds: int = 31) -> None:
        self.clock.advance(timedelta(seconds=seconds))
        await self.worker.run_due()


async def build(tmp_path: Path, data: dict[str, Any], **policy_overrides: Any) -> Rig:
    policy = Policy.model_validate({**data, "dry_run": False, **policy_overrides})
    policy.limits.send_delay_seconds = (30, 30)
    db = tmp_path / "t.db"
    conn = await connect(db)
    await migrate(conn)
    clock = FrozenClock(T0)
    bus = EventBus()
    backend = FakeBackend(
        {"triage": TRIAGE, "draft": DRAFT, "redflag": {"safe": True, "reason": "ok"}}
    )
    gw = LLMGateway(backend, conn, clock, daily_budget_usd=1.0, tz=ZoneInfo("Asia/Kolkata"))
    p = REPO_ROOT / "prompts"
    brain = Brain(
        gw,
        Triager(gw, load_prompt(p, "triage"), list(policy.categories)),
        Drafter(gw, load_prompt(p, "draft")),
        RedFlagChecker(gw, load_prompt(p, "redflag")),
    )
    modes = await ModeManager.open(db, bus, policy, clock)
    ingest = Ingestor(conn, bus, AuditLog(conn))
    Pipeline(conn, bus, policy, modes, clock, brain=brain)
    adapter = FakeAdapter()
    worker = OutboxWorker(conn, policy, adapter, clock, bus)
    approvals = ApprovalService(conn, policy)
    SelfChatCommands(conn, bus, CommandRouter(modes, policy, clock, approvals))
    SelfChatNotifier(conn, bus, policy, clock, lambda: ingest.me)
    await ingest.handle(SidecarHello(sidecar_version="t", state="open", me=MeInfo(id=ME)))
    await ingest.handle(
        ContactsUpsert(
            contacts=[
                ContactInfo(pn=MOM, name="Mom"),
                ContactInfo(pn=KAVYA, name="Kavya"),
                ContactInfo(pn=RAHUL, name="Rahul"),
            ]
        )
    )
    await conn.execute("UPDATE contacts SET tier='inner' WHERE wa_jid=?", (MOM,))
    await conn.execute("UPDATE contacts SET tier='trusted' WHERE wa_jid=?", (RAHUL,))
    await conn.commit()
    await modes.apply(ModeEvent.ENTER_WORKING, source="test")  # until 13:00 IST
    return Rig(conn, clock, modes, adapter, worker, ingest, policy)


@pytest.fixture
async def rig(tmp_path: Path, example_policy_dict: dict[str, Any]) -> AsyncIterator[Rig]:
    r = await build(tmp_path, example_policy_dict)
    yield r
    await r.modes.close()
    await r.conn.close()


def sent_to(rig: Rig, jid: str) -> list[str]:
    return [str(s["text"]) for s in rig.adapter.sent if s["chat_id"] == jid]


# ------------------------------------------------------------------ automatic holding replies


async def test_holding_reply_goes_out_by_itself(rig: Rig) -> None:
    await rig.msg(MOM, "did you eat?")
    auto = await rig.rows("auto")
    assert len(auto) == 1 and auto[0]["approval_id"] is None
    await rig.flush(29)
    assert sent_to(rig, MOM) == []  # waits the delay first
    await rig.flush(2)
    assert sent_to(rig, MOM) == [
        "Hey Mom! Hemanth is busy with work right now and will get back to you after 13:00. "
        "— FLINS, Hemanth's assistant"
    ]


async def test_only_one_holding_reply_per_session(rig: Rig) -> None:
    await rig.msg(MOM, "did you eat?")
    await rig.flush()
    await rig.msg(MOM, "hello??")
    await rig.flush()
    assert len(sent_to(rig, MOM)) == 1


async def test_cancelled_when_you_reply_yourself(rig: Rig) -> None:
    await rig.msg(MOM, "did you eat?")
    await rig.msg(MOM, "yes ma, eating now", from_me=True)  # you answered from your phone
    await rig.flush()
    assert sent_to(rig, MOM) == []
    assert (await rig.rows("auto"))[0]["error"] == "you replied yourself"


@pytest.mark.parametrize("command", ["I'm done", "stop"])
async def test_cancelled_by_leaving_the_mode_or_stop(rig: Rig, command: str) -> None:
    await rig.msg(MOM, "did you eat?")
    await rig.self_chat(command)
    await rig.flush()
    assert sent_to(rig, MOM) == []
    assert (await rig.rows("auto"))[0]["status"] == "cancelled"


async def test_cancelled_if_the_mode_expired_before_sending(rig: Rig) -> None:
    await rig.modes.apply(
        ModeEvent.ENTER_WORKING, source="test", until=rig.clock.now() + timedelta(seconds=10)
    )
    await rig.msg(MOM, "did you eat?")
    await rig.flush()  # 31 s later the mode has ended (not yet ticked)
    assert sent_to(rig, MOM) == []


async def test_auto_replies_off(tmp_path: Path, example_policy_dict: dict[str, Any]) -> None:
    rig = await build(tmp_path, example_policy_dict, auto_replies="off")
    try:
        await rig.msg(MOM, "did you eat?")
        assert await rig.rows("auto") == []
        # It becomes an approval instead.
        assert any("Reply y1" in r["text"] for r in await rig.rows("self"))
    finally:
        await rig.modes.close()
        await rig.conn.close()


async def test_free_form_ai_replies_still_need_approval(rig: Rig) -> None:
    await rig.msg(RAHUL, "hey what's up")  # trusted + small talk = AUTO (AI-written)
    await rig.flush()
    assert sent_to(rig, RAHUL) == []
    assert await rig.rows("auto") == []


# ------------------------------------------------------------------ self-chat notes


async def test_command_answers_go_to_your_self_chat(rig: Rig) -> None:
    await rig.self_chat("status")
    notes = await rig.rows("self")
    assert (
        notes and notes[-1]["text"].startswith(SELF_PREFIX) and "Working mode" in notes[-1]["text"]
    )
    await rig.flush(2)
    assert sent_to(rig, ME)


async def test_flins_never_reacts_to_its_own_notes(rig: Rig) -> None:
    await rig.self_chat("I'm done")  # FLINS answers "🤖 Back to normal. ..."
    await rig.self_chat("working mood")
    before = rig.modes.state.mode
    # The echo of a note that happens to contain command words must do nothing.
    await rig.self_chat(f"{SELF_PREFIX}Back to normal.")
    await rig.self_chat(f"{SELF_PREFIX}stop")
    assert rig.modes.state.mode == before


async def test_notes_still_reach_you_while_paused(rig: Rig) -> None:
    await rig.self_chat("stop")
    await rig.flush(2)
    assert any("Paused" in t for t in sent_to(rig, ME))


async def test_approval_prompt_in_self_chat(rig: Rig) -> None:
    await rig.msg(KAVYA, "are you free now?")  # known contact -> draft for approval
    prompts = [r["text"] for r in await rig.rows("self") if "Reply y1" in r["text"]]
    assert len(prompts) == 1
    assert 'Kavya: "are you free now?"' in prompts[0] and "Draft:" in prompts[0]
    await rig.self_chat("y1")
    await rig.flush(5)
    assert sent_to(rig, KAVYA)


async def test_urgent_alert(rig: Rig) -> None:
    await rig.msg(RAHUL, "URGENT call me now")
    alerts = [r["text"] for r in await rig.rows("self") if "URGENT" in r["text"]]
    assert alerts and 'from Rahul: "URGENT call me now"' in alerts[0]
    assert "urgent acknowledgement" in alerts[0]


# ------------------------------------------------------------------ summary


async def test_session_summary_lists_each_message(rig: Rig) -> None:
    await rig.msg(MOM, "did you eat?")
    await rig.flush()
    await rig.msg(KAVYA, "are you free now?")
    await rig.msg(RAHUL, "URGENT call me now")
    await rig.flush()
    await rig.self_chat("I'm done")
    summary = [r["text"] for r in await rig.rows("self") if "Back to normal" in r["text"]][-1]
    lines = summary.splitlines()
    assert "3 messages" in lines[0]
    assert lines[1].startswith('• ⚠ Rahul: "URGENT call me now"')  # urgent first
    assert any('Mom: "did you eat?" → sent a holding reply' in x for x in lines)
    assert any('Kavya: "are you free now?" → draft #1 waiting for you' in x for x in lines)
