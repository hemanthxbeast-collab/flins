"""M4: approvals -> outbox -> adapter, with every check, the kill switch and idempotency."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

from flins.adapters.base import (
    ContactInfo,
    ContactsUpsert,
    InboundMessage,
    MeInfo,
    SendResult,
    SidecarHello,
)
from flins.adapters.fake import FakeAdapter
from flins.core.approvals import ApprovalService
from flins.core.audit import AuditLog
from flins.core.clock import FrozenClock
from flins.core.commands import CommandRouter
from flins.core.db import connect, migrate
from flins.core.events import EventBus
from flins.core.ingest import Ingestor
from flins.core.modes import ModeEvent, ModeManager
from flins.core.outbox import OutboxError, OutboxWorker, enqueue
from flins.core.pipeline import Brain, Pipeline
from flins.core.policy_schema import Policy
from flins.llm.gateway import FakeBackend, LLMGateway
from flins.llm.prompts import load_prompt
from flins.llm.services import Drafter, RedFlagChecker, Triager
from tests.conftest import REPO_ROOT

T0 = datetime(2026, 11, 9, 4, 30, tzinfo=UTC)
ME = "919000000000@s.whatsapp.net"
KAVYA = "919777777777@s.whatsapp.net"
DRAFT_TEXT = "Hemanth's busy till 13:30, he'll get back to you then."


@dataclass
class Rig:
    conn: aiosqlite.Connection
    clock: FrozenClock
    modes: ModeManager
    adapter: FakeAdapter
    worker: OutboxWorker
    approvals: ApprovalService
    router: CommandRouter
    ingest: Ingestor
    policy: Policy
    n: int = 0

    async def message(self, text: str, sender: str = KAVYA) -> None:
        self.n += 1
        await self.ingest.handle(
            InboundMessage(
                wa_msg_id=f"m{self.n}",
                chat_jid=sender,
                sender_jid=sender,
                text=text,
                ts_ms=int(self.clock.now().timestamp() * 1000),
                type="text",
            )
        )

    async def outbox(self) -> list[aiosqlite.Row]:
        async with self.conn.execute("SELECT * FROM outbox ORDER BY id") as cur:
            return list(await cur.fetchall())


async def make_rig(tmp_path: Path, data: dict[str, Any], dry_run: bool) -> tuple[Rig, Any]:
    policy = Policy.model_validate({**data, "dry_run": dry_run})
    db = tmp_path / "t.db"
    conn = await connect(db)
    await migrate(conn)
    clock = FrozenClock(T0)
    bus = EventBus()
    backend = FakeBackend(
        {
            "triage_v1": {
                "category": "availability_query",
                "urgency": 0,
                "flags": [],
                "needs_human": False,
                "injection_suspected": False,
                "language": "en",
                "summary": "asks if free",
            },
            "draft_v1": {
                "reply": DRAFT_TEXT,
                "confidence": 0.9,
                "makes_commitment": False,
                "mentions_facts_not_provided": False,
                "reason": "ok",
            },
            "redflag_v1": {"safe": True, "reason": "ok"},
        }
    )
    gw = LLMGateway(backend, conn, clock, daily_budget_usd=1.0, tz=ZoneInfo("Asia/Kolkata"))
    prompts = REPO_ROOT / "prompts"
    brain = Brain(
        gw,
        Triager(gw, load_prompt(prompts, "triage_v1"), list(policy.categories)),
        Drafter(gw, load_prompt(prompts, "draft_v1")),
        RedFlagChecker(gw, load_prompt(prompts, "redflag_v1")),
    )
    modes = await ModeManager.open(db, bus, policy, clock)
    ingest = Ingestor(conn, bus, AuditLog(conn))
    Pipeline(conn, bus, policy, modes, clock, brain=brain)
    adapter = FakeAdapter()
    worker = OutboxWorker(conn, policy, adapter, clock, bus)
    approvals = ApprovalService(conn, policy)
    router = CommandRouter(modes, policy, clock, approvals)
    await ingest.handle(SidecarHello(sidecar_version="t", state="open", me=MeInfo(id=ME)))
    await ingest.handle(ContactsUpsert(contacts=[ContactInfo(pn=KAVYA, name="Kavya")]))
    await modes.apply(ModeEvent.ENTER_WORKING, source="test")
    rig = Rig(conn, clock, modes, adapter, worker, approvals, router, ingest, policy)
    return rig, modes


@pytest.fixture
async def rig(tmp_path: Path, example_policy_dict: dict[str, Any]) -> AsyncIterator[Rig]:
    r, modes = await make_rig(tmp_path, example_policy_dict, dry_run=False)
    yield r
    await modes.close()
    await r.conn.close()


async def test_known_contact_gets_numbered_draft(rig: Rig) -> None:
    await rig.message("Are you free now")
    pending = await rig.approvals.pending(rig.clock.now())
    assert [(p.short_id, p.draft) for p in pending] == [(1, DRAFT_TEXT)]
    assert await rig.outbox() == []  # nothing goes anywhere without approval


async def test_approve_sends_once_with_signature(rig: Rig) -> None:
    await rig.message("Are you free now")
    reply = await rig.router.handle_text("y1", source="self_chat")
    assert reply is not None and "Sending #1" in reply
    rig.clock.advance(timedelta(seconds=5))
    await rig.worker.run_due()
    assert len(rig.adapter.sent) == 1
    sent = rig.adapter.sent[0]
    assert sent["chat_id"] == KAVYA
    assert sent["text"] == f"{DRAFT_TEXT} {rig.policy.user.signature}"
    row = (await rig.outbox())[0]
    assert row["status"] == "sent" and row["wa_msg_id"] == "fake-1" and row["sent_at"]
    # Approving again does nothing.
    again = await rig.router.handle_text("y1", source="self_chat")
    assert again is not None and "already approved" in again
    await rig.worker.run_due()
    assert len(rig.adapter.sent) == 1


async def test_not_due_yet_is_not_sent(rig: Rig) -> None:
    await rig.message("Are you free now")
    await rig.router.handle_text("y1", source="self_chat")
    await rig.worker.run_due()  # the 3 s delay hasn't passed
    assert rig.adapter.sent == []


async def test_edit_sends_your_words_and_learns(rig: Rig) -> None:
    await rig.message("Are you free now")
    await rig.router.handle_text("e1 Not right now, call you at 8!", source="self_chat")
    rig.clock.advance(timedelta(seconds=5))
    await rig.worker.run_due()
    assert rig.adapter.sent[0]["text"] == "Not right now, call you at 8!"
    async with rig.conn.execute("SELECT draft, final FROM tone_examples") as cur:
        assert [tuple(r) for r in await cur.fetchall()] == [
            (DRAFT_TEXT, "Not right now, call you at 8!")
        ]


async def test_reject_and_expiry(rig: Rig) -> None:
    await rig.message("Are you free now")
    reply = await rig.router.handle_text("n1", source="self_chat")
    assert reply is not None and "Rejected #1" in reply
    await rig.message("hello?")
    rig.clock.advance(timedelta(minutes=31))  # approvals expire after 30 min
    reply = await rig.router.handle_text("y2", source="self_chat")
    assert reply is not None and "expired" in reply
    rig.clock.advance(timedelta(seconds=5))
    await rig.worker.run_due()
    assert rig.adapter.sent == []


async def test_pending_list_and_bare_send_it(rig: Rig) -> None:
    await rig.message("Are you free now")
    listing = await rig.router.handle_text("pending", source="self_chat")
    assert listing is not None and "#1 Kavya" in listing
    reply = await rig.router.handle_text("send it", source="self_chat")  # only one pending
    assert reply is not None and "Sending #1" in reply


async def test_kill_switch_cancels_queued_within_a_second(rig: Rig) -> None:
    await rig.message("Are you free now")
    await rig.router.handle_text("y1", source="self_chat")
    started = time.monotonic()
    await rig.modes.apply(ModeEvent.KILL, source="test")
    elapsed = time.monotonic() - started
    row = (await rig.outbox())[0]
    assert row["status"] == "cancelled" and elapsed < 1.0
    rig.clock.advance(timedelta(seconds=5))
    await rig.worker.run_due()
    assert rig.adapter.sent == []


async def test_stop_from_another_process_blocks_sending(rig: Rig, tmp_path: Path) -> None:
    await rig.message("Are you free now")
    await rig.router.handle_text("y1", source="self_chat")
    other = await ModeManager.open(tmp_path / "t.db", EventBus(), rig.policy, rig.clock)
    await other.apply(ModeEvent.KILL, source="cli")  # `flins stop` in another terminal
    await other.close()
    rig.clock.advance(timedelta(seconds=5))
    await rig.worker.run_due()
    assert rig.adapter.sent == []
    assert (await rig.outbox())[0]["status"] == "cancelled"


async def test_dry_run_records_but_never_sends(
    tmp_path: Path, example_policy_dict: dict[str, Any]
) -> None:
    rig, modes = await make_rig(tmp_path, example_policy_dict, dry_run=True)
    try:
        await rig.message("Are you free now")
        reply = await rig.router.handle_text("y1", source="self_chat")
        assert reply is not None and "dry run is on" in reply
        rig.clock.advance(timedelta(seconds=5))
        await rig.worker.run_due()
        assert rig.adapter.sent == []
        assert (await rig.outbox())[0]["status"] == "dry_run"
    finally:
        await modes.close()
        await rig.conn.close()


async def test_enqueue_refuses_groups_and_strangers(rig: Rig) -> None:
    now = rig.clock.now()
    with pytest.raises(OutboxError, match="groups"):
        await enqueue(rig.conn, chat_jid="1203@g.us", text="hi", level=2, now=now)
    with pytest.raises(OutboxError, match="24 hours"):
        await enqueue(rig.conn, chat_jid="919888888888@s.whatsapp.net", text="hi", level=2, now=now)


async def test_send_failure_is_recorded_not_retried(rig: Rig) -> None:
    class Broken(FakeAdapter):
        async def send_text(
            self, chat_id: str, text: str, *, quote: str | None, client_msg_id: str
        ) -> SendResult:
            self.sent.append(
                {"chat_id": chat_id, "text": text, "quote": quote, "client_msg_id": client_msg_id}
            )
            return SendResult(ok=False, error="WhatsApp is not connected")

    broken = Broken()
    worker = OutboxWorker(rig.conn, rig.policy, broken, rig.clock, EventBus())
    await rig.message("Are you free now")
    await rig.router.handle_text("y1", source="self_chat")
    rig.clock.advance(timedelta(seconds=5))
    await worker.run_due()
    await worker.run_due()
    row = (await rig.outbox())[0]
    assert row["status"] == "failed" and "not connected" in row["error"]
    assert len(broken.sent) == 1


async def test_hourly_cap(rig: Rig) -> None:
    rig.policy.limits.global_max_auto_per_hour = 0
    await rig.message("Are you free now")
    await rig.router.handle_text("y1", source="self_chat")
    rig.clock.advance(timedelta(seconds=5))
    await rig.worker.run_due()
    assert rig.adapter.sent == []
    assert "hourly" in (await rig.outbox())[0]["error"]
