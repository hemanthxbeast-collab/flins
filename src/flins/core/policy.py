"""Hard rules and the decide() function (SPEC §6.1, §7.3). Pure Python, no I/O, no LLM.

`decide()` computes the final autonomy level as the minimum of every ceiling that applies and
records each one by name, so every decision can be explained afterwards (CLAUDE.md invariants
1, 7 and 10).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import lru_cache
from typing import Any

from flins.core.levels import Level, Mode, min_level
from flins.core.policy_schema import HARD_NOTIFY_CATEGORIES, HARD_NOTIFY_FLAGS, TIMED_MODES, Policy
from flins.core.triage import TriageResult, fallback_category

# Always checked, even if someone empties the lists in policy.yaml (defence in depth).
BUILTIN_CREDENTIAL = [
    r"\botp\b",
    r"verification code",
    r"\bpassword\b",
    r"\bpasscode\b",
    r"\bpin\b",
    r"\bcvv\b",
    r"\b2fa\b",
    r"security code",
    r"login code",
]
BUILTIN_MONEY = [
    r"₹\s?\d",
    r"\brs\.?\s?\d",
    r"\binr\b",
    r"\bupi\b",
    r"\bgpay\b",
    r"\bphonepe\b",
    r"\bpaytm\b",
    r"send money",
    r"transfer",
    r"\bloan\b",
    r"\bpaise\b",
    r"\bpaisa\b",
    r"\bpayment\b",
    r"bank account",
    r"\bifsc\b",
]
BUILTIN_EMERGENCY = ["emergency", "accident", "hospital", "ambulance", "urgent"]
# Health and legal matters (invariant 7: NOTIFY only). Deterministic, so a fooled or confused
# AI triage can't miss them. Specific phrases only: "report" or "scan" alone are too common.
BUILTIN_SENSITIVE = [
    r"\bdoctors?\b",
    r"\bdr\.?\s",
    r"\bclinic\b",
    r"\bmedicines?\b",
    r"\bmedication\b",
    r"\bprescription\b",
    r"\btablets?\b",
    r"\bfever\b",
    r"\bsurgery\b",
    r"\bsymptoms?\b",
    r"\bdiagnos\w*",
    r"\bbiopsy\b",
    r"\bmri\b",
    r"\bct scan\b",
    r"\bx-?ray\b",
    r"\bpregnan\w*",
    r"\bblood (?:test|report|sugar|pressure)\b",
    r"\b(?:test|lab|medical|scan|blood) (?:results?|reports?)\b",
    r"\b(?:results?|reports?) (?:came|have come|are|is) (?:back|ready|out|in)\b",
    r"\bdawai\b",
    r"\bdavai\b",
    r"\bbimar\w*",
    r"\btabiyat\b",
    r"\blawyers?\b",
    r"\badvocate\b",
    r"\bcourt\b",
    r"\bpolice\b",
    r"\bfir\b",
    r"\blegal notice\b",
    r"\bsummons\b",
    r"\blawsuit\b",
]

ACTIONS: dict[Level, str] = {
    Level.IGNORE: "ignore",
    Level.NOTIFY: "notify",
    Level.DRAFT: "draft",
    Level.AUTO_SAFE: "auto_safe",
    Level.AUTO: "auto",
}


# ------------------------------------------------------------------ inputs and outputs


@dataclass(frozen=True)
class MessageFacts:
    text: str | None
    msg_type: str
    is_group: bool
    chat_jid: str
    mentions_me: bool
    ts: datetime


@dataclass(frozen=True)
class RuleContext:
    mode: Mode  # the effective mode (expired timed mode = NORMAL)
    mode_started_at: datetime
    tier: str
    message: MessageFacts
    auto_sent_last_hour: int = 0  # auto replies already sent to this contact in the last hour
    identical_recent: int = 1  # same text from this chat in the last 10 min, this one included
    fast_replies: int = 0  # how many of our last auto replies got an answer within seconds


@dataclass(frozen=True)
class RuleHit:
    rule: str
    reason: str
    cap: Level | None = None
    flags: frozenset[str] = frozenset()
    escalate: bool = False
    category: str | None = None


@dataclass(frozen=True)
class DecisionInputs:
    mode: Mode
    tier: str
    hits: list[RuleHit]
    fallback_category: str = "unknown"
    profile: str | None = None
    contact_max_level: Level | None = None
    allowed_categories: list[str] | None = None
    triage: TriageResult | None = None
    skill_ceiling: Level | None = None
    budget_ok: bool = True


@dataclass(frozen=True)
class Decision:
    level: Level
    action: str
    category: str
    flags: frozenset[str]
    escalate: bool
    interrupt: bool
    urgency: int
    template: str | None
    ceilings: dict[str, Level]
    hard_rules: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def with_ceiling(self, name: str, level: Level, note: str | None = None) -> Decision:
        """Add a later ceiling (draft failed, guard, red flag). Can only lower the level."""
        ceilings = {**self.ceilings, name: min(level, self.ceilings.get(name, level))}
        new_level = min_level(*ceilings.values())
        return replace(
            self,
            level=new_level,
            action=ACTIONS[new_level],
            ceilings=ceilings,
            template=self.template if new_level == Level.AUTO_SAFE else None,
            warnings=[*self.warnings, note] if note else self.warnings,
        )

    def cap_without(self, *names: str) -> Level:
        """The level all ceilings except `names` allow."""
        return min_level(*(v for k, v in self.ceilings.items() if k not in names))

    def trace(self) -> dict[str, Any]:
        """JSON-ready explanation, stored with every decision."""
        return {
            "level": self.level.name,
            "action": self.action,
            "category": self.category,
            "flags": sorted(self.flags),
            "escalate": self.escalate,
            "interrupt": self.interrupt,
            "urgency": self.urgency,
            "template": self.template,
            "ceilings": {k: v.name for k, v in self.ceilings.items()},
            "hard_rules": self.hard_rules,
            "warnings": self.warnings,
        }


# ------------------------------------------------------------------ hard rules


@lru_cache(maxsize=64)
def _compile(patterns: tuple[str, ...]) -> re.Pattern[str] | None:
    if not patterns:
        return None
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


def _keyword_rx(words: tuple[str, ...]) -> re.Pattern[str] | None:
    return _compile(tuple(rf"\b{re.escape(w)}\b" for w in words))


def is_emergency(text: str | None, policy: Policy) -> bool:
    """Quick check used to skip the debounce wait for urgent messages."""
    rx = _keyword_rx(tuple(dict.fromkeys(BUILTIN_EMERGENCY + policy.hard_rules.emergency_keywords)))
    return bool(text and rx and rx.search(text))


def evaluate_hard_rules(ctx: RuleContext, policy: Policy) -> list[RuleHit]:
    """Every rule is checked. The caller caps at the lowest hit and unions the flags."""
    hits: list[RuleHit] = []
    m = ctx.message
    text = m.text or ""
    hr = policy.hard_rules

    if ctx.mode == Mode.PAUSED:
        hits.append(RuleHit("paused", "FLINS is paused", cap=Level.IGNORE))

    if ctx.tier == "blocked":
        hits.append(RuleHit("blocked", "contact is blocked", cap=Level.IGNORE))

    if m.is_group:
        if m.chat_jid in hr.groups.allowlist and m.mentions_me:
            hits.append(
                RuleHit(
                    "group",
                    "mentioned in an allowed group",
                    cap=Level.NOTIFY,
                    category="group_mention",
                )
            )
        else:
            hits.append(
                RuleHit("group", "group message", cap=Level.IGNORE, category="group_message")
            )

    # A voice note or photo without text: FLINS can't hear or see it, so there is nothing safe
    # to reply. This is a structural fact, so triage can't talk it into "small talk".
    if m.msg_type != "text" and not text.strip():
        hits.append(
            RuleHit(
                "media_only",
                "voice note or media without text",
                cap=Level.NOTIFY,
                category="media_only",
            )
        )

    cred = _compile(tuple(BUILTIN_CREDENTIAL + hr.credential_patterns))
    if text and cred and cred.search(text):
        hits.append(
            RuleHit(
                "credentials",
                "asks about a code or password",
                cap=Level.NOTIFY,
                flags=frozenset({"credentials"}),
                category="credentials_otp",
            )
        )

    money = _compile(tuple(BUILTIN_MONEY + hr.money_patterns))
    if text and money and money.search(text):
        hits.append(
            RuleHit(
                "money",
                "mentions money or payment",
                cap=Level.NOTIFY,
                flags=frozenset({"money"}),
                category="money_payment",
            )
        )

    sensitive = _compile(tuple(BUILTIN_SENSITIVE + hr.sensitive_patterns))
    if text and sensitive and sensitive.search(text):
        hits.append(
            RuleHit(
                "legal_medical",
                "about health or a legal matter",
                cap=Level.NOTIFY,
                flags=frozenset({"legal_medical"}),
                category="personal_sensitive",
            )
        )

    emergency = _keyword_rx(tuple(dict.fromkeys(BUILTIN_EMERGENCY + hr.emergency_keywords)))
    if text and emergency and emergency.search(text):
        hits.append(
            RuleHit("emergency", "sounds urgent", escalate=True, category="urgent_emergency")
        )

    if ctx.auto_sent_last_hour >= policy.limits.per_contact_max_auto_per_hour:
        hits.append(
            RuleHit("contact_rate", "auto-reply limit for this contact reached", cap=Level.DRAFT)
        )

    loop = hr.loop_detection
    if (
        ctx.identical_recent >= loop.identical_msg_count
        or ctx.fast_replies >= loop.fast_reply_count
    ):
        hits.append(
            RuleHit(
                "loop",
                "looks like a bot or a reply loop",
                cap=Level.NOTIFY,
                flags=frozenset({"loop"}),
            )
        )

    backlog = policy.modes[ctx.mode].backlog if ctx.mode in policy.modes else "ignore_older"
    if ctx.mode in TIMED_MODES and backlog == "ignore_older" and m.ts < ctx.mode_started_at:
        hits.append(RuleHit("backlog", "arrived before this mode started", cap=Level.NOTIFY))

    return hits


# ------------------------------------------------------------------ decide


def _category_ceiling(policy: Policy, category: str) -> Level:
    pol = policy.categories.get(category) or policy.categories.get("unknown")
    level = pol.ceiling if pol else Level.DRAFT
    if category in HARD_NOTIFY_CATEGORIES:
        level = min(level, Level.NOTIFY)  # invariant 7, enforced here as well as in the schema
    return level


def _pick_category(policy: Policy, candidates: list[str]) -> str:
    """Of all categories that apply, take the one with the lowest ceiling (most cautious)."""
    known = [c for c in candidates if c in policy.categories] or ["unknown"]
    return min(known, key=lambda c: (_category_ceiling(policy, c), known.index(c)))


def decide(inp: DecisionInputs, policy: Policy) -> Decision:
    warnings: list[str] = []
    ceilings: dict[str, Level] = {}

    # Mode (with profile overlay). PAUSED is always IGNORE.
    mode_pol = policy.modes[inp.mode]
    ceilings["mode"] = Level.IGNORE if inp.mode == Mode.PAUSED else mode_pol.ceiling
    profile = policy.profiles.get(inp.profile) if inp.profile else None
    if profile is not None and profile.base == inp.mode and profile.ceiling is not None:
        ceilings["profile"] = profile.ceiling

    # Contact tier and per-contact limits.
    tier_pol = policy.tiers.get(inp.tier)
    tier_ceiling = (
        tier_pol.ceiling if tier_pol else (Level.IGNORE if inp.tier == "blocked" else Level.NOTIFY)
    )
    if profile is not None and profile.base == inp.mode and inp.tier in profile.tiers:
        tier_ceiling = profile.tiers[inp.tier]
    ceilings["tier"] = tier_ceiling
    if inp.contact_max_level is not None:
        ceilings["contact"] = inp.contact_max_level

    # Category: the most cautious of hard-rule, triage and structural categories.
    candidates = [h.category for h in inp.hits if h.category]
    if inp.triage is not None:
        if inp.triage.category not in policy.categories:
            warnings.append(f"triage category {inp.triage.category!r} unknown; using 'unknown'")
            candidates.append("unknown")
        else:
            candidates.append(inp.triage.category)
    if not candidates:
        candidates.append(inp.fallback_category)
    category = _pick_category(policy, candidates)
    ceilings["category"] = _category_ceiling(policy, category)

    if inp.allowed_categories is not None and category not in inp.allowed_categories:
        ceilings["allowed_categories"] = Level.DRAFT

    # Sensitivity flags from hard rules and triage.
    flags: frozenset[str] = frozenset().union(*(h.flags for h in inp.hits))
    if inp.triage is not None:
        flags |= inp.triage.all_flags()
    if flags:
        caps = []
        for f in flags:
            cap = policy.sensitivity_caps.get(f, Level.NOTIFY)
            if f in HARD_NOTIFY_FLAGS:
                cap = min(cap, Level.NOTIFY)  # invariant 7
            caps.append(cap)
        ceilings["sensitivity"] = min(caps)

    # Triage, skills and budget can only lower.
    if inp.triage is not None and (t := inp.triage.ceiling()) is not None:
        ceilings["triage"] = t
    if inp.skill_ceiling is not None:
        ceilings["skill"] = inp.skill_ceiling
    if not inp.budget_ok:
        ceilings["budget"] = Level.NOTIFY

    hit_caps = [h.cap for h in inp.hits if h.cap is not None]
    if hit_caps:
        ceilings["hard_rules"] = min(hit_caps)

    level = min_level(*ceilings.values())

    # AUTO_SAFE means "send a pre-written template". No template → no auto reply at all.
    template: str | None = None
    if level == Level.AUTO_SAFE:
        cat_pol = policy.categories.get(category)
        template = (cat_pol.template if cat_pol else None) or mode_pol.holding_template
        if (
            tier_pol is not None
            and not tier_pol.holding_reply
            and not (cat_pol and cat_pol.template)
        ):
            template = None
        if template is None or template not in policy.templates:
            ceilings["template"] = Level.NOTIFY
            level = min_level(*ceilings.values())
            template = None

    escalate = any(h.escalate for h in inp.hits)
    cat_pol = policy.categories.get(category)
    escalate |= bool(cat_pol and cat_pol.escalate)
    urgency = max(inp.triage.urgency if inp.triage else 0, 3 if escalate else 0)
    interrupt = escalate or urgency >= mode_pol.interrupt_threshold

    return Decision(
        level=level,
        action=ACTIONS[level],
        category=category,
        flags=flags,
        escalate=escalate,
        interrupt=interrupt,
        urgency=urgency,
        template=template,
        ceilings=ceilings,
        hard_rules=[h.rule for h in inp.hits],
        warnings=warnings,
    )


def decide_message(
    ctx: RuleContext,
    policy: Policy,
    *,
    profile: str | None = None,
    contact_max_level: Level | None = None,
    allowed_categories: list[str] | None = None,
    triage: TriageResult | None = None,
    budget_ok: bool = True,
) -> Decision:
    """Convenience: hard rules + decide in one call."""
    hits = evaluate_hard_rules(ctx, policy)
    m = ctx.message
    return decide(
        DecisionInputs(
            mode=ctx.mode,
            tier=ctx.tier,
            hits=hits,
            fallback_category=fallback_category(
                msg_type=m.msg_type, text=m.text, is_group=m.is_group
            ),
            profile=profile,
            contact_max_level=contact_max_level,
            allowed_categories=allowed_categories,
            triage=triage,
            budget_ok=budget_ok,
        ),
        policy,
    )
