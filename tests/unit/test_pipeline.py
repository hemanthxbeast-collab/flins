"""Pipeline behaviour that matters for privacy and cost: when the LLM is (not) called."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from flins.adapters.base import ContactInfo, ContactsUpsert, InboundMessage, MeInfo, SidecarHello
from flins.core.audit import AuditLog
from flins.core.clock import FrozenClock
from flins.core.db import connect, migrate
from flins.core.events import EventBus
from flins.core.ingest import Ingestor
from flins.core.levels import Level
from flins.core.modes import ModeEvent, ModeManager
from flins.core.pipeline import Brain, Pipeline
from flins.core.policy_schema import Policy
from flins.llm.gateway import FakeBackend, LLMGateway
from flins.llm.prompts import load_prompt
from flins.llm.services import Drafter, RedFlagChecker, Triager
from tests.conftest import REPO_ROOT

T0 = datetime(2026, 11, 9, 4, 30, tzinfo=UTC)
ME = "919000000000@s.whatsapp.net"
RAHUL = "919111111111@s.whatsapp.net"
STRANGER = "919999999999@s.whatsapp.net"
TRIAGE = {
    "category": "availability_query",
    "urgency": 0,
    "flags": [],
    "needs_human": False,
    "injection_suspected": False,
    "language": "en",
    "summary": "asks if free",
}
DRAFT = {
    "reply": "He's working till 13:30, will reply after.",
    "confidence": 0.9,
    "makes_commitment": False,
    "mentions_facts_not_provided": False,
    "reason": "ok",
}


@dataclass
class Rig:
    backend: FakeBackend
    ingest: Ingestor
    modes: ModeManager
    pipeline: Pipeline
    clock: FrozenClock
    n: int = 0

    async def message(self, sender: str, text: str) -> Any:
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
        return self.n


@pytest.fixture
async def rig(tmp_path: Path, example_policy_dict: dict[str, Any]) -> AsyncIterator[Rig]:
    policy = Policy.model_validate(example_policy_dict)
    db = tmp_path / "t.db"
    conn = await connect(db)
    await migrate(conn)
    clock = FrozenClock(T0)
    bus = EventBus()
    backend = FakeBackend(
        {"triage_v1": TRIAGE, "draft_v1": DRAFT, "redflag_v1": {"safe": True, "reason": "ok"}}
    )
    gw = LLMGateway(
        backend,
        conn,
        clock,
        daily_budget_usd=policy.limits.daily_llm_budget_usd,
        tz=ZoneInfo("Asia/Kolkata"),
    )
    prompts = REPO_ROOT / "prompts"
    brain = Brain(
        gw,
        Triager(gw, load_prompt(prompts, "triage_v1"), list(policy.categories)),
        Drafter(gw, load_prompt(prompts, "draft_v1")),
        RedFlagChecker(gw, load_prompt(prompts, "redflag_v1")),
    )
    modes = await ModeManager.open(db, bus, policy, clock)
    ingest = Ingestor(conn, bus, AuditLog(conn))
    pipeline = Pipeline(conn, bus, policy, modes, clock, brain=brain)
    await ingest.handle(SidecarHello(sidecar_version="t", state="open", me=MeInfo(id=ME)))
    await ingest.handle(ContactsUpsert(contacts=[ContactInfo(pn=RAHUL, name="Rahul")]))
    await conn.execute("UPDATE contacts SET tier='trusted' WHERE wa_jid=?", (RAHUL,))
    await conn.commit()
    yield Rig(backend, ingest, modes, pipeline, clock)
    await modes.close()
    await conn.close()


def prompts_called(rig: Rig) -> list[str]:
    return [name for name, _ in rig.backend.calls]


async def test_normal_mode_never_calls_the_llm(rig: Rig) -> None:
    await rig.message(RAHUL, "hey are you free?")
    assert prompts_called(rig) == []


@pytest.mark.parametrize(
    ("sender", "text"),
    [
        (STRANGER, "hey are you free?"),  # unknown sender
        (RAHUL, "can you send 500 rs on upi"),  # money
        (RAHUL, "what's the otp you got"),  # credentials
    ],
)
async def test_capped_messages_never_reach_the_llm(rig: Rig, sender: str, text: str) -> None:
    await rig.modes.apply(ModeEvent.ENTER_WORKING, source="test")
    await rig.message(sender, text)
    assert prompts_called(rig) == []


async def test_paused_never_calls_the_llm(rig: Rig) -> None:
    await rig.modes.apply(ModeEvent.ENTER_WORKING, source="test")
    await rig.modes.apply(ModeEvent.KILL, source="test")
    await rig.message(RAHUL, "hey are you free?")
    assert prompts_called(rig) == []


async def test_full_path_for_an_allowed_message(rig: Rig) -> None:
    await rig.modes.apply(ModeEvent.ENTER_WORKING, source="test")
    await rig.message(RAHUL, "hey are you free?")
    assert prompts_called(rig) == ["triage_v1", "draft_v1", "redflag_v1"]
    # No phone numbers or WhatsApp ids reach the model, only names.
    for _, user_text in rig.backend.calls:
        assert "@s.whatsapp.net" not in user_text and "919111111111" not in user_text


async def test_budget_exhausted_falls_back_to_notify(rig: Rig) -> None:
    await rig.modes.apply(ModeEvent.ENTER_WORKING, source="test")
    rig.pipeline._brain.gateway._budget = 0.0  # type: ignore[union-attr]
    n = await rig.message(RAHUL, "hey are you free?")
    assert prompts_called(rig) == []
    async with rig.ingest._conn.execute(
        "SELECT final_level FROM decisions d JOIN messages m ON m.id = d.message_id "
        "WHERE m.wa_msg_id = ?",
        (f"m{n}",),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == int(Level.NOTIFY)


async def test_triage_failure_is_cautious(rig: Rig) -> None:
    await rig.modes.apply(ModeEvent.ENTER_WORKING, source="test")
    rig.backend.answers["triage_v1"] = "garbage"
    await rig.message(RAHUL, "hey are you free?")
    # Without triage the category is unknown, so at most a draft for approval.
    async with rig.ingest._conn.execute(
        "SELECT d.final_level, dr.would_send FROM decisions d LEFT JOIN drafts dr "
        "ON dr.decision_id = d.id ORDER BY d.id DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] <= int(Level.DRAFT) and not row[1]
