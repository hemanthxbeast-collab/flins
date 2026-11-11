from __future__ import annotations

from pathlib import Path

import pytest

from flins.cli import STUBS, main
from tests.conftest import REPO_ROOT


def test_doctor_healthy_on_fresh_project(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--root", str(project), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "result: healthy" in out
    assert "dry_run ON" in out
    assert (project / "data" / "flins.db").is_file()


def test_doctor_fails_on_broken_policy(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "config" / "policy.yaml").write_text("version: 1\n", encoding="utf-8")
    assert main(["--root", str(project), "doctor"]) == 1
    assert "FAIL" in capsys.readouterr().out


@pytest.mark.parametrize("name", sorted(STUBS))
def test_stubs_exit_2(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(name.split()) == 2
    assert STUBS[name] in capsys.readouterr().out


def test_gitignore_keeps_secrets_and_data_out() -> None:
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in (".env", "data/", "config/policy.yaml", "config/flins.yaml"):
        assert entry in lines


def test_mode_and_stop_commands(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = ["--root", str(project)]
    assert main([*root, "mode"]) == 0
    assert "Normal mode" in capsys.readouterr().out
    assert main([*root, "mode", "focus", "mode", "for", "1h"]) == 0
    assert "Focus mode on until" in capsys.readouterr().out
    assert main([*root, "stop"]) == 0
    assert "Paused" in capsys.readouterr().out
    assert main([*root, "mode", "working", "mood"]) == 0
    assert "resume" in capsys.readouterr().out  # still paused: must resume first
    assert main([*root, "resume"]) == 0
    assert "Resumed" in capsys.readouterr().out
    assert main([*root, "mode", "make", "me", "a", "sandwich"]) == 2


def test_voice_download_list_flag_is_accepted() -> None:
    from flins.cli import build_parser

    args = build_parser().parse_args(["voice", "download", "--list"])
    assert args.action == "download" and args.list is True
    assert build_parser().parse_args(["voice", "say", "hello", "there"]).text == ["hello", "there"]
