from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_POLICY = REPO_ROOT / "config" / "policy.example.yaml"


@pytest.fixture
def example_policy_dict() -> dict[str, Any]:
    with EXAMPLE_POLICY.open(encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway FLINS project root using the example configs."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='flins-test'\n", encoding="utf-8")
    shutil.copytree(
        REPO_ROOT / "config",
        tmp_path / "config",
        ignore=shutil.ignore_patterns(
            "flins.yaml", "policy.yaml", "contacts.yaml", "tone_profiles.yaml", "about_me.yaml"
        ),
    )
    shutil.copytree(REPO_ROOT / "prompts", tmp_path / "prompts")
    return tmp_path
