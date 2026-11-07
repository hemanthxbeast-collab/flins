from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from flins.core.levels import Level, Mode
from flins.core.policy import (
    DecisionInputs,
    MessageFacts,
    RuleContext,
    RuleHit,
    decide,
    decide_message,
)
from flins.core.policy_schema import HARD_NOTIFY_CATEGORIES, Policy
from flins.core.triage import TriageResult
from tests.conftest import REPO_ROOT

T0 = datetime(2026, 11, 9, 4, 30, tzinfo=UTC)
GROUP = "120363000000000000@g.us"
CASES = [
    json.loads(line)
    for line in (REPO_ROOT / "tests" / "fixtures" / "hard_rule_cases.jsonl")
    .read_text(encoding="utf-8")
    .splitlines()
    if line.strip()
]


@pytest.fixture
def policy(example_policy_dict: dict[str, Any]) -> Policy:
    return Policy.model_validate(example_policy_dict)


def run_case(case: dict[str, Any], policy: Policy) -> Any:
    if case.get("allowlist"):
        policy.hard_rules.groups.allowlist.append(GROUP)
    is_group = case.get("is_group", False)
    ts = T0 + (timedelta(minutes=-1) if case.get("before_mode_start") else timedelta(minutes=1))
    ctx = RuleContext(
        mode=Mode(case.get("mode", "WORKING")),
        mode_started_at=T0,
        tier=case.get("tier", "trusted"),
        message=MessageFacts(
            text=case.get("text"),
            msg_type=case.get("type", "text"),
            is_group=is_group,
            chat_jid=GROUP if is_group else "1@s.whatsapp.net",
            mentions_me=case.get("mentions_me", False),
            ts=ts,
        ),
        auto_sent_last_hour=case.get("auto_sent_last_hour", 0),
        identical_recent=case.get("identical_recent", 1),
        fast_replies=case.get("fast_replies", 0),
    )
    triage = TriageResult(**case["triage"]) if "triage" in case else None
    return decide_message(ctx, policy, triage=triage)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_hard_rule_case(case: dict[str, Any], policy: Policy) -> None:
    d = run_case(case, policy)
    exp = case["expect"]
    if "level" in exp:
        assert d.level.name == exp["level"], d.trace()
    if "rules" in exp:
        assert sorted(d.hard_rules) == sorted(exp["rules"]), d.trace()
    if "category" in exp:
        assert d.category == exp["category"], d.trace()
    if "flags" in exp:
        assert sorted(d.flags) == sorted(exp["flags"]), d.trace()
    for key in ("escalate", "interrupt", "template"):
        if key in exp:
            assert getattr(d, key) == exp[key], d.trace()


def test_there_are_enough_cases() -> None:
    assert len(CASES) >= 40


# ---------------------------------------------------------------- invariants


def test_invariant7_holds_even_if_config_is_tampered(policy: Policy) -> None:
    """Even if a hard-NOTIFY category somehow got AUTO after loading, decide() clamps it."""
    for cat in HARD_NOTIFY_CATEGORIES:
        policy.categories[cat].ceiling = Level.AUTO
    policy.sensitivity_caps["money"] = Level.AUTO
    for cat in HARD_NOTIFY_CATEGORIES:
        d = decide(
            DecisionInputs(
                mode=Mode.WORKING, tier="trusted", hits=[], triage=TriageResult(category=cat)
            ),
            policy,
        )
        assert d.level <= Level.NOTIFY, (cat, d.trace())
    d = decide(
        DecisionInputs(
            mode=Mode.WORKING,
            tier="trusted",
            hits=[RuleHit("money", "x", flags=frozenset({"money"}))],
            triage=TriageResult(category="greeting_smalltalk"),
        ),
        policy,
    )
    assert d.level <= Level.NOTIFY


@pytest.mark.parametrize(
    "category",
    [
        "greeting_smalltalk",
        "availability_query",
        "scheduling",
        "money_payment",
        "urgent_emergency",
        "unknown",
    ],
)
@pytest.mark.parametrize("tier", ["inner", "trusted", "known", "unknown", "blocked"])
@pytest.mark.parametrize("mode", list(Mode))
def test_triage_can_only_lower(policy: Policy, category: str, tier: str, mode: Mode) -> None:
    """Invariant 1: adding triage never raises the level above the no-triage ceilings."""
    base = decide(DecisionInputs(mode=mode, tier=tier, hits=[]), policy)
    without_cat = min(c for k, c in base.ceilings.items() if k != "category")
    with_triage = decide(
        DecisionInputs(
            mode=mode,
            tier=tier,
            hits=[],
            triage=TriageResult(category=category, needs_human=True, urgency=3),
        ),
        policy,
    )
    assert with_triage.level <= without_cat
    assert with_triage.level <= Level.DRAFT  # needs_human caps at DRAFT


def test_auto_safe_without_template_falls_to_notify(policy: Policy) -> None:
    policy.modes[Mode.WORKING].holding_template = None
    d = decide(
        DecisionInputs(
            mode=Mode.WORKING,
            tier="inner",
            hits=[],
            triage=TriageResult(category="greeting_smalltalk"),
        ),
        policy,
    )
    assert d.level == Level.NOTIFY
    assert d.ceilings["template"] == Level.NOTIFY


def test_profile_overlays(policy: Policy) -> None:
    triage = TriageResult(category="greeting_smalltalk")
    strict = decide(
        DecisionInputs(
            mode=Mode.WORKING, tier="trusted", hits=[], triage=triage, profile="working-strict"
        ),
        policy,
    )
    assert strict.level == Level.AUTO_SAFE
    open_ = decide(
        DecisionInputs(
            mode=Mode.WORKING, tier="known", hits=[], triage=triage, profile="working-open"
        ),
        policy,
    )
    assert open_.level == Level.AUTO_SAFE
    # A profile for another mode has no effect.
    other = decide(
        DecisionInputs(
            mode=Mode.FOCUS, tier="known", hits=[], triage=triage, profile="working-open"
        ),
        policy,
    )
    assert other.level == Level.DRAFT


def test_contact_limits_and_budget(policy: Policy) -> None:
    triage = TriageResult(category="greeting_smalltalk")
    d = decide(
        DecisionInputs(
            mode=Mode.WORKING, tier="trusted", hits=[], triage=triage, contact_max_level=Level.DRAFT
        ),
        policy,
    )
    assert d.level == Level.DRAFT
    d = decide(
        DecisionInputs(
            mode=Mode.WORKING,
            tier="trusted",
            hits=[],
            triage=triage,
            allowed_categories=["scheduling"],
        ),
        policy,
    )
    assert d.level == Level.DRAFT
    d = decide(
        DecisionInputs(mode=Mode.WORKING, tier="trusted", hits=[], triage=triage, budget_ok=False),
        policy,
    )
    assert d.level == Level.NOTIFY


def test_trace_names_every_ceiling(policy: Policy) -> None:
    d = decide(
        DecisionInputs(
            mode=Mode.WORKING,
            tier="trusted",
            hits=[],
            triage=TriageResult(category="greeting_smalltalk"),
        ),
        policy,
    )
    trace = d.trace()
    assert set(trace["ceilings"]) >= {"mode", "tier", "category"}
    assert trace["level"] == "AUTO"
    json.dumps(trace)  # must be JSON-serialisable for the audit log
