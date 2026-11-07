"""Deterministic output guard (SPEC §8.3, CLAUDE.md invariant 6).

Checks a proposed reply before it can count as "would send". Nothing here calls an LLM. A
failure lowers the level by one step (AUTO -> DRAFT, DRAFT or AUTO_SAFE -> NOTIFY); serious
failures go straight to NOTIFY. The draft is kept and shown, never silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from flins.core.levels import Level

# Things a reply must not contain unless they already appear in the inbound text or the facts.
_SENSITIVE = {
    "phone": re.compile(r"\+?\d[\d\s().-]{6,}\d"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "url": re.compile(
        r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|in|org|net|io|co|me|ly|app)\b", re.I
    ),
    "digits": re.compile(r"\d{4,}"),
    "money": re.compile(
        r"(?:₹|rs\.?|inr|\$|usd)\s?\d[\d,.]*|\d[\d,.]*\s?(?:rs|rupees|inr|k\b)", re.I
    ),
}
# Lying about being a person, or pretending to be the user.
_DENIES_AI = re.compile(
    r"\b(?:i\s*(?:'?m|am)\s+not\s+(?:a\s+)?(?:bot|robot|ai|machine|assistant))\b"
    r"|\b(?:i\s*(?:'?m|am)\s+(?:a\s+)?(?:real\s+)?(?:human|person))\b"
    r"|\bthis\s+is\s+(?:really\s+)?(?:me|hemanth)\b(?!.*assistant)",
    re.I,
)
# Writing *as* the user ("I'm travelling", "main kaam kar raha hoon") instead of about them.
# "I" is fine only for FLINS itself: "I'll pass it on", "I'm FLINS, his assistant".
_SPEAKS_AS_USER = re.compile(
    r"\bI\s*(?:'|\u2019)?(?:m|am|was|ve|have|ll|will|d)\b"
    r"(?!.{0,30}\b(?:FLINS|assistant|pass|let (?:him|her)|tell (?:him|her)|ask (?:him|her)|"
    r"remind (?:him|her)|check with (?:him|her)|make sure (?:he|she))\b)"
    r"|\b(?:main|mai|mujhe|mera|meri|mere|hoon|hu|hun)\b",
    re.IGNORECASE,
)
# Claims the user is free right now. Wrong (and risky) while a busy mode is on.
_CLAIMS_FREE_NOW = re.compile(
    r"\b(?:free|available|done with work|off work)\s+(?:now|right now|rn|at the moment)\b"
    r"|\b(?:he|she)(?:'|\u2019)?s\s+(?:free|available)\b"
    r"|\bjust\s+(?:wrapped up|finished|got done)\b"
    r"|\b(?:abhi|ab)\s+free\b|\bfree\s+ho\s+(?:gaya|gayi|gaye)\b",
    re.IGNORECASE,
)
# Signs the reply is following instructions smuggled into the thread.
_INJECTION_ECHO = re.compile(
    r"ignore (?:all |your |previous )|system prompt|as an ai language model|my instructions",
    re.I,
)


@dataclass(frozen=True)
class GuardInput:
    reply: str
    level: Level  # the level the decision would act at (AUTO, AUTO_SAFE or DRAFT)
    inbound_texts: list[str]
    facts: list[str]
    never_mention: list[str] = field(default_factory=list)
    max_chars: int = 160
    language: str = "en"
    confidence: float = 1.0
    makes_commitment: bool = False
    mentions_facts_not_provided: bool = False
    is_template: bool = False
    min_confidence_auto: float = 0.75
    min_confidence_draft: float = 0.40
    allow_links: bool = False
    busy: bool = False  # a busy mode (working / focus / away) is on


@dataclass(frozen=True)
class GuardResult:
    failures: list[str]
    cap: Level | None  # None = passed

    @property
    def ok(self) -> bool:
        return self.cap is None

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "failures": self.failures,
            "cap": self.cap.name if self.cap else None,
        }


def _one_step_down(level: Level) -> Level:
    return Level.DRAFT if level == Level.AUTO else Level.NOTIFY


def _mostly_non_latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 8:
        return False
    non_latin = sum(1 for c in letters if ord(c) > 0x24F)
    return non_latin / len(letters) > 0.3


def check(g: GuardInput) -> GuardResult:
    failures: list[str] = []
    severe = False
    reply = g.reply.strip()
    source = " ".join([*g.inbound_texts, *g.facts]).lower()

    if not reply:
        return GuardResult(["empty_reply"], Level.NOTIFY)

    if not g.is_template:
        if g.confidence < g.min_confidence_draft:
            failures.append(f"confidence {g.confidence:.2f} too low to show as a draft")
            severe = True
        elif g.level >= Level.AUTO and g.confidence < g.min_confidence_auto:
            failures.append(f"confidence {g.confidence:.2f} too low to send unreviewed")
        if g.makes_commitment:
            failures.append("makes a commitment")
        if g.mentions_facts_not_provided:
            failures.append("states facts it wasn't given")

    for kind, rx in _SENSITIVE.items():
        if kind == "url" and g.allow_links:
            continue
        for match in rx.findall(reply):
            token = match.strip().lower()
            if token and token not in source:
                failures.append(f"contains {kind} not in the conversation or facts: {token[:30]!r}")
                break

    lowered = reply.lower()
    for term in g.never_mention:
        if term and term.lower() in lowered:
            failures.append(f"mentions a never-mention topic: {term!r}")

    if len(reply) > g.max_chars:
        failures.append(f"too long ({len(reply)} > {g.max_chars} characters)")

    if g.language.lower().startswith("en") and _mostly_non_latin(reply):
        failures.append("not in the contact's language")

    if _DENIES_AI.search(reply):
        failures.append("claims to be a person or the user")
        severe = True

    if g.busy and _CLAIMS_FREE_NOW.search(reply):
        failures.append("says the user is free, but a busy mode is on")
        severe = True

    if _SPEAKS_AS_USER.search(reply):  # FLINS speaks about the user, never as the user
        failures.append("writes as if it were the user (should say he/she)")

    if _INJECTION_ECHO.search(reply):
        failures.append("echoes instructions from the thread")
        severe = True

    if not failures:
        return GuardResult([], None)
    return GuardResult(failures, Level.NOTIFY if severe else _one_step_down(g.level))
