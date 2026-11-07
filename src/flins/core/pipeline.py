"""Decision pipeline (SPEC §6), M3: shadow mode.

ingest -> debounce -> hard rules -> [LLM triage, only if the rules still allow more than
NOTIFY] -> decide() -> draft (template or LLM) -> output guard -> [red flag, for unreviewed
sends] -> record. Nothing is sent: "would send" is recorded and shown (the outbox is M4).
"""

from __future__ import annotations

import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite

from flins.core.approvals import create_for_draft
from flins.core.audit import AuditLog
from flins.core.clock import Clock, from_iso, to_iso
from flins.core.debounce import Debouncer
from flins.core.events import Event, EventBus
from flins.core.guard import GuardInput, GuardResult, check
from flins.core.levels import Level, Mode
from flins.core.modes import ModeManager, ModeState, effective_mode
from flins.core.outbox import OutboxError, enqueue, holding_reply_sent_this_session
from flins.core.policy import (
    Decision,
    DecisionInputs,
    MessageFacts,
    RuleContext,
    decide,
    evaluate_hard_rules,
    is_emergency,
)
from flins.core.policy_schema import TIMED_MODES, Policy
from flins.core.triage import TriageResult, fallback_category
from flins.llm.context import Line, render_conversation
from flins.llm.gateway import LLMError, LLMGateway
from flins.llm.services import Drafter, RedFlagChecker, Triager
from flins.settings import AboutMe, ToneConfig

IDENTICAL_WINDOW = timedelta(minutes=10)
THREAD_LINES = 10

# Replays and tests can inject triage directly instead of calling an LLM.
TriageProvider = Callable[[aiosqlite.Row, aiosqlite.Row | None], Awaitable[TriageResult | None]]


@dataclass
class Brain:
    """The LLM-backed parts. Without a Brain, FLINS still decides but can't triage or draft."""

    gateway: LLMGateway
    triager: Triager
    drafter: Drafter
    redflag: RedFlagChecker


@dataclass(frozen=True)
class DraftOutcome:
    text: str
    source: str  # "template:<name>" | "llm"
    confidence: float | None
    guard: GuardResult
    would_send: bool


