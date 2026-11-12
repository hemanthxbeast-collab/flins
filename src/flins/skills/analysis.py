"""What a skill could do with what it asks for, in plain words (JARVIS phase 8).

Shown on the review page, above the code. Worked out from the manifest by code: the author's
own description plays no part, so a skill can't describe itself as harmless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from flins.core.levels import Level
from flins.core.policy_schema import HARD_NOTIFY_CATEGORIES
from flins.skills.lock import LockEntry
from flins.skills.manifest import Manifest

Risk = Literal["low", "medium", "high"]
_ORDER: dict[Risk, int] = {"low": 0, "medium": 1, "high": 2}
READS_MESSAGES = ("wa:read_thread", "messages:read_recent")
READS_PERSONAL = (*READS_MESSAGES, "contacts:read_card", "calendar:read", "faq:read")


@dataclass(frozen=True)
class Finding:
    risk: Risk
    text: str


@dataclass(frozen=True)
class Analysis:
    risk: Risk
    findings: list[Finding] = field(default_factory=list)
    new: list[str] = field(default_factory=list)  # compared with the version you approved

    @property
    def needs_acknowledgement(self) -> bool:
        return self.risk == "high"

    @property
    def headline(self) -> str:
        return {
            "low": "Low risk: it can't send by itself and reads little.",
            "medium": "Medium risk: it reads personal data, or its replies wait for your approval.",
            "high": "High risk: read the points marked HIGH before you approve.",
        }[self.risk]


def analyse(manifest: Manifest, previous: LockEntry | None = None) -> Analysis:
    m = manifest
    out: list[Finding] = []
    perms = set(m.permissions)

    proposes = "wa:propose_reply" in perms
    if proposes and m.max_level >= Level.AUTO_SAFE:
        out.append(
            Finding(
                "high",
                f"Its replies can go out without you seeing them (it asks for {m.max_level.name}). "
                "The policy, the output guard and the limits still apply, but you won't read "
                "each one first.",
            )
        )
    elif proposes and m.max_level == Level.DRAFT:
        out.append(Finding("medium", "It writes reply drafts. Each one waits for your approval."))
    elif proposes:
        out.append(
            Finding("low", f"It may suggest replies, but at {m.max_level.name} nothing is sent.")
        )
    else:
        out.append(Finding("low", "It can't suggest replies: it only produces notes for you."))

    if "messages:read_recent" in perms:
        out.append(
            Finding(
                "medium", "It reads recent incoming messages from all chats, and what FLINS did."
            )
        )
    if "wa:read_thread" in perms:
        out.append(
            Finding("medium", "It reads the chat it is answering: what the other person wrote.")
        )
    if "calendar:read" in perms:
        out.append(Finding("medium", "It reads the busy times in your calendar file."))
    if "faq:read" in perms:
        out.append(Finding("low", "It reads the answers you wrote in config/faq.yaml."))
    if "contacts:read_card" in perms:
        out.append(Finding("low", "It sees the sender's first name, tier and language."))

    if "http:get" in perms or m.network:
        hosts = ", ".join(m.network) or "no hosts listed, so it can't actually connect"
        reads = sorted(perms & set(READS_PERSONAL))
        if m.network and reads:
            out.append(
                Finding(
                    "high",
                    f"It can fetch web pages from {hosts} AND read {', '.join(reads)}. What it "
                    "reads could be put into a web address and so leave this PC. Check in the "
                    "code what it sends.",
                )
            )
        elif m.network:
            out.append(
                Finding("medium", f"It can fetch web pages from {hosts} (read-only requests).")
            )
        else:
            out.append(Finding("low", f"It asks for web access but lists {hosts}."))

    t = m.triggers
    fixed = sorted(set(t.categories) & HARD_NOTIFY_CATEGORIES)
    if fixed:
        out.append(
            Finding(
                "low",
                f"It runs for {', '.join(fixed)}: those are only ever shown to you, whatever "
                "the skill returns.",
            )
        )
    if not t.categories and not t.commands:
        out.append(Finding("low", "It has no triggers, so it never runs."))
    elif not t.modes and t.categories:
        out.append(Finding("low", "It runs in every mode except paused."))

    new: list[str] = []
    if previous is not None:
        added = sorted(perms - set(previous.permissions))
        if added:
            new.append("new permissions: " + ", ".join(added))
        hosts_added = sorted(set(m.network) - set(previous.network))
        if hosts_added:
            new.append("new hosts: " + ", ".join(hosts_added))
        before = Level[previous.max_level]
        if m.max_level > before:
            new.append(f"level raised from {before.name} to {m.max_level.name}")
    risk: Risk = max((f.risk for f in out), key=lambda r: _ORDER[r], default="low")
    out.sort(key=lambda f: -_ORDER[f.risk])
    return Analysis(risk, out, new)
