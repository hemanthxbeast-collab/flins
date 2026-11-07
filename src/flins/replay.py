"""Replay harness: run a scripted conversation through the real FLINS code on a frozen clock.

A script is JSON Lines. The first line is a header:
  {"start": "2026-11-09T10:00:00+05:30", "me": "9190...@s.whatsapp.net",
   "contacts": [{"alias": "rahul", "pn": "...", "name": "Rahul", "tier": "trusted"},
                {"alias": "x", "pn": "..."}]}      # no name/tier: not in the address book

Then one step per line:
  {"after": "2m"}                                  advance the clock (s/m/h)
  {"say": "I'm in working mood till 6pm"}          a command, as typed on the CLI
  {"self": "status"}                               a message in the WhatsApp self-chat
  {"msg": {"id": "r1", "from": "rahul", "text": "...", "type": "text", "group": false},
   "triage": {"category": "availability_query"},   an incoming message with recorded LLM
   "draft": {"reply": "...", "confidence": 0.9, ...},  answers (draft_v1 / redflag_v1 shape).
   "redflag": {"safe": true, "reason": "ok"}}      Missing draft -> drafting fails -> NOTIFY.
  {"tick": true}                                   one timer tick (expiry, warnings)
  {"restart": true}                                FLINS restarts (mode reloaded from the DB)
  {"expect": "mode", "mode": "WORKING", "until": "18:00"}
  {"expect": "decision", "msg": "r1", "level": "AUTO", "rules": [...], "escalate": true, ...}
  {"expect": "reply", "contains": "Working mode on", "absent": false}
  {"expect": "history", "count": 3}
  {"expect": "draft", "msg": "r1", "would_send": true, "contains": "after 18:00"}
Header option "debounce": true groups bursts; they are processed on {"tick": true}.
Lines starting with // are comments.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from flins.adapters.base import ContactInfo, ContactsUpsert, InboundMessage, MeInfo, SidecarHello
from flins.adapters.fake import FakeAdapter
from flins.core.audit import AuditLog
from flins.core.clock import FrozenClock
from flins.core.commands import CommandRouter, SelfChatCommands
from flins.core.db import connect, migrate
from flins.core.events import Event, EventBus
from flins.core.ingest import Ingestor
from flins.core.levels import Level
from flins.core.modes import ModeManager
from flins.core.pipeline import Brain, Pipeline
from flins.core.policy_schema import Policy
from flins.core.triage import TriageResult
from flins.llm.gateway import FakeBackend, LLMGateway
from flins.llm.prompts import load_prompt
from flins.llm.services import Drafter, RedFlagChecker, Triager

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


class ReplayFailure(AssertionError):
    pass


@dataclass
class ReplayReport:
    steps: int = 0
    expectations: int = 0
    replies: list[str] = field(default_factory=list)


def _duration(text: str) -> timedelta:
    m = re.fullmatch(r"(\d+)\s*([smh])", text.strip())
    if not m:
        raise ValueError(f"bad duration {text!r}; use e.g. '30s', '2m', '1h'")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours"}[unit]: n})


def load_script(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(x) for x in lines if x.strip() and not x.lstrip().startswith("//")]


async def run_script(steps: list[dict[str, Any]], db_path: Path, policy: Policy) -> ReplayReport:
    header, *body = steps
    tz = ZoneInfo(policy.user.timezone)
    clock = FrozenClock(datetime.fromisoformat(header["start"]))
    me = header.get("me", "919000000000@s.whatsapp.net")
    aliases: dict[str, str] = {}

    conn = await connect(db_path)
    await migrate(conn)
    bus = EventBus()
    adapter = FakeAdapter()
    events = adapter.events()
    ingest = Ingestor(conn, bus, AuditLog(conn))
    triage_for: dict[str, TriageResult] = {}

    async def triage(msg: aiosqlite.Row, contact: aiosqlite.Row | None) -> TriageResult | None:
        return triage_for.get(msg["wa_msg_id"])

    # Recorded LLM answers for the message being processed (drafts and red flags).
    current: dict[str, Any] = {}
    backend = FakeBackend(
        {
            "draft_v1": lambda prompt, user: current.get("draft"),
            "redflag_v1": lambda prompt, user: current.get(
                "redflag", {"safe": True, "reason": "ok"}
            ),
        }
    )
    gateway = LLMGateway(backend, conn, clock, daily_budget_usd=1.0, tz=tz)
    brain = Brain(
        gateway=gateway,
        triager=Triager(gateway, load_prompt(PROMPTS_DIR, "triage_v1"), []),
        drafter=Drafter(gateway, load_prompt(PROMPTS_DIR, "draft_v1")),
        redflag=RedFlagChecker(gateway, load_prompt(PROMPTS_DIR, "redflag_v1")),
    )
    modes = await ModeManager.open(db_path, bus, policy, clock)
    router = CommandRouter(modes, policy, clock)
    pipeline = Pipeline(
        conn,
        bus,
        policy,
        modes,
        clock,
        brain=brain,
        triage=triage,
        debounce=bool(header.get("debounce")),
    )
    SelfChatCommands(conn, bus, router)
    report = ReplayReport()

    async def on_reply(e: Event) -> None:
        report.replies.append(e.data["text"])

    bus.subscribe("command.reply", on_reply)

    async def feed(event: Any) -> None:
        await adapter.push(event)
        await ingest.handle(await anext(events))

    await feed(SidecarHello(sidecar_version="replay", state="open", me=MeInfo(id=me)))
    book = [c for c in header.get("contacts", []) if c.get("name")]
    for c in header.get("contacts", []):
        aliases[c["alias"]] = c["pn"]
    await feed(ContactsUpsert(contacts=[ContactInfo(pn=c["pn"], name=c["name"]) for c in book]))
    for c in book:
        if "tier" in c:
            await conn.execute(
                "UPDATE contacts SET tier = ? WHERE wa_jid = ?", (c["tier"], c["pn"])
            )
    await conn.commit()

    msg_ids: dict[str, int] = {}
    counter = 0
    try:
        for n, step in enumerate(body, start=2):
            report.steps += 1
            try:
                if "expect" in step:
                    report.expectations += 1
                    await _check(step, conn, modes, report, msg_ids, tz)
                elif "after" in step:
                    clock.advance(_duration(step["after"]))
                elif "say" in step:
                    reply = await router.handle_text(step["say"], source=step.get("source", "cli"))
                    report.replies.append(reply if reply is not None else "<not a command>")
                elif "self" in step or "msg" in step:
                    counter += 1
                    if "self" in step:
                        inbound = InboundMessage(
                            wa_msg_id=f"self-{counter}",
                            chat_jid=me,
                            sender_jid=me,
                            from_me=True,
                            ts_ms=int(clock.now().timestamp() * 1000),
                            type="text",
                            text=step["self"],
                            upsert_type=step.get("upsert", "notify"),
                        )
                    else:
                        m = step["msg"]
                        jid = m.get("chat") or aliases[m["from"]]
                        sender = aliases.get(m["from"], m["from"])
                        inbound = InboundMessage(
                            wa_msg_id=m["id"],
                            chat_jid=jid,
                            sender_jid=sender,
                            is_group=bool(m.get("group")),
                            ts_ms=int(clock.now().timestamp() * 1000),
                            type=m.get("type", "text"),
                            text=m.get("text"),
                            push_name=m.get("push_name"),
                        )
                        if "triage" in step:
                            triage_for[m["id"]] = TriageResult(**step["triage"])
                        current.clear()
                        current.update({k: step[k] for k in ("draft", "redflag") if k in step})
                    await feed(inbound)
                    async with conn.execute(
                        "SELECT id FROM messages WHERE wa_msg_id = ?", (inbound.wa_msg_id,)
                    ) as cur:
                        row = await cur.fetchone()
                    if row is not None:
                        msg_ids[inbound.wa_msg_id] = int(row[0])
                elif "tick" in step:
                    await pipeline.flush_due()
                    result = await modes.tick()
                    if result is not None:
                        report.replies.append(result.readback)
                elif "restart" in step:
                    await modes.close()
                    modes = await ModeManager.open(db_path, bus, policy, clock)
                    router._modes = modes  # same objects, reloaded mode state
                    pipeline._modes = modes
                else:
                    raise ValueError(f"unknown step {step}")
            except ReplayFailure as exc:
                raise ReplayFailure(f"line {n}: {exc}") from None
    finally:
        await modes.close()
        await adapter.close()
        await conn.close()
    return report


async def _check(
    step: dict[str, Any],
    conn: aiosqlite.Connection,
    modes: ModeManager,
    report: ReplayReport,
    msg_ids: dict[str, int],
    tz: ZoneInfo,
) -> None:
    kind = step["expect"]
    if kind == "mode":
        await modes.refresh()
        s = modes.state
        if s.mode.value != step["mode"]:
            raise ReplayFailure(f"mode is {s.mode.value}, expected {step['mode']}")
        if "until" in step:
            got = s.until.astimezone(tz).strftime("%H:%M") if s.until else None
            if got != step["until"]:
                raise ReplayFailure(f"until is {got}, expected {step['until']}")
    elif kind == "reply":
        last = report.replies[-1] if report.replies else ""
        if step.get("absent"):
            if "contains" in step and step["contains"] in last:
                raise ReplayFailure(f"reply should not contain {step['contains']!r}: {last!r}")
        elif step["contains"] not in last:
            raise ReplayFailure(f"last reply {last!r} lacks {step['contains']!r}")
    elif kind == "no_reply":
        if len(report.replies) != step["count"]:
            raise ReplayFailure(f"{len(report.replies)} replies so far, expected {step['count']}")
    elif kind == "history":
        async with conn.execute("SELECT COUNT(*) FROM mode_history") as cur:
            row = await cur.fetchone()
        if row is None or row[0] != step["count"]:
            raise ReplayFailure(
                f"mode_history has {row[0] if row else 0} rows, expected {step['count']}"
            )
    elif kind == "draft":
        mid = msg_ids.get(step["msg"])
        async with conn.execute(
            "SELECT dr.* FROM drafts dr JOIN decisions de ON de.id = dr.decision_id "
            "WHERE de.message_id = ? ORDER BY dr.id DESC LIMIT 1",
            (mid,),
        ) as cur:
            dr = await cur.fetchone()
        if step.get("none"):
            if dr is not None:
                raise ReplayFailure(f"message {step['msg']} should have no draft: {dr['text']!r}")
            return
        if dr is None:
            raise ReplayFailure(f"no draft for message {step['msg']}")
        if "would_send" in step and bool(dr["would_send"]) != step["would_send"]:
            raise ReplayFailure(
                f"draft for {step['msg']}: would_send is {bool(dr['would_send'])} "
                f"({dr['guard_results']})"
            )
        if "contains" in step and step["contains"] not in dr["text"]:
            raise ReplayFailure(f"draft {dr['text']!r} lacks {step['contains']!r}")
    elif kind == "decision":
        mid = msg_ids.get(step["msg"])
        async with conn.execute(
            "SELECT * FROM decisions WHERE message_id = ? ORDER BY id DESC LIMIT 1", (mid,)
        ) as cur:
            d = await cur.fetchone()
        if d is None:
            if step.get("none"):
                return
            raise ReplayFailure(f"no decision for message {step['msg']}")
        if step.get("none"):
            raise ReplayFailure(f"message {step['msg']} should not have a decision")
        trace = json.loads(d["ceilings"])
        checks: dict[str, tuple[Any, Any]] = {}
        if "level" in step:
            checks["level"] = (Level(d["final_level"]).name, step["level"])
        if "action" in step:
            checks["action"] = (d["action"], step["action"])
        if "category" in step:
            checks["category"] = (d["category"], step["category"])
        if "escalate" in step:
            checks["escalate"] = (bool(d["escalate"]), step["escalate"])
        if "rules" in step:
            got_rules = sorted(d["hard_rule"].split(",")) if d["hard_rule"] else []
            checks["rules"] = (got_rules, sorted(step["rules"]))
        if "flags" in step:
            checks["flags"] = (sorted(json.loads(d["flags"])), sorted(step["flags"]))
        bad = {k: v for k, v in checks.items() if v[0] != v[1]}
        if bad:
            raise ReplayFailure(
                f"decision for {step['msg']}: "
                + ", ".join(f"{k} is {g!r}, expected {e!r}" for k, (g, e) in bad.items())
                + f" (ceilings {trace})"
            )
    else:
        raise ValueError(f"unknown expectation {kind!r}")
