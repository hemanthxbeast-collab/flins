"""Run every tests/replay/*.jsonl script through the real ingest, pipeline and mode code."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from flins.core.policy_schema import Policy
from flins.replay import ReplayFailure, load_script, run_script
from tests.conftest import REPO_ROOT

SCRIPTS = sorted((REPO_ROOT / "tests" / "replay").glob("*.jsonl"))


@pytest.mark.parametrize("script", SCRIPTS, ids=[s.stem for s in SCRIPTS])
async def test_replay(script: Path, tmp_path: Path, example_policy_dict: dict[str, Any]) -> None:
    policy = Policy.model_validate(example_policy_dict)
    report = await run_script(load_script(script), tmp_path / "replay.db", policy)
    assert report.expectations > 0


async def test_replay_reports_the_failing_line(
    tmp_path: Path, example_policy_dict: dict[str, Any]
) -> None:
    policy = Policy.model_validate(example_policy_dict)
    steps = [
        {"start": "2026-11-09T10:00:00+05:30"},
        {"say": "focus mode"},
        {"expect": "mode", "mode": "WORKING"},
    ]
    with pytest.raises(ReplayFailure, match="line 3: mode is FOCUS"):
        await run_script(steps, tmp_path / "r.db", policy)


def test_there_are_replay_scripts() -> None:
    assert len(SCRIPTS) >= 2