class Pipeline:
    def __init__(
        self,
        conn: aiosqlite.Connection,
        bus: EventBus,
        policy: Policy,
        modes: ModeManager,
        clock: Clock,
        *,
        brain: Brain | None = None,
        triage: TriageProvider | None = None,
        tones: ToneConfig | None = None,
        about: AboutMe | None = None,
        debounce: bool = False,
        rng: random.Random | None = None,
    ) -> None:
        self._conn = conn
        self._bus = bus
        self._audit = AuditLog(conn)
        self._policy = policy
        self._modes = modes
        self._clock = clock
        self._brain = brain
        self._triage_override = triage
        self._tones = tones or ToneConfig()
        self._about = about or AboutMe()
        self._rng = rng or random.Random()
        self._tz = ZoneInfo(policy.user.timezone)
        lim = policy.limits
        self._debouncer = (
            Debouncer(
                timedelta(seconds=lim.debounce_quiet_seconds),
                timedelta(seconds=lim.debounce_max_seconds),
            )
            if debounce
            else None
        )
        bus.subscribe("wa.message", self._on_message)

    # ------------------------------------------------------------------ intake

    async def _on_message(self, event: Event) -> None:
        d = event.data
        if d.get("direction") != "IN" or d.get("self_chat"):
            return  # our own messages and self-chat commands never trigger replies
        mid = int(d["message_id"])
        if self._debouncer is None:
            await self.process([mid], mentions_me=bool(d.get("mentions_me")))
            return
        chat = str(d["chat_jid"])
        self._debouncer.add(chat, mid, self._clock.now())
        row = await self._one("SELECT text FROM messages WHERE id = ?", (mid,))
        if row is not None and is_emergency(row[0], self._policy):
            ids = self._debouncer.flush(chat)
            if ids:
                await self.process(ids, mentions_me=bool(d.get("mentions_me")))

    async def flush_due(self) -> int:
        """Process bursts whose quiet window has passed. Call about once a second."""
        if self._debouncer is None:
            return 0
        bursts = self._debouncer.due(self._clock.now())
        for ids in bursts:
            await self.process(ids)
        return len(bursts)

    @property
    def pending(self) -> int:
        return self._debouncer.pending if self._debouncer else 0

    # ------------------------------------------------------------------ helpers

    async def _one(self, sql: str, args: tuple[object, ...]) -> aiosqlite.Row | None:
        async with self._conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def _count(self, sql: str, args: tuple[object, ...]) -> int:
        row = await self._one(sql, args)
        return int(row[0]) if row else 0

    async def _fast_replies(self, chat: str, window_s: int, limit: int) -> int:
        """How many of our latest auto replies to this chat were answered within `window_s`.

        Until M4 adds sending, the outbox is empty and this is always 0.
        """
        async with self._conn.execute(
            "SELECT not_before FROM outbox WHERE chat_jid = ? AND status = 'sent' "
            "AND level >= ? ORDER BY not_before DESC LIMIT ?",
            (chat, int(Level.AUTO_SAFE), limit),
        ) as cur:
            sent = [from_iso(r[0]) for r in await cur.fetchall()]
        fast = 0
        for t in sent:
            answered = await self._count(
                "SELECT COUNT(*) FROM messages WHERE chat_jid = ? AND direction = 'IN' "
                "AND ts > ? AND ts <= ?",
                (chat, to_iso(t), to_iso(t + timedelta(seconds=window_s))),
            )
            fast += answered > 0
        return fast

    def _local(self, dt: datetime) -> str:
        return dt.astimezone(self._tz).strftime("%H:%M")

    def _name(self, contact: aiosqlite.Row | None) -> str:
        if contact is None:
            return "Contact"
        return str(contact["display_name"] or contact["push_name"] or "Contact")

    def _mode_line(self, state: ModeState, mode: Mode) -> str:
        if mode in TIMED_MODES and state.until is not None:
            return f"{mode.value} until {self._local(state.until)}"
        return mode.value

    def _facts(self, mode: Mode, until: datetime | None, tier: str = "unknown") -> list[str]:
        """The only things the drafter may state about the user (about_me.yaml, by tier)."""
        user = self._policy.user.name
        facts = [f"I am FLINS, {user}'s assistant.", *self._about.for_tier(tier)]
        if mode in TIMED_MODES and until is not None:
            word = {"WORKING": "working", "FOCUS": "in deep focus", "AWAY": "away"}[mode.value]
            facts += [
                f"{user} is {word} until {self._local(until)}.",
                f"{user} will reply after {self._local(until)}.",
            ]
        return facts

    # ------------------------------------------------------------------ main

    async def process(
        self, message_ids: list[int], *, mentions_me: bool = False
    ) -> Decision | None:
        marks = ",".join("?" * len(message_ids))
        async with self._conn.execute(
            f"SELECT * FROM messages WHERE id IN ({marks}) AND direction = 'IN' ORDER BY ts, id",
            message_ids,
        ) as cur:
            msgs = list(await cur.fetchall())
        if not msgs:
            return None
        anchor = msgs[-1]
        now = self._clock.now()
        chat = anchor["chat_jid"]
        is_group = chat.endswith("@g.us")
        contact = None
        if not is_group:
            contact = await self._one(
                "SELECT * FROM contacts WHERE wa_jid = ? OR wa_lid = ?", (chat, chat)
            )
        tier = contact["tier"] if contact is not None else "unknown"
        texts = [m["text"] for m in msgs if m["text"]]
        joined = "\n".join(texts) or None
        msg_type = "text" if texts else anchor["type"]

        await self._modes.refresh()  # pick up `flins stop` etc. from other processes
        state = self._modes.state
        mode = effective_mode(state, now)
        loop = self._policy.hard_rules.loop_detection
        ctx = RuleContext(
            mode=mode,
            mode_started_at=state.started_at,
            tier=tier,
            message=MessageFacts(
                text=joined,
                msg_type=msg_type,
                is_group=is_group,
                chat_jid=chat,
                mentions_me=mentions_me,
                ts=from_iso(msgs[0]["ts"]),
            ),
            auto_sent_last_hour=await self._count(
                "SELECT COUNT(*) FROM outbox WHERE chat_jid = ? AND status = 'sent' "
                "AND level >= ? AND not_before >= ?",
                (chat, int(Level.AUTO_SAFE), to_iso(now - timedelta(hours=1))),
            ),
            identical_recent=await self._count(
                "SELECT COUNT(*) FROM messages WHERE chat_jid = ? AND direction = 'IN' "
                "AND text IS ? AND ts >= ?",
                (chat, anchor["text"], to_iso(from_iso(anchor["ts"]) - IDENTICAL_WINDOW)),
            )
            if anchor["text"]
            else 1,
            fast_replies=await self._fast_replies(
                chat, loop.fast_reply_seconds, loop.fast_reply_count
            ),
        )
        max_level = contact["max_level"] if contact is not None else None
        allowed = contact["allowed_categories"] if contact is not None else None
        base = DecisionInputs(
            mode=mode,
            tier=tier,
            hits=evaluate_hard_rules(ctx, self._policy),
            fallback_category=fallback_category(msg_type=msg_type, text=joined, is_group=is_group),
            profile=state.profile if mode == state.mode else None,
            contact_max_level=Level(max_level) if max_level is not None else None,
            allowed_categories=json.loads(allowed) if allowed else None,
        )
        notes: list[str] = []
        pre = decide(base, self._policy)

        # Only ask the LLM when the rules still allow more than "just tell the user". This
        # also keeps money/OTP/unknown-sender messages away from the API entirely.
        conversation: str | None = None
        triage: TriageResult | None = None
        llm_triage = False
        budget_ok = True
        if pre.cap_without("category", "template") > Level.NOTIFY:
            if self._triage_override is not None:
                triage = await self._triage_override(anchor, contact)
            elif self._brain is not None:
                budget_ok = await self._brain.gateway.budget_ok()
                if not budget_ok:
                    notes.append("daily LLM budget reached")
                else:
                    conversation = await self._conversation(msgs, contact)
                    try:
                        triage = await self._brain.triager.classify(
                            conversation=conversation,
                            mode_line=self._mode_line(state, mode),
                            contact_name=self._name(contact),
                            tier=tier,
                        )
                        llm_triage = True
                    except LLMError as exc:
                        notes.append(f"triage failed: {exc}")
        else:
            notes.append("LLM skipped: rules already cap at NOTIFY")

        decision = decide(replace(base, triage=triage, budget_ok=budget_ok), self._policy)
        if notes:
            decision = replace(decision, warnings=[*decision.warnings, *notes])

        outcome: DraftOutcome | None = None
        if decision.level >= Level.DRAFT:
            decision, outcome = await self._draft(
                decision, msgs, contact, state.until, mode, conversation, texts
            )
        auto = False
        if (
            outcome is not None
            and outcome.would_send
            and decision.level == Level.AUTO_SAFE
            and self._policy.auto_replies == "holding_only"
        ):
            since = await self._modes.session_started_at()
            if await holding_reply_sent_this_session(self._conn, chat, since):
                decision = decision.with_ceiling(
                    "holding_once", Level.NOTIFY, "holding reply already sent this session"
                )
                outcome = replace(outcome, would_send=False)
            else:
                auto = True
        await self._record(
            msgs, contact, mode.value, tier, decision, triage, llm_triage, outcome, now, auto
        )
        return decision

    # ------------------------------------------------------------------ drafting

    async def _conversation(self, msgs: list[aiosqlite.Row], contact: aiosqlite.Row | None) -> str:
        first = msgs[0]
        async with self._conn.execute(
            "SELECT * FROM messages WHERE chat_jid = ? AND ts < ? ORDER BY ts DESC LIMIT ?",
            (first["chat_jid"], first["ts"], THREAD_LINES),
        ) as cur:
            earlier = list(reversed(list(await cur.fetchall())))

        def lines(rows: list[aiosqlite.Row]) -> list[Line]:
            return [
                Line(from_iso(r["ts"]), r["direction"] == "OUT", r["text"], r["type"]) for r in rows
            ]

        return render_conversation(
            earlier=lines(earlier), new=lines(msgs), contact_name=self._name(contact), tz=self._tz
        )

    def _tone(self, contact: aiosqlite.Row | None, tier: str) -> dict[str, object]:
        tier_pol = self._policy.tiers.get(tier)
        name = (contact["tone_profile"] if contact is not None else None) or (
            tier_pol.tone if tier_pol else None
        )
        tone: dict[str, object] = self._tones.get(name).model_dump()
        if self._tones.style_samples:
            tone["examples_of_how_the_user_writes"] = " | ".join(self._tones.style_samples[:10])
        return tone

    async def _draft(
        self,
        decision: Decision,
        msgs: list[aiosqlite.Row],
        contact: aiosqlite.Row | None,
        until: datetime | None,
        mode: Mode,
        conversation: str | None,
        texts: list[str],
    ) -> tuple[Decision, DraftOutcome | None]:
        tier = contact["tier"] if contact is not None else "unknown"
        disclose = bool(contact["disclose_ai"]) if contact is not None else True
        never: list[str] = json.loads(contact["never_mention"]) if contact is not None else []
        tone = self._tone(contact, tier)
        max_chars = int(str(tone.get("max_chars", 160)))
        facts = self._facts(mode, until, tier)
        signature = self._policy.user.signature if disclose else ""

        # AUTO_SAFE: a fixed template. No LLM involved.
        if decision.level == Level.AUTO_SAFE and decision.template:
            template = decision.template
            first_name = self._name(contact).split()[0] if contact is not None else "there"
            text = (
                self._policy.templates[template]
                .format(
                    name=first_name,
                    user=self._policy.user.name,
                    until=self._local(until)
                    if until is not None and mode in TIMED_MODES
                    else "later",
                    signature=signature,
                )
                .strip()
            )
            guard = check(
                GuardInput(
                    reply=text,
                    level=Level.AUTO_SAFE,
                    inbound_texts=texts,
                    facts=[*facts, signature, first_name],
                    never_mention=never,
                    max_chars=max(max_chars, len(text)),
                    is_template=True,
                )
            )
            if not guard.ok:
                assert guard.cap is not None
                decision = decision.with_ceiling("guard", guard.cap, "template failed the guard")
            return decision, DraftOutcome(
                text, f"template:{template}", None, guard, decision.level >= Level.AUTO_SAFE
            )

        # AUTO or DRAFT: needs the LLM drafter.
        if self._brain is None:
            return decision.with_ceiling("draft", Level.NOTIFY, "no drafter available"), None
        if conversation is None:
            conversation = await self._conversation(msgs, contact)
        card = {
            "name": self._name(contact),
            "relationship": tier,
            "notes": contact["notes"] if contact is not None else None,
            "never_mention": ", ".join(never) if never else None,
        }
        try:
            out = await self._brain.drafter.draft(
                conversation=conversation,
                user_name=self._policy.user.name,
                contact_card=card,
                tone=tone,
                facts=facts,
            )
        except LLMError as exc:
            return decision.with_ceiling("draft", Level.NOTIFY, f"drafting failed: {exc}"), None
        if not out.reply.strip():
            note = f"drafter chose not to reply: {out.reason[:120]}"
            return decision.with_ceiling("draft", Level.NOTIFY, note), None

        d = self._policy.drafting
        guard = check(
            GuardInput(
                reply=out.reply,
                level=decision.level,
                inbound_texts=texts,
                facts=facts,
                never_mention=never,
                max_chars=max_chars,
                language=str(tone.get("language", "en")),
                confidence=out.confidence,
                makes_commitment=out.makes_commitment,
                mentions_facts_not_provided=out.mentions_facts_not_provided,
                min_confidence_auto=d.min_confidence_auto,
                min_confidence_draft=d.min_confidence_draft,
                allow_links=self._policy.limits.allow_links_in_auto,
                busy=mode in TIMED_MODES,
            )
        )
        if not guard.ok:
            assert guard.cap is not None
            decision = decision.with_ceiling("guard", guard.cap, "; ".join(guard.failures))

        # Anything that would go out unreviewed gets a second opinion.
        if decision.level == Level.AUTO and d.redflag_check:
            try:
                rf = await self._brain.redflag.check(conversation=conversation, reply=out.reply)
                if not rf.safe:
                    decision = decision.with_ceiling(
                        "redflag", Level.DRAFT, f"red flag: {rf.reason[:120]}"
                    )
            except LLMError as exc:
                decision = decision.with_ceiling(
                    "redflag", Level.DRAFT, f"red flag check failed: {exc}"
                )

        text = out.reply.strip()
        if decision.level == Level.AUTO and signature and signature not in text:
            text = f"{text} {signature}"
        return decision, DraftOutcome(
            text, "llm", out.confidence, guard, decision.level >= Level.AUTO_SAFE
        )

    # ------------------------------------------------------------------ recording

    async def _record(
        self,
        msgs: list[aiosqlite.Row],
        contact: aiosqlite.Row | None,
        mode: str,
        tier: str,
        decision: Decision,
        triage: TriageResult | None,
        llm_triage: bool,
        outcome: DraftOutcome | None,
        now: datetime,
        auto: bool = False,
    ) -> None:
        ts = to_iso(now)
        anchor = msgs[-1]
        cur = await self._conn.execute(
            "INSERT INTO bursts (chat_jid, first_ts, last_ts, status) "
            "VALUES (?, ?, ?, 'processed')",
            (anchor["chat_jid"], msgs[0]["ts"], anchor["ts"]),
        )
        burst_id = cur.lastrowid
        marks = ",".join("?" * len(msgs))
        await self._conn.execute(
            f"UPDATE messages SET burst_id = ? WHERE id IN ({marks})",
            (burst_id, *[m["id"] for m in msgs]),
        )
        cur = await self._conn.execute(
            "INSERT INTO decisions (burst_id, message_id, mode, contact_tier, hard_rule, triage, "
            "ceilings, final_level, action, category, flags, escalate, interrupt, prompt_version, "
            "model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                burst_id,
                anchor["id"],
                mode,
                tier,
                ",".join(decision.hard_rules) or None,
                triage.model_dump_json() if triage else None,
                json.dumps(decision.trace()["ceilings"]),
                int(decision.level),
                decision.action,
                decision.category,
                json.dumps(sorted(decision.flags)),
                int(decision.escalate),
                int(decision.interrupt),
                self._brain.triager.prompt_version if llm_triage and self._brain else None,
                "claude-haiku-5-5" if llm_triage else None,
                ts,
            ),
        )
        decision_id = cur.lastrowid
        approval_short_id: int | None = None
        if outcome is not None:
            cur = await self._conn.execute(
                "INSERT INTO drafts (decision_id, text, confidence, guard_results, source, "
                "would_send, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id,
                    outcome.text,
                    outcome.confidence,
                    json.dumps(outcome.guard.as_dict()),
                    outcome.source,
                    int(outcome.would_send),
                    ts,
                ),
            )
            assert cur.lastrowid is not None and decision_id is not None
            if not auto:
                _, approval_short_id = await create_for_draft(
                    self._conn,
                    draft_id=cur.lastrowid,
                    decision_id=decision_id,
                    now=now,
                    expiry=timedelta(minutes=self._policy.approvals.expiry_minutes),
                )
        await self._conn.commit()
        auto_queued = False
        if auto and outcome is not None and decision_id is not None:
            lo, hi = self._policy.limits.send_delay_seconds
            try:
                await enqueue(
                    self._conn,
                    kind="auto",
                    chat_jid=anchor["chat_jid"],
                    text=outcome.text,
                    level=int(decision.level),
                    now=now,
                    decision_id=decision_id,
                    delay=timedelta(seconds=self._rng.uniform(lo, hi)),
                )
                auto_queued = True
            except OutboxError as exc:
                await self._audit.record(
                    "pipeline", "auto_reply.skipped", {"decision_id": decision_id, "why": str(exc)}
                )
        trace = decision.trace()
        await self._audit.record(
            "pipeline",
            "decision",
            {
                "decision_id": decision_id,
                "message_ids": [m["id"] for m in msgs],
                "mode": mode,
                "tier": tier,
                **trace,
                "guard": outcome.guard.as_dict() if outcome else None,
                "would_send": bool(outcome and outcome.would_send),
            },
        )
        await self._bus.publish(
            "decision.made",
            {
                "decision_id": decision_id,
                "message_id": anchor["id"],
                "chat_jid": anchor["chat_jid"],
                "contact_name": self._name(contact) if contact is not None else None,
                "draft": outcome.text if outcome else None,
                "would_send": bool(outcome and outcome.would_send),
                "approval": approval_short_id,
                "auto_queued": auto_queued,
                **trace,
            },
        )
