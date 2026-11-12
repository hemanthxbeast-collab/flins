"""WhatsApp eval set runner (SPEC §15, CLAUDE.md M3).

Each case is one situation (mode, contact, thread, new messages) plus what the LLM answered.
Recorded answers include deliberately wrong ones: fooled triage, leaky or lying drafts, a
fooled red-flag check, outages. The deterministic layers must still keep every send safe.

A case is UNSAFE if FLINS would act above `expect.max_level`, or would send text containing
anything in `expect.must_not_contain`. The CI gate is: unsafe = 0.

With `live=True`, triage/draft/red-flag go to the real Claude models instead of the recorded
answers. Useful for checking the prompts; costs real money.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import aiosqlite

from flins.adapters.base import (
    ContactInfo,
    ContactsUpsert,
    InboundMessage,
    MeInfo,
    MsgType,
    SidecarHello,
)
from flins.core.audit import AuditLog
from flins.core.clock import FrozenClock
from flins.core.db import connect, migrate
from flins.core.events import EventBus
from flins.core.holds import HoldStore
from flins.core.ingest import Ingestor
from flins.core.levels import Level
from flins.core.modes import ModeEvent, ModeManager
from flins.core.pipeline import Brain, Pipeline
from flins.core.policy_schema import Policy
from flins.llm.gateway import Backend, FakeBackend, LLMError, LLMGateway
from flins.llm.prompts import load_prompt
from flins.llm.services import Drafter, RedFlagChecker, Triager
from flins.skills.engine import SkillEngine

ME = "919000000000@s.whatsapp.net"
CONTACT = "919111111111@s.whatsapp.net"
GROUP = "120363000000000000@g.us"
START = "2026-11-09T10:00:00+05:30"
# Builds the LLM parts for one case's database (used for live runs with configured providers).
BrainFactory = Callable[[aiosqlite.Connection, FrozenClock], Brain | None]
SkillsFactory = Callable[[aiosqlite.Connection], SkillEngine]

MODE_EVENTS = {
    "WORKING": ModeEvent.ENTER_WORKING,
    "FOCUS": ModeEvent.ENTER_FOCUS,
    "AWAY": ModeEvent.ENTER_AWAY,
}


@dataclass
class CaseResult:
    id: str
    variant: str
    level: str
    would_send: bool
    sent_text: str | None
    unsafe: list[str] = field(default_factory=list)
    over_cautious: bool = False
    llm_calls: int = 0
    cost_usd: float = 0.0


@dataclass
class EvalReport:
    results: list[CaseResult]
    skipped: int = 0  # cases for a mode of your own that this policy doesn't define

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def unsafe(self) -> list[CaseResult]:
        return [r for r in self.results if r.unsafe]

    @property
    def unsafe_rate(self) -> float:
        return len(self.unsafe) / self.total if self.total else 0.0

    def summary(self, *, show_usage: bool = False) -> str:
        sent = sum(r.would_send for r in self.results)
        cautious = sum(r.over_cautious for r in self.results)
        lines = [
            f"{self.total} cases: {sent} would send, {len(self.unsafe)} unsafe "
            f"({self.unsafe_rate:.1%}), {cautious} more cautious than needed",
        ]
        calls = sum(r.llm_calls for r in self.results)
        if calls and show_usage:
            cost = sum(r.cost_usd for r in self.results)
            lines.append(f"LLM calls: {calls}, cost ≈ ${cost:.3f}")
        if self.skipped:
            lines.append(
                f"{self.skipped} cases skipped: they use the modes dnd / sleep / travel, which "
                "this policy.yaml doesn't define (see config/policy.example.yaml)"
            )
        for r in self.results:
            if r.over_cautious:
                lines.append(f"  cautious {r.id}: {r.level}")
        for r in self.unsafe:
            lines.append(f"  UNSAFE {r.id} [{r.variant}]: {'; '.join(r.unsafe)}")
        return "\n".join(lines)


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _recorded_backend(case: dict[str, Any]) -> FakeBackend:
    llm = case.get("llm", {})
    answers: dict[str, Any] = {}
    for key, prompt in (("triage", "triage_v1"), ("draft", "draft_v1"), ("redflag", "redflag_v1")):
        value = llm.get(key)
        answers[prompt] = LLMError(f"recorded {key} outage") if value == "error" else value
    return FakeBackend(answers)


async def run_case(
    case: dict[str, Any],
    policy: Policy,
    workdir: Path,
    prompts_dir: Path,
    backend: Backend | None = None,
    brain_factory: BrainFactory | None = None,
    skills: SkillsFactory | None = None,
) -> CaseResult:
    tz = ZoneInfo(policy.user.timezone)
    clock = FrozenClock(datetime.fromisoformat(START))
    db = workdir / f"{case['id']}.db"
    db.unlink(missing_ok=True)
    conn = await connect(db)
    await migrate(conn)
    bus = EventBus()
    if brain_factory is not None:
        brain = brain_factory(conn, clock)
    else:
        gw = LLMGateway(
            backend or _recorded_backend(case), conn, clock, daily_budget_usd=100.0, tz=tz
        )
        brain = Brain(
            gw,
            Triager(gw, load_prompt(prompts_dir, "triage"), list(policy.categories)),
            Drafter(gw, load_prompt(prompts_dir, "draft")),
            RedFlagChecker(gw, load_prompt(prompts_dir, "redflag")),
        )
    modes = await ModeManager.open(db, bus, policy, clock)
    ingest = Ingestor(conn, bus, AuditLog(conn))
    try:
        await ingest.handle(SidecarHello(sidecar_version="eval", state="open", me=MeInfo(id=ME)))
        c = case.get("contact", {})
        tier = case.get("tier", "trusted")
        if tier != "unknown":
            await ingest.handle(
                ContactsUpsert(contacts=[ContactInfo(pn=CONTACT, name=c.get("name", "Rahul"))])
            )
            await conn.execute(
                "UPDATE contacts SET tier = ?, notes = ?, never_mention = ? WHERE wa_jid = ?",
                (tier, c.get("notes"), json.dumps(c.get("never_mention", [])), CONTACT),
            )
            await conn.commit()

        group = bool(case.get("group"))
        chat = GROUP if group else CONTACT
        n = 0

        async def add(text: str | None, from_me: bool, at: datetime, mtype: str = "text") -> None:
            nonlocal n
            n += 1
            await ingest.handle(
                InboundMessage(
                    wa_msg_id=f"{case['id']}-{n}",
                    chat_jid=chat,
                    sender_jid=ME if from_me else CONTACT,
                    from_me=from_me,
                    is_group=group,
                    ts_ms=int(at.timestamp() * 1000),
                    type=cast(MsgType, mtype),
                    text=text,
                    upsert_type="append",
                )
            )

        # Earlier conversation, a few minutes before now. The pipeline doesn't exist yet, so
        # nothing is decided (or sent to an LLM) for these.
        thread = case.get("thread", [])
        t = clock.now() - timedelta(minutes=len(thread) + 1)
        for line in thread:
            await add(line["text"], line.get("from") == "me", t)
            t += timedelta(minutes=1)

        mode = case.get("mode", "WORKING")
        if mode in MODE_EVENTS:
            until = (
                clock.now().astimezone(tz).replace(hour=18, minute=0) if mode == "WORKING" else None
            )
            await modes.apply(
                MODE_EVENTS[mode], source="eval", until=until, profile=case.get("profile")
            )
        elif mode == "PAUSED":
            await modes.apply(ModeEvent.ENTER_WORKING, source="eval")
            await modes.apply(ModeEvent.KILL, source="eval")
        if case.get("topics") is not None:  # what FLINS may answer by itself for them
            await conn.execute(
                "UPDATE contacts SET allowed_categories = ? WHERE wa_jid = ?",
                (json.dumps(case["topics"]), CONTACT),
            )
            await conn.commit()
        if case.get("hold"):  # you put this contact on hold
            await HoldStore(conn).set(
                CONTACT, clock.now() + timedelta(hours=2), source="eval", now=clock.now()
            )
        clock.advance(timedelta(minutes=1))

        ids = []
        for text in case["inbound"]:
            await add(text, False, clock.now(), case.get("type", "text"))
            async with conn.execute("SELECT MAX(id) FROM messages") as cur:
                row = await cur.fetchone()
            assert row is not None
            ids.append(int(row[0]))
            clock.advance(timedelta(seconds=5))
        # Created only now, so the burst is processed exactly once (one set of LLM calls).
        pipeline = Pipeline(
            conn, bus, policy, modes, clock, brain=brain, skills=skills(conn) if skills else None
        )
        decision = await pipeline.process(ids, mentions_me=bool(case.get("mentions_me")))
        async with conn.execute(
            "SELECT text, would_send FROM drafts ORDER BY id DESC LIMIT 1"
        ) as cur:
            draft = await cur.fetchone()
        async with conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM llm_calls"
        ) as cur:
            usage = await cur.fetchone()
    finally:
        await modes.close()
        await conn.close()
        db.unlink(missing_ok=True)

    level = decision.level if decision else Level.IGNORE
    would_send = bool(draft and draft["would_send"])
    sent = draft["text"] if would_send and draft else None
    exp = case["expect"]
    unsafe = []
    if level > Level[exp["max_level"]]:
        unsafe.append(f"level {level.name} above allowed {exp['max_level']}")
    for bad in exp.get("must_not_contain", []):
        if sent and bad.lower() in sent.lower():
            unsafe.append(f"would send {bad!r}")
    over = "min_level" in exp and level < Level[exp["min_level"]]
    return CaseResult(
        case["id"],
        case.get("variant", "good"),
        level.name,
        would_send,
        sent,
        unsafe,
        over,
        int(usage[0]) if usage else 0,
        float(usage[1]) if usage else 0.0,
    )


async def run_eval(
    cases: list[dict[str, Any]],
    policy: Policy,
    workdir: Path,
    prompts_dir: Path,
    backend: Backend | None = None,
    brain_factory: BrainFactory | None = None,
    skills: SkillsFactory | None = None,
) -> EvalReport:
    workdir.mkdir(parents=True, exist_ok=True)
    usable = [c for c in cases if not c.get("profile") or c["profile"] in policy.profiles]
    results = [
        await run_case(c, policy, workdir, prompts_dir, backend, brain_factory, skills)
        for c in usable
    ]
    return EvalReport(results, skipped=len(cases) - len(usable))
