"""The core side of `ctx.*` (SPEC §9.3). Every call is checked against the skill's manifest.

A denied call goes back to the skill as an error **and** writes an `audit_log` row
(`skill.denied`). The data itself comes from a `Provider`: the real one reads the database and
your config files; tests and `flins skills test` use fixture data with the same checks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiosqlite
import httpx
import yaml

from flins.core.audit import AuditLog
from flins.core.clock import from_iso, to_iso
from flins.skills.manifest import Manifest

# method -> permission it needs (None: always allowed)
REQUIRES: dict[str, str | None] = {
    "log": None,
    "thread": "wa:read_thread",
    "faq": "faq:read",
    "calendar.free_busy": "calendar:read",
    "messages.recent": "messages:read_recent",
    "http.get": "http:get",
}
MAX_CALLS = 50
MAX_THREAD = 20
MAX_RECENT = 200
MAX_HTTP_BYTES = 200_000


class CapabilityDenied(PermissionError):
    pass


class CapabilityFailed(RuntimeError):
    pass


class Provider(Protocol):
    async def thread(self, n: int) -> list[dict[str, Any]]: ...
    async def faq(self) -> list[dict[str, Any]]: ...
    async def free_busy(self, start: datetime, end: datetime) -> list[dict[str, str]]: ...
    async def recent(self, since: datetime) -> list[dict[str, Any]]: ...
    async def http_get(self, url: str) -> dict[str, Any]: ...


@dataclass
class Capabilities:
    manifest: Manifest
    provider: Provider
    tz: ZoneInfo
    now: datetime
    audit: AuditLog | None = None
    run: str = ""
    denied: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    calls: int = 0

    async def deny(self, what: str, detail: str) -> None:
        self.denied.append(what)
        if self.audit is not None:
            await self.audit.record(
                f"skill:{self.manifest.name}",
                "skill.denied",
                {"skill": self.manifest.name, "what": what, "detail": detail, "run": self.run},
            )

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls += 1
        if self.calls > MAX_CALLS:
            raise CapabilityFailed(f"more than {MAX_CALLS} ctx calls")
        if method not in REQUIRES:
            await self.deny(method, "unknown capability")
            raise CapabilityDenied(f"unknown capability {method!r}")
        perm = REQUIRES[method]
        if perm is not None and not self.manifest.allows(perm):
            await self.deny(perm, f"called ctx.{method} without declaring {perm}")
            raise CapabilityDenied(f"{method} needs permission {perm!r}, which isn't declared")
        try:
            return await self._dispatch(method, params)
        except CapabilityDenied:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise CapabilityFailed(f"bad arguments for {method}: {exc}") from None

    async def _dispatch(self, method: str, p: dict[str, Any]) -> Any:
        if method == "log":
            if len(self.logs) < 50:
                self.logs.append(str(p.get("message", ""))[:500])
            return None
        if method == "thread":
            n = max(1, min(int(p.get("n", 10)), MAX_THREAD))
            return await self.provider.thread(n)
        if method == "faq":
            return await self.provider.faq()
        if method == "calendar.free_busy":
            start = self._local_dt(str(p["start"]))
            end = self._local_dt(str(p["end"]))
            if end <= start or end - start > timedelta(days=7):
                raise ValueError("end must be after start, at most 7 days")
            return await self.provider.free_busy(start, end)
        if method == "messages.recent":
            hours = float(p.get("hours", 8))
            if not 0 < hours <= 24:
                raise ValueError("hours must be between 0 and 24")
            return await self.provider.recent(self.now - timedelta(hours=hours))
        # http.get
        url = str(p["url"])
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or host not in self.manifest.network:
            await self.deny(f"http:{host or url[:60]}", "host not in the network allowlist")
            raise CapabilityDenied(f"{host or url!r} is not in this skill's network list")
        return await self.provider.http_get(url)

    def _local_dt(self, text: str) -> datetime:
        dt = datetime.fromisoformat(text)
        return dt.replace(tzinfo=self.tz) if dt.tzinfo is None else dt.astimezone(self.tz)


# ---------------------------------------------------------------------- real data


class DbProvider:
    """Real data for a live run. Reads only what the method promises."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        tz: ZoneInfo,
        *,
        chat_jid: str | None = None,
        faq_path: Path | None = None,
        calendar_path: Path | None = None,
    ) -> None:
        self._conn = conn
        self._tz = tz
        self._chat = chat_jid
        self._faq = faq_path
        self._calendar = calendar_path

    def _local(self, iso: str) -> str:
        return from_iso(iso).astimezone(self._tz).strftime("%Y-%m-%d %H:%M")

    async def thread(self, n: int) -> list[dict[str, Any]]:
        if self._chat is None:
            return []
        async with self._conn.execute(
            "SELECT direction, type, text, ts FROM messages WHERE chat_jid = ? "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (self._chat, n),
        ) as cur:
            rows = list(await cur.fetchall())
        return [
            {
                "from": "me" if r["direction"] == "OUT" else "them",
                "text": r["text"] if r["text"] is not None else f"[{r['type']}]",
                "time": self._local(r["ts"]),
            }
            for r in reversed(rows)
        ]

    async def faq(self) -> list[dict[str, Any]]:
        return load_faq(self._faq)

    async def free_busy(self, start: datetime, end: datetime) -> list[dict[str, str]]:
        if self._calendar is None or not self._calendar.is_file():
            raise CapabilityFailed("no calendar file configured (skills.calendar_ics)")
        events = parse_ics(self._calendar.read_text(encoding="utf-8", errors="replace"), self._tz)
        return busy_between(events, start, end)

    async def recent(self, since: datetime) -> list[dict[str, Any]]:
        async with self._conn.execute(
            """
            SELECT m.ts, m.type, m.text,
                   COALESCE(c.display_name, '~' || c.push_name, 'Someone') AS who,
                   d.action, d.escalate, d.flags, d.category
            FROM messages m
            LEFT JOIN contacts c ON c.wa_jid = m.chat_jid OR c.wa_lid = m.chat_jid
            LEFT JOIN decisions d ON d.message_id = m.id
            WHERE m.direction = 'IN' AND m.ts >= ? AND m.chat_jid NOT LIKE '%@g.us'
            ORDER BY m.ts LIMIT ?
            """,
            (to_iso(since), MAX_RECENT),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "who": r["who"],
                "time": self._local(r["ts"]),
                "text": (r["text"] or f"[{r['type']}]")[:200],
                "action": r["action"],
                "urgent": bool(r["escalate"]),
                "flags": json.loads(r["flags"] or "[]"),
                "category": r["category"],
            }
            for r in rows
        ]

    async def http_get(self, url: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            try:
                r = await client.get(url, headers={"User-Agent": "flins-skill"})
            except httpx.HTTPError as exc:
                raise CapabilityFailed(f"fetch failed: {type(exc).__name__}") from None
        return {
            "status": r.status_code,
            "text": r.content[:MAX_HTTP_BYTES].decode("utf-8", errors="replace"),
        }


class FixtureProvider:
    """Synthetic data for `flins skills test` and unit tests."""

    def __init__(self, data: dict[str, Any], tz: ZoneInfo) -> None:
        self._d = data
        self._tz = tz

    async def thread(self, n: int) -> list[dict[str, Any]]:
        return list(self._d.get("thread", []))[-n:]

    async def faq(self) -> list[dict[str, Any]]:
        return list(self._d.get("faq", []))

    async def free_busy(self, start: datetime, end: datetime) -> list[dict[str, str]]:
        if "calendar" not in self._d:
            raise CapabilityFailed("no calendar file configured (skills.calendar_ics)")
        events = [
            (
                datetime.fromisoformat(e["start"]).replace(tzinfo=self._tz),
                datetime.fromisoformat(e["end"]).replace(tzinfo=self._tz),
            )
            for e in self._d["calendar"]
        ]
        return busy_between(events, start, end)

    async def recent(self, since: datetime) -> list[dict[str, Any]]:
        return list(self._d.get("recent", []))

    async def http_get(self, url: str) -> dict[str, Any]:
        pages: dict[str, Any] = self._d.get("http", {})
        if url not in pages:
            raise CapabilityFailed("fetch failed: not in fixture")
        return {"status": 200, "text": str(pages[url])}


# ---------------------------------------------------------------------- files


def load_faq(path: Path | None) -> list[dict[str, Any]]:
    """config/faq.yaml: a list of {q: [phrases], a: answer, tiers: [..]}."""
    if path is None or not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = data.get("faq", []) if isinstance(data, dict) else data
    out = []
    for item in items or []:
        if isinstance(item, dict) and item.get("a") and item.get("q"):
            q = item["q"] if isinstance(item["q"], list) else [item["q"]]
            out.append(
                {
                    "q": [str(x) for x in q],
                    "a": str(item["a"]),
                    "tiers": [str(t) for t in item.get("tiers", ["inner", "trusted"])],
                }
            )
    return out


_ICS_LINE = re.compile(r"^(DTSTART|DTEND)([^:]*):(.+)$")


def _ics_time(params: str, value: str, tz: ZoneInfo) -> datetime | None:
    value = value.strip()
    try:
        if "VALUE=DATE" in params.upper() or re.fullmatch(r"\d{8}", value):
            d = date(int(value[:4]), int(value[4:6]), int(value[6:8]))
            return datetime(d.year, d.month, d.day, tzinfo=tz)
        base = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    if value.endswith("Z"):
        return base.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    m = re.search(r"TZID=([^;:]+)", params)
    if m:
        try:
            return base.replace(tzinfo=ZoneInfo(m.group(1).strip('"'))).astimezone(tz)
        except (KeyError, ValueError):
            pass
    return base.replace(tzinfo=tz)


def parse_ics(text: str, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """Busy intervals from an .ics export. One-off events only (repeating rules are skipped);
    free/transparent events are ignored."""
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold continuation lines
    events: list[tuple[datetime, datetime]] = []
    start = end = None
    inside = transparent = recurring = False
    for raw in text.splitlines():
        line = raw.strip()
        if line == "BEGIN:VEVENT":
            inside, start, end, transparent, recurring = True, None, None, False, False
        elif line == "END:VEVENT":
            if inside and start and not transparent and not recurring:
                events.append((start, end or start + timedelta(hours=1)))
            inside = False
        elif inside:
            m = _ICS_LINE.match(line)
            if m:
                t = _ics_time(m.group(2), m.group(3), tz)
                if m.group(1) == "DTSTART":
                    start = t
                else:
                    end = t
            elif line.upper() == "TRANSP:TRANSPARENT" or line.upper() == "STATUS:CANCELLED":
                transparent = True
            elif line.upper().startswith("RRULE"):
                recurring = True
    return events


def busy_between(
    events: list[tuple[datetime, datetime]], start: datetime, end: datetime
) -> list[dict[str, str]]:
    """Merged busy intervals overlapping [start, end), as local 'YYYY-MM-DD HH:MM' strings."""
    tz = start.tzinfo
    spans = sorted((max(s, start), min(e, end)) for s, e in events if s < end and e > start)
    merged: list[tuple[datetime, datetime]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    fmt = "%Y-%m-%d %H:%M"
    return [
        {"start": s.astimezone(tz).strftime(fmt), "end": e.astimezone(tz).strftime(fmt)}
        for s, e in merged
    ]
