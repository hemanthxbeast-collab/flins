from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from flins.core.levels import Level, Mode, min_level
from flins.core.policy_schema import (
    HARD_NOTIFY_CATEGORIES,
    HARD_NOTIFY_FLAGS,
    Policy,
    parse_duration,
)
from tests.conftest import EXAMPLE_POLICY


def test_example_policy_loads_without_clamps(example_policy_dict: dict[str, Any]) -> None:
    policy = Policy.model_validate(example_policy_dict)
    assert policy.warnings == []
    assert policy.modes[Mode.WORKING].ceiling == Level.AUTO
    assert policy.modes[Mode.WORKING].default_duration == timedelta(hours=3)
    assert policy.categories["greeting_smalltalk"].ceiling == Level.AUTO
    assert policy.categories["urgent_emergency"].escalate is True


def test_example_file_is_dry_run() -> None:
    assert "dry_run: true" in EXAMPLE_POLICY.read_text(encoding="utf-8")


def test_dry_run_defaults_true(example_policy_dict: dict[str, Any]) -> None:
    del example_policy_dict["dry_run"]
    assert Policy.model_validate(example_policy_dict).dry_run is True


@pytest.mark.parametrize("category", sorted(HARD_NOTIFY_CATEGORIES))
def test_hard_categories_clamped_to_notify(
    example_policy_dict: dict[str, Any], category: str
) -> None:
    example_policy_dict["categories"][category] = "AUTO"
    policy = Policy.model_validate(example_policy_dict)
    assert policy.categories[category].ceiling == Level.NOTIFY
    assert any(category in w for w in policy.warnings)


@pytest.mark.parametrize("category", sorted(HARD_NOTIFY_CATEGORIES))
def test_missing_hard_category_added_at_notify(
    example_policy_dict: dict[str, Any], category: str
) -> None:
    del example_policy_dict["categories"][category]
    policy = Policy.model_validate(example_policy_dict)
    assert policy.categories[category].ceiling == Level.NOTIFY


@pytest.mark.parametrize("flag", sorted(HARD_NOTIFY_FLAGS))
def test_hard_flags_clamped_to_notify(example_policy_dict: dict[str, Any], flag: str) -> None:
    example_policy_dict["sensitivity_caps"][flag] = "AUTO"
    policy = Policy.model_validate(example_policy_dict)
    assert policy.sensitivity_caps[flag] == Level.NOTIFY


def test_paused_clamped_to_ignore(example_policy_dict: dict[str, Any]) -> None:
    example_policy_dict["modes"]["PAUSED"]["ceiling"] = "AUTO"
    policy = Policy.model_validate(example_policy_dict)
    assert policy.modes[Mode.PAUSED].ceiling == Level.IGNORE


@pytest.mark.parametrize("mode", ["WORKING", "FOCUS", "AWAY"])
@pytest.mark.parametrize("duration", [None, "0m"])
def test_timed_modes_must_expire(
    example_policy_dict: dict[str, Any], mode: str, duration: str | None
) -> None:
    data = copy.deepcopy(example_policy_dict)
    if duration is None:
        del data["modes"][mode]["default_duration"]
    else:
        data["modes"][mode]["default_duration"] = duration
    with pytest.raises(ValidationError, match="default_duration"):
        Policy.model_validate(data)


def test_missing_mode_rejected(example_policy_dict: dict[str, Any]) -> None:
    del example_policy_dict["modes"]["FOCUS"]
    with pytest.raises(ValidationError, match="FOCUS"):
        Policy.model_validate(example_policy_dict)


def test_unknown_level_rejected(example_policy_dict: dict[str, Any]) -> None:
    example_policy_dict["categories"]["scheduling"] = "SUPER_AUTO"
    with pytest.raises(ValidationError, match="unknown level"):
        Policy.model_validate(example_policy_dict)


def test_unknown_key_rejected(example_policy_dict: dict[str, Any]) -> None:
    example_policy_dict["limits"]["per_contact_max_auto_per_hr"] = 5  # typo
    with pytest.raises(ValidationError):
        Policy.model_validate(example_policy_dict)


def test_unknown_template_rejected(example_policy_dict: dict[str, Any]) -> None:
    example_policy_dict["modes"]["WORKING"]["holding_template"] = "nope"
    with pytest.raises(ValidationError, match="unknown template"):
        Policy.model_validate(example_policy_dict)


def test_bad_regex_rejected(example_policy_dict: dict[str, Any]) -> None:
    example_policy_dict["hard_rules"]["money_patterns"].append("(unclosed")
    with pytest.raises(ValidationError, match="invalid regex"):
        Policy.model_validate(example_policy_dict)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3h", timedelta(hours=3)),
        ("90m", timedelta(minutes=90)),
        ("1h30m", timedelta(minutes=90)),
        (45, timedelta(minutes=45)),
    ],
)
def test_parse_duration(raw: Any, expected: timedelta) -> None:
    assert parse_duration(raw) == expected


@pytest.mark.parametrize("raw", ["", "soon", "3 days", "h"])
def test_parse_duration_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(raw)


def test_level_parse_and_min() -> None:
    assert Level.parse("auto_safe") == Level.AUTO_SAFE
    assert min_level(Level.AUTO, Level.DRAFT, Level.AUTO_SAFE) == Level.DRAFT
    assert min_level() == Level.IGNORE
