"""Draft v2 + about_me.yaml: FLINS answers the message using only facts you allowed."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from flins.adapters.base import ContactInfo, ContactsUpsert, InboundMessage, MeInfo, SidecarHello
from flins.core.audit import AuditLog
from flins.core.clock import FrozenClock
from flins.core.db import connect, migrate
from flins.core.events import EventBus
from flins.core.ingest import Ingestor
from flins.core.modes import ModeEvent, ModeManager
from flins.core.pipeline import Brain, Pipeline
from flins.core.policy_schema import Policy
from flins.llm.gateway import FakeBackend, LLMGateway
from flins.llm.prompts import PromptError, latest_name, load_prompt
from flins.llm.services import Drafter, RedFlagChecker, Triager
from flins.settings import AboutMe, load_settings
from tests.conftest import REPO_ROOT

PROMPTS = REPO_ROOT / "prompts"
ABOUT = AboutMe.model_validate(
    {
        "facts": [
            {"fact": "Hemanth is an engineering student.", "share_with": "known"},
            {"fact": "He's usually free after 7pm.", "share_with": "trusted"},
            {"fact": "He's travelling to Hyderabad this weekend.", "share_with": "inner"},
            {"fact": "He has a blog.", "share_with": "everyone"},
        ],
        "answers": [
            {
                "question": "Did you get my file?",
                "answer": "He checks email tonight.",
                "share_with": "known",
            }
        ],
    }
)


@pytest.mark.parametrize(
    ("tier", "visible"),
    [
        ("unknown", {"blog"}),
        ("known", {"blog", "student", "file"}),
        ("trusted", {"blog", "student", "file", "7pm"}),
        ("inner", {"blog", "student", "file", "7pm", "Hyderabad"}),
        ("blocked", set()),
    ],
)
def test_facts_by_tier(tier: str, visible: set[str]) -> None:
    text = " ".join(ABOUT.for_tier(tier))
    for word in ("blog", "student", "file", "7pm", "Hyderabad"):
        assert (word in text) == (word in visible), (tier, word)


def test_default_share_with_is_trusted() -> None:
    about = AboutMe.model_validate({"facts": [{"fact": "x"}]})
    assert about.for_tier("known") == [] and about.for_tier("trusted") == ["x"]


def test_newest_prompt_version_is_used(tmp_path: Path) -> None:
    assert latest_name(PROMPTS, "draft") == "draft_v2"
    assert load_prompt(PROMPTS, "draft").version == "draft_v2"
    assert load_prompt(PROMPTS, "draft_v1").version == "draft_v1"  # old one still loadable
    with pytest.raises(PromptError):
        latest_name(tmp_path, "draft")


def test_draft_v2_tells_the_model_to_answer_the_message() -> None:
    system = load_prompt(PROMPTS, "draft").system
    assert "Answer what the person actually said" in system
    assert "Mention the user's availability only when it matters" in system


def test_example_about_me_shares_nothing(project: Path) -> None:
    """Out of the box FLINS knows nothing about you: every fact is opt-in."""
    settings = load_settings(project)
    assert settings.about.for_tier("inner") == []


@pytest.mark.parametrize(
    ("tier", "present", "absent"),
    [
        ("trusted", ["usually free after 7pm"], ["Hyderabad"]),
        ("inner", ["usually free after 7pm", "Hyderabad"], []),
    ],
)
async def test_drafter_gets_only_the_contacts_facts(
    tmp_path: Path,
    example_policy_dict: dict[str, Any],
    tier: str,
    present: list[str],
    absent: list[str],
) -> None:
    policy = Policy.model_validate(example_policy_dict)
    db = tmp_path / "t.db"
    conn = await connect(db)
    await migrate(conn)
    clock = FrozenClock(datetime(2026, 11, 9, 4, 30, tzinfo=UTC))
    bus = EventBus()
    backend = FakeBackend(
        {
            "triage": {
                "category": "scheduling",  # plans: drafted for approval, for any tier
                "urgency": 0,
                "flags": [],
                "needs_human": False,
                "injection_suspected": False,
                "language": "en",
                "summary": "plans",
            },
            "draft": {
                "reply": "Nice!",
                "confidence": 0.9,
                "makes_commitment": False,
                "mentions_facts_not_provided": False,
                "reason": "ok",
            },
            "redflag": {"safe": True, "reason": "ok"},
        }
    )
    gw = LLMGateway(backend, conn, clock, daily_budget_usd=1.0, tz=ZoneInfo("Asia/Kolkata"))
    brain = Brain(
        gw,
        Triager(gw, load_prompt(PROMPTS, "triage"), list(policy.categories)),
        Drafter(gw, load_prompt(PROMPTS, "draft")),
        RedFlagChecker(gw, load_prompt(PROMPTS, "redflag")),
    )
    modes = await ModeManager.open(db, bus, policy, clock)
    ingest = Ingestor(conn, bus, AuditLog(conn))
    Pipeline(conn, bus, policy, modes, clock, brain=brain, about=ABOUT)
    jid = "919111111111@s.whatsapp.net"
    await ingest.handle(
        SidecarHello(sidecar_version="t", state="open", me=MeInfo(id="919000000000@s.whatsapp.net"))
    )
    await ingest.handle(ContactsUpsert(contacts=[ContactInfo(pn=jid, name="Rahul")]))
    await conn.execute("UPDATE contacts SET tier = ? WHERE wa_jid = ?", (tier, jid))
    await conn.commit()
    await modes.apply(ModeEvent.ENTER_WORKING, source="test")
    await ingest.handle(
        InboundMessage(
            wa_msg_id="m1",
            chat_jid=jid,
            sender_jid=jid,
            text="what are you up to this weekend?",
            ts_ms=int(clock.now().timestamp() * 1000),
            type="text",
        )
    )
    await modes.close()
    await conn.close()
    drafts = [user for name, user in backend.calls if name == "draft_v2"]
    assert drafts, backend.calls
    for text in present:
        assert text in drafts[0]
    for text in absent:
        assert text not in drafts[0]
