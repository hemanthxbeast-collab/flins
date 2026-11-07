"""Schema for config/policy.yaml (SPEC §7, §12).

Loading a policy validates it and enforces the safety invariants from CLAUDE.md:
- invariant 4: every mode except NORMAL and PAUSED must have a positive default duration.
- invariant 7: money, OTP/credentials, sensitive and conflict categories (and their
  sensitivity flags) can never be above NOTIFY. Higher values are clamped, with a warning.
- invariant 12: dry_run defaults to true.
PAUSED is always clamped to IGNORE.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PrivateAttr,
    model_validator,
)

from flins.core.levels import Level, Mode

# Categories and flags that may never be handled above NOTIFY (CLAUDE.md invariant 7).
HARD_NOTIFY_CATEGORIES: frozenset[str] = frozenset(
    {"money_payment", "credentials_otp", "personal_sensitive", "conflict_emotional"}
)
HARD_NOTIFY_FLAGS: frozenset[str] = frozenset(
    {"money", "credentials", "pii", "legal_medical", "injection_suspected"}
)
TIMED_MODES: frozenset[Mode] = frozenset({Mode.WORKING, Mode.FOCUS, Mode.AWAY})

_DURATION_RE = re.compile(r"^\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*$", re.IGNORECASE)


def parse_duration(value: Any) -> Any:
    """Accept '3h', '90m', '1h30m', or a number of minutes."""
    if value is None or isinstance(value, timedelta):
        return value
    if isinstance(value, int | float):
        return timedelta(minutes=value)
    if isinstance(value, str):
        m = _DURATION_RE.match(value)
        if m and (m.group(1) or m.group(2)):
            return timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
    raise ValueError(f"invalid duration {value!r}; use e.g. '3h', '90m' or '1h30m'")


LevelField = Annotated[Level, BeforeValidator(Level.parse)]
Duration = Annotated[timedelta | None, BeforeValidator(parse_duration)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserInfo(_Strict):
    name: str
    signature: str = ""
    timezone: str = "UTC"
    languages: list[str] = Field(default_factory=lambda: ["en"])


class ModePolicy(_Strict):
    ceiling: LevelField
    default_duration: Duration = None
    interrupt_threshold: int = Field(default=3, ge=0, le=3)
    holding_template: str | None = None
    backlog: Literal["ignore_older", "include_recent"] = "ignore_older"


class ProfilePolicy(_Strict):
    base: Mode
    ceiling: LevelField | None = None
    tiers: dict[str, LevelField] = Field(default_factory=dict)


class TierPolicy(_Strict):
    ceiling: LevelField
    tone: str | None = None
    disclose_ai: bool = True
    holding_reply: bool = True


class CategoryPolicy(_Strict):
    ceiling: LevelField
    escalate: bool = False
    template: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_shorthand(cls, data: Any) -> Any:
        # Allow `money_payment: NOTIFY` as shorthand for `{ceiling: NOTIFY}`.
        if isinstance(data, str | int):
            return {"ceiling": data}
        return data


class GroupRules(_Strict):
    allowlist: list[str] = Field(default_factory=list)


class LoopDetection(_Strict):
    fast_reply_seconds: int = Field(default=5, gt=0)
    fast_reply_count: int = Field(default=2, gt=0)
    identical_msg_count: int = Field(default=3, gt=0)


class HardRules(_Strict):
    emergency_keywords: list[str] = Field(default_factory=list)
    money_patterns: list[str] = Field(default_factory=list)
    credential_patterns: list[str] = Field(default_factory=list)
    groups: GroupRules = Field(default_factory=GroupRules)
    loop_detection: LoopDetection = Field(default_factory=LoopDetection)

    @model_validator(mode="after")
    def _patterns_compile(self) -> HardRules:
        for pattern in [*self.money_patterns, *self.credential_patterns]:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return self


class Limits(_Strict):
    per_contact_max_auto_per_hour: int = Field(default=2, ge=0)
    global_max_auto_per_hour: int = Field(default=20, ge=0)
    global_max_auto_per_day: int = Field(default=80, ge=0)
    debounce_quiet_seconds: int = Field(default=25, gt=0)
    debounce_max_seconds: int = Field(default=90, gt=0)
    send_delay_seconds: tuple[int, int] = (20, 90)
    typing_chars_per_second: int = Field(default=8, gt=0)
    only_reply_if_contact_initiated: bool = True
    allow_links_in_auto: bool = False
    daily_llm_budget_usd: float = Field(default=2.0, ge=0)

    @model_validator(mode="after")
    def _ranges(self) -> Limits:
        lo, hi = self.send_delay_seconds
        if not 0 <= lo <= hi:
            raise ValueError("send_delay_seconds must be [min, max] with 0 <= min <= max")
        if self.debounce_max_seconds < self.debounce_quiet_seconds:
            raise ValueError("debounce_max_seconds must be >= debounce_quiet_seconds")
        return self


class Drafting(_Strict):
    min_confidence_auto: float = Field(default=0.75, ge=0, le=1)
    min_confidence_draft: float = Field(default=0.40, ge=0, le=1)
    max_thread_messages: int = Field(default=10, gt=0)
    redflag_check: bool = True


class Approvals(_Strict):
    expiry_minutes: int = Field(default=30, gt=0)
    on_expiry: Literal["none", "holding_template"] = "holding_template"
    channels: list[Literal["dashboard", "voice", "wa_selfchat"]] = Field(
        default_factory=lambda: ["dashboard", "voice", "wa_selfchat"]  # type: ignore[arg-type]
    )


class Retention(_Strict):
    messages_days: int = Field(default=30, gt=0)
    audit_days: int = Field(default=180, gt=0)


class Policy(_Strict):
    version: Literal[1]
    dry_run: bool = True
    # M5: send your holding-reply templates automatically in busy modes ("off" = never).
    auto_replies: Literal["holding_only", "off"] = "holding_only"
    # M5: FLINS writes command answers, approval prompts and urgent alerts to your self-chat.
    self_chat_notes: bool = True
    user: UserInfo
    modes: dict[Mode, ModePolicy]
    profiles: dict[str, ProfilePolicy] = Field(default_factory=dict)
    tiers: dict[str, TierPolicy]
    categories: dict[str, CategoryPolicy]
    sensitivity_caps: dict[str, LevelField] = Field(default_factory=dict)
    hard_rules: HardRules = Field(default_factory=HardRules)
    limits: Limits = Field(default_factory=Limits)
    drafting: Drafting = Field(default_factory=Drafting)
    approvals: Approvals = Field(default_factory=Approvals)
    templates: dict[str, str] = Field(default_factory=dict)
    retention: Retention = Field(default_factory=Retention)

    _warnings: list[str] = PrivateAttr(default_factory=list)

    @property
    def warnings(self) -> list[str]:
        """Safety clamps applied while loading. Show these to the user."""
        return list(self._warnings)

    @model_validator(mode="after")
    def _enforce_invariants(self) -> Policy:
        warn = self._warnings

        missing = [m.value for m in Mode if m not in self.modes]
        if missing:
            raise ValueError(f"modes missing from policy: {', '.join(missing)}")

        for mode in TIMED_MODES:
            d = self.modes[mode].default_duration
            if d is None or d <= timedelta(0):
                raise ValueError(
                    f"mode {mode} needs a positive default_duration (modes must expire)"
                )

        paused = self.modes[Mode.PAUSED]
        if paused.ceiling != Level.IGNORE:
            warn.append(f"modes.PAUSED.ceiling {paused.ceiling.name} clamped to IGNORE")
            paused.ceiling = Level.IGNORE

        for name, profile in self.profiles.items():
            if profile.base == Mode.PAUSED:
                raise ValueError(f"profile {name!r} cannot be based on PAUSED")

        for cat in sorted(HARD_NOTIFY_CATEGORIES):
            pol = self.categories.get(cat)
            if pol is None:
                self.categories[cat] = CategoryPolicy(ceiling=Level.NOTIFY)
                warn.append(f"category {cat} missing; added at NOTIFY")
            elif pol.ceiling > Level.NOTIFY:
                warn.append(f"category {cat} {pol.ceiling.name} clamped to NOTIFY")
                pol.ceiling = Level.NOTIFY

        for flag in sorted(HARD_NOTIFY_FLAGS):
            cap = self.sensitivity_caps.get(flag)
            if cap is None:
                self.sensitivity_caps[flag] = Level.NOTIFY
                warn.append(f"sensitivity cap {flag} missing; added at NOTIFY")
            elif cap > Level.NOTIFY:
                warn.append(f"sensitivity cap {flag} {cap.name} clamped to NOTIFY")
                self.sensitivity_caps[flag] = Level.NOTIFY

        referenced = [
            (f"modes.{m}.holding_template", p.holding_template) for m, p in self.modes.items()
        ] + [(f"categories.{c}.template", p.template) for c, p in self.categories.items()]
        for where, tpl in referenced:
            if tpl is not None and tpl not in self.templates:
                raise ValueError(f"{where} refers to unknown template {tpl!r}")

        return self


def load_policy(path: Path) -> Policy:
    """Load and validate a policy file. Raises ValueError / pydantic.ValidationError."""
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return Policy.model_validate(raw)
