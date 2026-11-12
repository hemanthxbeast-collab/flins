"""JARVIS phase 6b: modes of your own, hold, per-contact topics (docs/milestones/J6b.md).

Every new input to decide() can only lower the level. A phrase of yours can never shadow a
fixed command. Holds and modes always end.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from flins import cli
from flins.core.db import connect, migrate
from flins.core.grammar import ModeCommand, ParseError, is_fixed_command, parse
from flins.core.holds import HoldCommand, HoldError, HoldStore, parse_hold
from flins.core.levels import Level, Mode
from flins.core.modes import ModeEvent, effective_mode
from flins.core.policy import MessageFacts, RuleContext, decide_message
from flins.core.policy_schema import HARD_NOTIFY_CATEGORIES, Policy, ProfilePolicy
from flins.core.triage import TriageResult
from flins.evaluation import load_cases, run_eval
from tests.conftest import EXAMPLE_POLICY, REPO_ROOT
from tests.unit.test_j1_assistant import T0, Rig, build, chat
from tests.unit.test_j6_chats import MOM, PRIYA_K, PRIYA_S, RAHUL, contact

TZ = ZoneInfo("Asia/Kolkata")
OWN = ("dnd", "sleep", "travel")


def raw() -> dict[str, Any]:
    data = yaml.safe_load(EXAMPLE_POLICY.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="module")
def policy() -> Policy:
    return Policy.model_validate(raw())


def triage(category: str, urgency: int = 0) -> TriageResult:
    return TriageResult.model_validate(
        {
            "category": category,
            "urgency": urgency,
            "flags": [],
            "needs_human": False,
            "injection_suspected": False,
            "language": "en",
            "summary": "x",
        }
    )


def ctx(mode: Mode, tier: str = "trusted", text: str = "hey, how are you?") -> RuleContext:
    facts = MessageFacts(
        text=text, msg_type="text", is_group=False, chat_jid=RAHUL, mentions_me=False, ts=T0
    )
    return RuleContext(
        mode=mode, mode_started_at=T0 - timedelta(minutes=5), tier=tier, message=facts
    )


def near(a: Any, b: Any) -> bool:
    """The test clock moves a few seconds with every sentence."""
    return a is not None and abs(a - b) <= timedelta(minutes=1)


@pytest.fixture
async def rig(project: Path) -> AsyncIterator[Rig]:
    r = await build(project)
    await contact(r.conn, RAHUL, "Rahul")
    await contact(r.conn, MOM, "Mom")
    yield r
    await r.modes.close()
    await r.conn.close()


# ---------------------------------------------------------------------- policy schema


def test_example_policy_has_three_modes_of_your_own(policy: Policy) -> None:
    for name in OWN:
        prof = policy.profiles[name]
        assert prof.say and prof.holding_template in policy.templates
        assert prof.default_duration is not None
        assert timedelta(0) < prof.default_duration <= timedelta(hours=12)
    assert policy.profiles["sleep"].base == Mode.AWAY and policy.profiles["dnd"].base == Mode.FOCUS


@pytest.mark.parametrize(
    "phrase",
    ["stop", "Stop!", "resume", "status", "I'm done", "cancel", "yes", "no", "approve 3",
     "pending", "FLINS, stop", "hold", "ok"],
)  # fmt: skip
def test_a_phrase_of_yours_cannot_be_an_existing_command(phrase: str) -> None:
    assert is_fixed_command(phrase)
    data = raw()
    data["profiles"]["sleep"]["say"] = ["sleep mode", phrase]
    with pytest.raises(ValueError, match=r"already is a FLINS command|is too short"):
        Policy.model_validate(data)


@pytest.mark.parametrize("phrase", ["sleep mode", "dnd", "do not disturb", "movie night", "gym"])
def test_ordinary_phrases_are_allowed(phrase: str) -> None:
    assert not is_fixed_command(phrase)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"default_duration": "13h"}, "at most 12h"),
        ({"default_duration": 0}, "at most 12h"),
        ({"holding_template": "nope"}, "unknown template"),
        ({"say": ["zz"]}, "too short"),
        ({"say": ["do not disturb"]}, "also used by profile"),
        ({"base": "NORMAL"}, "base must be WORKING, FOCUS or AWAY"),
        ({"base": "PAUSED"}, "cannot be based on PAUSED"),
        ({"interrupt_threshold": 4}, "interrupt_threshold"),
    ],
)
def test_bad_profiles_are_refused_at_load(change: dict[str, Any], error: str) -> None:
    data = raw()
    data["profiles"]["sleep"].update(change)
    with pytest.raises(ValueError, match=error):
        Policy.model_validate(data)


# ---------------------------------------------------------------------- grammar


@pytest.mark.parametrize(
    ("said", "event", "profile", "hour"),
    [
        ("sleep mode", ModeEvent.ENTER_AWAY, "sleep", None),
        ("FLINS, sleep mode please", ModeEvent.ENTER_AWAY, "sleep", None),
        ("I'm going to sleep", ModeEvent.ENTER_AWAY, "sleep", None),
        ("I'm going to sleep till 7am", ModeEvent.ENTER_AWAY, "sleep", 7),
        ("good night FLINS", ModeEvent.ENTER_AWAY, "sleep", None),
        ("travel mode for 3 hours", ModeEvent.ENTER_AWAY, "travel", 17),
        ("switch to travel mode until 9pm", ModeEvent.ENTER_AWAY, "travel", 21),
        ("I’m travelling", ModeEvent.ENTER_AWAY, "travel", None),
        ("dnd", ModeEvent.ENTER_FOCUS, "dnd", None),
        ("do not disturb till 4pm", ModeEvent.ENTER_FOCUS, "dnd", 16),
        ("dnd 90 minutes", ModeEvent.ENTER_FOCUS, "dnd", 15),
        # the built-in modes are unchanged
        ("working mood till 6pm", ModeEvent.ENTER_WORKING, None, 18),
        ("focus", ModeEvent.ENTER_FOCUS, None, None),
        ("working mood, strict", ModeEvent.ENTER_WORKING, "working-strict", None),
    ],
)
def test_phrases_start_your_modes(
    policy: Policy, said: str, event: ModeEvent, profile: str | None, hour: int | None
) -> None:
    cmd = parse(said, now=T0, tz=TZ, profiles=policy.profiles)
    assert isinstance(cmd, ModeCommand) and (cmd.event, cmd.profile) == (event, profile)
    assert (cmd.until.astimezone(TZ).hour if cmd.until else None) == hour


def test_fixed_commands_always_win_and_other_text_is_untouched(policy: Policy) -> None:
    # Even a profile that slipped past validation can't take over the kill switch.
    rogue = {"x": ProfilePolicy(base=Mode.AWAY, say=["stop", "status", "i'm done"])}
    for said, event in (("stop", ModeEvent.KILL), ("i'm done", ModeEvent.EXIT_TO_NORMAL)):
        cmd = parse(said, now=T0, tz=TZ, profiles=rogue)
        assert isinstance(cmd, ModeCommand) and cmd.event == event and cmd.profile is None
    for said in ("sleep", "I slept well", "travel plans for december", "sleep mode is nice"):
        assert parse(said, now=T0, tz=TZ, profiles=policy.profiles) is None
    bad = parse("sleep mode till banana", now=T0, tz=TZ, profiles=policy.profiles)
    assert isinstance(bad, ParseError)
    assert parse("sleep mode", now=T0, tz=TZ) is None  # without your policy it means nothing


# ---------------------------------------------------------------------- modes end to end


async def test_sleep_mode_by_voice_expires_and_stop_still_works(rig: Rig) -> None:
    said = await rig.say("sleep mode", "voice")
    assert said is not None and said.startswith("Sleep mode on until 22:00.")  # 14:00 + 8 h
    state = rig.modes.state
    assert (state.mode, state.profile) == (Mode.AWAY, "sleep")
    assert state.until is not None and near(state.until, T0 + timedelta(hours=8))
    status = await rig.say("status", "voice")
    assert status is not None and status.startswith("Sleep mode until 22:00.")
    assert await rig.say("stop", "voice") is not None
    assert rig.modes.state.mode == Mode.PAUSED

    await rig.say("resume", "cli")
    await rig.say("yes", "cli")
    rig.clock.advance(timedelta(hours=8, minutes=1))
    assert effective_mode(rig.modes.state, rig.clock.now()) == Mode.NORMAL  # it always ends


async def test_your_modes_take_a_time_and_respect_the_twelve_hour_limit(rig: Rig) -> None:
    said = await rig.say("dnd till 4pm", "cli")
    assert said is not None and said.startswith("DND mode on until 16:00.")
    assert rig.modes.state.profile == "dnd" and rig.modes.state.mode == Mode.FOCUS
    await rig.say("I'm done", "cli")
    assert await rig.say("travel mode for 13 hours", "cli") == "Modes can last at most 12 hours."
    assert rig.modes.state.mode == Mode.NORMAL
    said = await rig.say("travel mode", "cli")
    assert said is not None and said.startswith("Travel mode on until 18:00.")  # its own 4 h


# ---------------------------------------------------------------------- decide(): only lower


def test_a_mode_of_yours_uses_its_own_holding_reply(policy: Policy) -> None:
    plain = decide_message(ctx(Mode.AWAY), policy, triage=triage("greeting_smalltalk"))
    assert (plain.level, plain.template) == (Level.AUTO_SAFE, "away")
    own = decide_message(
        ctx(Mode.AWAY), policy, profile="sleep", triage=triage("greeting_smalltalk")
    )
    assert (own.level, own.template) == (Level.AUTO_SAFE, "sleep")
    # a profile for another mode does nothing
    other = decide_message(
        ctx(Mode.WORKING), policy, profile="sleep", triage=triage("logistics_eta")
    )
    assert other.template == "working" and "profile" not in other.ceilings


def test_a_profile_can_change_what_interrupts_you() -> None:
    data = raw()
    data["profiles"]["sleep"]["interrupt_threshold"] = 1
    loud = Policy.model_validate(data)
    msg = triage("scheduling", urgency=2)
    assert decide_message(ctx(Mode.AWAY), loud, profile="sleep", triage=msg).interrupt is True
    assert decide_message(ctx(Mode.AWAY), loud, triage=msg).interrupt is False  # AWAY: 3


def test_hold_sends_nothing_but_still_alerts(policy: Policy) -> None:
    d = decide_message(ctx(Mode.WORKING), policy, triage=triage("greeting_smalltalk"), hold=True)
    assert d.level == Level.NOTIFY and d.ceilings["hold"] == Level.NOTIFY and d.template is None
    urgent = decide_message(ctx(Mode.FOCUS, "inner", "emergency, call me now"), policy, hold=True)
    assert urgent.level == Level.NOTIFY and urgent.template is None  # no automatic reply
    assert urgent.escalate and urgent.interrupt  # but you are told at once
    assert "hold" in urgent.trace()["ceilings"]  # and it is in the audit trail


def test_profiles_holds_and_topic_lists_never_raise_the_level(policy: Policy) -> None:
    """Over every mode, tier and category: adding any of the three can only lower."""
    texts = ["hey, how are you?", "send ₹500 on upi", "what's the otp", "urgent, call me now"]
    categories = list(policy.categories)
    for mode, tier, category, text in itertools.product(
        (Mode.NORMAL, Mode.WORKING, Mode.FOCUS, Mode.AWAY),
        ("inner", "trusted", "known", "unknown", "blocked"),
        categories,
        texts,
    ):
        base = decide_message(ctx(mode, tier, text), policy, triage=triage(category))
        held = decide_message(ctx(mode, tier, text), policy, triage=triage(category), hold=True)
        assert held.level <= min(base.level, Level.NOTIFY)
        for topics in ([], ["greeting_smalltalk"], categories):
            limited = decide_message(
                ctx(mode, tier, text), policy, triage=triage(category), allowed_categories=topics
            )
            assert limited.level <= base.level
        for name in OWN:
            own = decide_message(
                ctx(mode, tier, text), policy, profile=name, triage=triage(category)
            )
            assert own.level <= base.level
        if base.category in HARD_NOTIFY_CATEGORIES:  # invariant 7, whatever is switched on
            for name in (None, *OWN):
                d = decide_message(
                    ctx(mode, tier, text),
                    policy,
                    profile=name,
                    triage=triage(category),
                    allowed_categories=categories,
                )
                assert d.level <= Level.NOTIFY


# ---------------------------------------------------------------------- hold commands


@pytest.mark.parametrize(
    ("said", "command"),
    [
        ("hold rahul", HoldCommand("hold", "rahul", None, False)),
        ("hold rahul till 6pm", HoldCommand("hold", "rahul", "till 6pm", False)),
        ("hold rahul for 2 hours", HoldCommand("hold", "rahul", "for 2 hours", False)),
        ("hold rahul's messages until 8", HoldCommand("hold", "rahul", "until 8", True)),
        ("hold replies to priya sharma", HoldCommand("hold", "priya sharma", None, True)),
        ("don't reply to mom till 5", HoldCommand("hold", "mom", "till 5", True)),
        ("put rahul on hold", HoldCommand("hold", "rahul", None, True)),
        ("release rahul", HoldCommand("release", "rahul")),
        ("stop holding rahul's messages", HoldCommand("release", "rahul")),
        ("take mom off hold", HoldCommand("release", "mom")),
        ("release everyone", HoldCommand("release_all", "everyone")),
        ("who is on hold", HoldCommand("list")),
        ("holds", HoldCommand("list")),
        ("hold", None),
        ("holding on to hope", None),
        ("what's the weather", None),
    ],
)
def test_parse_hold(said: str, command: HoldCommand | None) -> None:
    assert parse_hold(said) == command


async def test_hold_and_release_by_voice(rig: Rig) -> None:
    said = await rig.say("hold Rahul till 6pm", "voice")
    assert said is not None and said.startswith("Holding Rahul's messages until 18:00.")
    assert not rig.router.awaiting("voice")  # stopping replies needs no confirmation
    holds = HoldStore(rig.conn)
    assert await holds.active(RAHUL, rig.clock.now()) == T0.replace(hour=12, minute=30)  # 18:00
    assert await holds.active(MOM, rig.clock.now()) is None
    assert await rig.say("who is on hold?", "voice") == "On hold: Rahul until 18:00."
    released = await rig.say("release Rahul", "voice")
    assert released is not None and released.startswith("Released Rahul.")
    assert await rig.say("release Rahul", "voice") == "Rahul isn't on hold."
    assert await rig.say("who is on hold", "cli") == "Nobody is on hold."


async def test_a_hold_always_ends(rig: Rig) -> None:
    holds = HoldStore(rig.conn)
    now = rig.clock.now()
    # no time given, no busy mode: four hours
    await rig.say("hold Rahul", "cli")
    assert near(await holds.active(RAHUL, now), now + timedelta(hours=4))
    # in a busy mode: until that mode ends
    await rig.say("working mood till 5pm", "cli")
    await rig.say("put mom on hold", "cli")
    assert await holds.active(MOM, now) == T0.replace(hour=11, minute=30)  # 17:00 IST
    # never more than twelve hours, never in the past
    assert await rig.say("hold Rahul for 13 hours", "cli") == "A hold can last at most 12 hours."
    with pytest.raises(HoldError):
        await holds.set(RAHUL, now - timedelta(minutes=1), source="test", now=now)
    with pytest.raises(HoldError):
        await holds.set(RAHUL, now + timedelta(hours=12, minutes=1), source="test", now=now)
    rig.clock.advance(timedelta(hours=4, minutes=1))
    assert await holds.active(RAHUL, rig.clock.now()) is None  # ended by itself
    assert await holds.all(rig.clock.now()) == []


async def test_hold_needs_a_real_contact(rig: Rig) -> None:
    await contact(rig.conn, PRIYA_S, "Priya Sharma")
    await contact(rig.conn, PRIYA_K, "Priya Kumar")
    rig.answers(chat=chat("Sure, holding the door."))
    assert await rig.say("hold the door", "cli") == "Sure, holding the door."  # not a command
    assert await rig.say("hold Zoya's messages", "cli") == "I couldn't find a contact called zoya."
    which = await rig.say("hold Priya till 6pm", "cli")
    assert which == "Which one: Priya Sharma, Priya Kumar? Say it again with the full name."
    assert await HoldStore(rig.conn).all(rig.clock.now()) == []
    await rig.say("hold Priya Sharma till 6pm", "cli")
    await rig.say("hold Rahul", "cli")
    assert await rig.say("release everyone", "cli") == "Released everyone who was on hold."


async def test_stop_is_still_the_kill_switch(rig: Rig) -> None:
    await rig.say("hold Rahul", "cli")
    await rig.say("working mood", "cli")
    await rig.say("stop", "cli")
    assert rig.modes.state.mode == Mode.PAUSED  # "stop" is never read as a hold command


# ---------------------------------------------------------------------- topics (CLI)


def test_cli_contact_topics(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    async def seed() -> None:
        from flins.settings import load_settings

        conn = await connect(load_settings(project).db_path)
        await migrate(conn)
        await contact(conn, RAHUL, "Rahul")
        await conn.close()

    async def stored() -> Any:
        from flins.settings import load_settings

        conn = await connect(load_settings(project).db_path)
        async with conn.execute("SELECT allowed_categories FROM contacts") as cur:
            row = await cur.fetchone()
        await conn.close()
        return json.loads(row[0]) if row and row[0] else None

    asyncio.run(seed())
    root = ["--root", str(project), "contacts", "topics"]
    assert cli.main([*root, "Rahul"]) == 0
    assert "no topic limit" in capsys.readouterr().out
    assert cli.main([*root, "Rahul", "greeting_smalltalk", "availability_query,"]) == 0
    out = capsys.readouterr().out
    assert "only about: availability_query, greeting_smalltalk" in out
    assert asyncio.run(stored()) == ["availability_query", "greeting_smalltalk"]
    assert cli.main([*root, "Rahul"]) == 0
    assert "answers by itself only about" in capsys.readouterr().out
    assert cli.main([*root, "Rahul", "gossip"]) == 1
    assert "Unknown topic(s): gossip" in capsys.readouterr().out
    assert asyncio.run(stored()) == ["availability_query", "greeting_smalltalk"]  # unchanged
    assert cli.main([*root, "Rahul", "money_payment"]) == 0
    assert "always just tell(s) you" in capsys.readouterr().out
    assert cli.main([*root, "Rahul", "all"]) == 0
    assert asyncio.run(stored()) is None
    assert cli.main([*root, "Nobody"]) == 1
    assert cli.main([*root]) == 2


# ---------------------------------------------------------------------- eval


def test_eval_set_covers_phase_6b() -> None:
    cases = load_cases(REPO_ROOT / "tests" / "eval" / "whatsapp_cases.jsonl")
    assert len(cases) >= 200
    assert {c.get("profile") for c in cases} >= {"dnd", "sleep", "travel"}
    held = [c for c in cases if c.get("hold")]
    assert len(held) >= 20 and all(c["expect"]["max_level"] == "NOTIFY" for c in held)
    limited = [c for c in cases if c.get("topics") is not None]
    assert len(limited) >= 12
    # a topic list that names a NOTIFY-only topic still expects NOTIFY
    assert all(
        c["expect"]["max_level"] == "NOTIFY" for c in limited if c["id"].startswith("topics_cannot")
    )


async def test_eval_skips_modes_a_policy_does_not_define(tmp_path: Path) -> None:
    cases = load_cases(REPO_ROOT / "tests" / "eval" / "whatsapp_cases.jsonl")
    two = [
        next(c for c in cases if c["id"] == "dnd_greeting__good"),
        next(c for c in cases if c["id"] == "hold_greeting__good"),
    ]
    data = raw()
    for name in OWN:
        del data["profiles"][name]
    report = await run_eval(two, Policy.model_validate(data), tmp_path, REPO_ROOT / "prompts")
    assert (report.total, report.skipped, report.unsafe) == (1, 1, [])
    assert "1 cases skipped" in report.summary()
