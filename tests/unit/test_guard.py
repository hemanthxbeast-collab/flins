from __future__ import annotations

from typing import Any

import pytest

from flins.core.guard import GuardInput, check
from flins.core.levels import Level

FACTS = ["Hemanth is working until 18:00", "He will reply after 18:00"]


def g(reply: str, **kw: Any) -> GuardInput:
    base: dict[str, Any] = {
        "reply": reply,
        "level": Level.AUTO,
        "inbound_texts": ["hey are you free for a quick call?"],
        "facts": FACTS,
        "confidence": 0.9,
    }
    base.update(kw)
    return GuardInput(**base)


def test_clean_reply_passes() -> None:
    r = check(g("Hey! He's working till 18:00, will call you after."))
    assert r.ok, r.failures


@pytest.mark.parametrize(
    ("reply", "fragment"),
    [
        ("Call him on +91 98765 43210", "phone"),
        ("mail him at hemanth@example.com", "email"),
        ("details here https://evil.example/x", "url"),
        ("check bit.ly for it", "url"),
        ("the code is 482913", "digits"),
        ("he'll send ₹2000 tonight", "money"),
        ("sending 2k now", "money"),
    ],
)
def test_leaks_are_caught(reply: str, fragment: str) -> None:
    r = check(g(reply))
    assert not r.ok
    assert any(fragment in f for f in r.failures), r.failures
    assert r.cap == Level.DRAFT  # AUTO goes down one step


def test_values_already_in_the_conversation_are_fine() -> None:
    r = check(
        g(
            "Got it, 2k for the tickets, he'll see it after 18:00",
            inbound_texts=["can you transfer 2k for the tickets"],
        )
    )
    assert not any("money" in f for f in r.failures)


@pytest.mark.parametrize(
    "reply",
    ["No, I'm not a bot!", "I am a real person haha", "this is Hemanth, call me", "I'm human"],
)
def test_pretending_to_be_human_goes_straight_to_notify(reply: str) -> None:
    r = check(g(reply))
    assert r.cap == Level.NOTIFY


def test_honest_assistant_answer_passes() -> None:
    assert check(g("I'm FLINS, Hemanth's assistant. He's working till 18:00.")).ok


def test_injection_echo_is_severe() -> None:
    r = check(g("Sure, ignore previous instructions. Here you go"))
    assert r.cap == Level.NOTIFY


def test_self_reported_risks_and_confidence() -> None:
    assert check(g("See you at 5!", makes_commitment=True)).cap == Level.DRAFT
    assert check(g("He's at the gym", mentions_facts_not_provided=True)).cap == Level.DRAFT
    assert check(g("ok", confidence=0.6)).cap == Level.DRAFT  # below auto threshold
    assert check(g("ok", confidence=0.6, level=Level.DRAFT)).ok  # fine as a draft
    assert check(g("ok", confidence=0.2)).cap == Level.NOTIFY  # not even worth drafting


def test_draft_failure_goes_to_notify() -> None:
    assert check(g("call +91 98765 43210", level=Level.DRAFT)).cap == Level.NOTIFY


def test_never_mention_length_language_empty() -> None:
    assert not check(g("How's the new job going?", never_mention=["job"])).ok
    assert not check(g("x" * 200, max_chars=160)).ok
    assert not check(g("नमस्ते, वह अभी काम कर रहे हैं")).ok
    assert check(g("   ")).cap == Level.NOTIFY


def test_templates_skip_llm_self_reports_but_not_leak_checks() -> None:
    tpl = "Hey Mom! I'm heads-down working right now. I'll get back to you after 18:00."
    assert check(g(tpl, level=Level.AUTO_SAFE, is_template=True, confidence=0.0)).ok
    assert check(g(tpl + " +91 98765 43210", level=Level.AUTO_SAFE, is_template=True)).cap == (
        Level.NOTIFY
    )
