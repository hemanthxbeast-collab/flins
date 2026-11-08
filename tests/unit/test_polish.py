"""After M9: dashboard times in your time zone, medical/legal hard rule, Piper voice,
dashboard WhatsApp pairing."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flins.core.db import connect, migrate
from flins.core.levels import Level, Mode
from flins.core.policy import MessageFacts, RuleContext, decide_message
from flins.core.policy_schema import Policy
from flins.llm.prompts import load_prompt
from flins.settings import Settings, load_settings
from flins.voice.factory import VoiceUnavailable, make_tts
from flins.voice.tts import PiperTTS
from flins.web.app import TEMPLATES, create_app
from flins.web.qr import qr_svg
from tests.conftest import REPO_ROOT

T0 = datetime(2026, 11, 8, 8, 30, tzinfo=UTC)
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def policy(example_policy_dict: dict[str, Any]) -> Policy:
    return Policy.model_validate(example_policy_dict)


# ---------------------------------------------------------------------- times


def test_dashboard_shows_local_time(tmp_path: Path, policy: Policy) -> None:
    create_app(tmp_path / "x.db", policy, port=PORT)
    local = TEMPLATES.env.filters["local"]
    assert local("2026-11-08T08:30:00.000+00:00") == "14:00"  # IST = UTC + 5:30
    assert local("2026-11-08T20:00:00.000+00:00", "%d %b %H:%M") == "09 Nov 01:30"
    assert local(None) == "" and local("not a time") == "not a time"


# ---------------------------------------------------------------------- medical / legal


def decide(policy: Policy, text: str) -> Any:
    ctx = RuleContext(
        mode=Mode.WORKING,
        mode_started_at=T0,
        tier="trusted",
        message=MessageFacts(
            text=text,
            msg_type="text",
            is_group=False,
            chat_jid="1@s.whatsapp.net",
            mentions_me=False,
            ts=T0,
        ),
        auto_sent_last_hour=0,
        identical_recent=1,
        fast_replies=0,
    )
    return decide_message(ctx, policy)


@pytest.mark.parametrize(
    "text",
    [
        "results came back, call when you can",  # the one Groq missed
        "reports are ready",
        "the doctor said he needs rest",
        "did you take your medicines?",
        "blood test report attached",
        "she has fever since morning",
        "police came to the house",
        "got a legal notice today",
        "court date is next week",
        "dawai le li?",
    ],
)
def test_health_and_legal_messages_only_notify(policy: Policy, text: str) -> None:
    d = decide(policy, text)
    assert d.level <= Level.NOTIFY, text
    assert "legal_medical" in d.flags and "legal_medical" in d.hard_rules


@pytest.mark.parametrize(
    "text",
    ["send me the report by 5", "can you scan the document", "are you free for a call?"],
)
def test_ordinary_words_are_not_flagged(policy: Policy, text: str) -> None:
    assert "legal_medical" not in decide(policy, text).flags


def test_hospital_emergency_still_gets_the_urgent_acknowledgement(policy: Policy) -> None:
    d = decide(policy, "Emergency, dad is in hospital")
    assert d.escalate and "legal_medical" not in d.flags


def test_your_own_sensitive_words(example_policy_dict: dict[str, Any]) -> None:
    data = {**example_policy_dict}
    data["hard_rules"] = {**data["hard_rules"], "sensitive_patterns": [r"\bvakil\b"]}
    d = decide(Policy.model_validate(data), "vakil se baat hui")
    assert d.level <= Level.NOTIFY and "legal_medical" in d.flags


def test_triage_v2_names_vague_health_cues() -> None:
    p = load_prompt(REPO_ROOT / "prompts", "triage")
    assert p.version == "triage_v2"
    assert "results came back" in p.system and "personal_sensitive" in p.system


# ---------------------------------------------------------------------- Piper


@pytest.fixture
def settings(project: Path) -> Settings:
    return load_settings(project)


def test_piper_needs_a_downloaded_voice(settings: Settings) -> None:
    settings.app.voice.tts = "piper"
    with pytest.raises(VoiceUnavailable, match="flins voice download"):
        make_tts(settings)
    model = settings.data_dir / "voices" / f"{settings.app.voice.piper_voice}.onnx"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"x")
    assert isinstance(make_tts(settings), PiperTTS)


def test_piper_joins_sentences_and_applies_speed(monkeypatch: pytest.MonkeyPatch) -> None:
    np = pytest.importorskip("numpy")
    pytest.importorskip("piper")

    class Chunk:
        def __init__(self, n: int) -> None:
            self.audio_float_array = np.ones(n, dtype="float32")
            self.sample_rate = 22050

    class FakeVoice:
        def __init__(self) -> None:
            self.config: Any = None

        def synthesize(self, text: str, syn_config: Any = None) -> list[Chunk]:
            self.config = syn_config
            return [Chunk(100), Chunk(50)]

    tts = PiperTTS(Path("unused.onnx"), speed=1.25)
    tts._voice = FakeVoice()
    audio, rate = tts._audio("Hello. Second sentence.")
    assert len(audio) == 150 and rate == 22050
    assert tts._voice.config.length_scale == pytest.approx(0.8)


# ---------------------------------------------------------------------- pairing


def test_qr_svg() -> None:
    grid = [[1 if (r + c) % 2 else 0 for c in range(21)] for r in range(21)]
    svg = qr_svg(grid)
    assert svg.startswith("<svg") and svg.count("h1v1h-1z") == sum(map(sum, grid))
    for bad in ([], [[1] * 20] * 21, [[1, 0]] * 21):
        with pytest.raises(ValueError):
            qr_svg(bad)


class FakeProc:
    returncode: int | None = None


@pytest.fixture
def dash(settings: Settings) -> TestClient:
    async def init() -> None:
        conn = await connect(settings.db_path)
        await migrate(conn)
        await conn.close()

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(init())
    app = create_app(settings.db_path, settings.policy, port=PORT, token="T", settings=settings)
    return TestClient(app, base_url=BASE)


def test_pairing_from_the_dashboard(
    dash: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flins.adapters import sidecar_proc

    page = dash.get("/pair")
    assert page.status_code == 200 and "Show the QR code" in page.text
    assert dash.post("/pair", data={"token": "wrong"}).status_code == 403

    started: list[bool] = []
    pair_file = settings.data_dir / "pairing.json"

    async def fake_start(self: Any) -> FakeProc:
        started.append(True)
        grid = [[1 if (r * c) % 3 == 0 else 0 for c in range(25)] for r in range(25)]
        pair_file.write_text(json.dumps({"state": "qr", "modules": grid}), encoding="utf-8")
        return FakeProc()

    monkeypatch.setattr(sidecar_proc.Sidecar, "start_pairing", fake_start)
    r = dash.post("/pair", data={"token": "T"}, follow_redirects=False)
    assert r.status_code == 303 and started == [True]
    html = dash.get("/pair").text
    assert "<svg" in html and "Link a device" in html and 'http-equiv="refresh"' in html


def test_paired_shows_status(dash: TestClient, settings: Settings) -> None:
    auth = settings.data_dir / "wa-auth"
    auth.mkdir(parents=True)
    (auth / "creds.json").write_text("{}", encoding="utf-8")
    html = dash.get("/pair").text
    assert "paired with your WhatsApp" in html and "Show the QR code" not in html
