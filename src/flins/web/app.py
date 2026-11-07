"""Local dashboard (SPEC §2): inbox with decision traces, contacts and tiers, mode buttons.

Listens on 127.0.0.1 only. Every write (POST) needs the per-run token embedded in the page,
and every request must carry a Host header for 127.0.0.1/localhost on our port. Together these
stop other websites in your browser (CSRF) and DNS-rebinding tricks from driving FLINS.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiosqlite
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from flins.core.audit import AuditLog
from flins.core.clock import Clock, SystemClock
from flins.core.commands import CommandRouter
from flins.core.db import connect
from flins.core.events import EventBus
from flins.core.levels import Level
from flins.core.modes import ModeManager
from flins.core.policy_schema import Policy

TIERS = ["inner", "trusted", "known", "unknown", "blocked"]
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
ACTION_WORDS = {
    "auto": "would reply",
    "auto_safe": "would send holding reply",
    "draft": "would ask you to approve",
    "notify": "told you",
    "ignore": "ignored",
}


def create_app(
    db_path: Path,
    policy: Policy,
    *,
    port: int,
    token: str | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    app = FastAPI(title="FLINS", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token or secrets.token_urlsafe(24)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    clk = clock or SystemClock()

    @asynccontextmanager
    async def db() -> AsyncIterator[aiosqlite.Connection]:
        conn = await connect(db_path)
        try:
            yield conn
        finally:
            await conn.close()

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.headers.get("host", "") not in allowed_hosts:
            return Response("Bad host", status_code=400)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "frame-ancestors 'none'"
        )
        return response

    def check_token(given: str) -> None:
        if not secrets.compare_digest(given, app.state.token):
            raise HTTPException(status_code=403, detail="bad token")

    async def mode_text() -> str:
        modes = await ModeManager.open(db_path, EventBus(), policy, clk)
        try:
            return await modes.status_text()
        finally:
            await modes.close()

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, name, {"token": app.state.token, "msg": request.query_params.get("msg"), **ctx}
        )

    # ------------------------------------------------------------------ pages

    @app.get("/", response_class=HTMLResponse)
    async def inbox(request: Request) -> HTMLResponse:
        async with db() as conn:
            async with conn.execute(
                """
                SELECT d.id, d.created_at, d.action, d.final_level, d.category, d.escalate,
                       d.hard_rule, d.mode, d.contact_tier, m.chat_jid,
                       (SELECT group_concat(COALESCE(x.text, '[' || x.type || ']'), ' / ')
                          FROM messages x WHERE x.burst_id = d.burst_id) AS texts,
                       COALESCE(c.display_name, '~' || c.push_name, m.chat_jid) AS who,
                       dr.text AS draft, dr.would_send, dr.guard_results
                FROM decisions d
                JOIN messages m ON m.id = d.message_id
                LEFT JOIN contacts c ON c.wa_jid = m.chat_jid OR c.wa_lid = m.chat_jid
                LEFT JOIN drafts dr ON dr.decision_id = d.id
                ORDER BY d.id DESC LIMIT 100
                """
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            async with conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM llm_calls WHERE ts >= date('now')"
            ) as cur:
                cost_row = await cur.fetchone()
        for r in rows:
            r["words"] = ACTION_WORDS.get(r["action"], r["action"])
            guard = json.loads(r["guard_results"]) if r["guard_results"] else None
            r["guard_failures"] = guard["failures"] if guard else []
            r["is_group"] = r["chat_jid"].endswith("@g.us")
        return page(
            request,
            "inbox.html",
            rows=rows,
            mode=await mode_text(),
            dry_run=policy.dry_run,
            cost=cost_row[0] if cost_row else 0,
            llm_calls=cost_row[1] if cost_row else 0,
        )

    @app.get("/decision/{decision_id}", response_class=HTMLResponse)
    async def decision(request: Request, decision_id: int) -> HTMLResponse:
        async with db() as conn:
            async with conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)) as cur:
                d = await cur.fetchone()
            if d is None:
                raise HTTPException(status_code=404)
            async with conn.execute(
                "SELECT ts, type, text FROM messages WHERE burst_id = ? ORDER BY ts",
                (d["burst_id"],),
            ) as cur:
                msgs = [dict(r) for r in await cur.fetchall()]
            async with conn.execute(
                "SELECT * FROM drafts WHERE decision_id = ?", (decision_id,)
            ) as cur:
                draft = await cur.fetchone()
            async with conn.execute(
                "SELECT payload FROM audit_log WHERE kind = 'decision' "
                "AND json_extract(payload, '$.decision_id') = ?",
                (decision_id,),
            ) as cur:
                audit = await cur.fetchone()
        trace = json.loads(audit[0]) if audit else {}
        return page(
            request,
            "decision.html",
            d=dict(d),
            msgs=msgs,
            draft=dict(draft) if draft else None,
            guard=json.loads(draft["guard_results"]) if draft and draft["guard_results"] else None,
            triage=json.loads(d["triage"]) if d["triage"] else None,
            ceilings=json.loads(d["ceilings"]),
            trace=trace,
            level=Level(d["final_level"]).name,
        )

    @app.get("/contacts", response_class=HTMLResponse)
    async def contacts(request: Request, q: str = "", tier: str = "") -> HTMLResponse:
        where, args = ["1=1"], []
        if q:
            where.append("(display_name LIKE ? OR push_name LIKE ? OR wa_jid LIKE ?)")
            args += [f"%{q}%"] * 3
        if tier in TIERS:
            where.append("tier = ?")
            args.append(tier)
        async with db() as conn:
            async with conn.execute(
                f"SELECT * FROM contacts WHERE {' AND '.join(where)} "
                "ORDER BY CASE tier WHEN 'inner' THEN 0 WHEN 'trusted' THEN 1 WHEN 'known' THEN 2 "
                "WHEN 'unknown' THEN 3 ELSE 4 END, COALESCE(display_name, push_name, wa_jid) "
                "LIMIT 300",
                args,
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
            async with conn.execute("SELECT tier, COUNT(*) FROM contacts GROUP BY tier") as cur:
                counts = {r[0]: r[1] for r in await cur.fetchall()}
            async with conn.execute(
                "SELECT a.ts, json_extract(a.payload, '$.pn_row') AS a_id, "
                "json_extract(a.payload, '$.lid_row') AS b_id FROM audit_log a "
                "WHERE a.kind = 'contacts.merge_needed' "
                "AND EXISTS (SELECT 1 FROM contacts "
                "            WHERE id = json_extract(a.payload, '$.pn_row')) "
                "AND EXISTS (SELECT 1 FROM contacts "
                "            WHERE id = json_extract(a.payload, '$.lid_row'))"
            ) as cur:
                merges = [dict(r) for r in await cur.fetchall()]
        return page(
            request,
            "contacts.html",
            rows=rows,
            q=q,
            tier=tier,
            tiers=TIERS,
            counts=counts,
            merges=merges,
        )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True, "mode": await mode_text(), "dry_run": policy.dry_run})

    # ------------------------------------------------------------------ writes

    @app.post("/contacts/{contact_id}/tier")
    async def set_tier(
        contact_id: int, tier: str = Form(...), token: str = Form(...), back: str = Form("")
    ) -> RedirectResponse:
        check_token(token)
        if tier not in TIERS:
            raise HTTPException(status_code=400, detail="unknown tier")
        async with db() as conn:
            async with conn.execute("SELECT tier FROM contacts WHERE id = ?", (contact_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404)
            if row[0] != tier:
                await conn.execute(
                    "UPDATE contacts SET tier = ?, updated_at = datetime('now') WHERE id = ?",
                    (tier, contact_id),
                )
                await conn.commit()
                await AuditLog(conn).record(
                    "dashboard",
                    "contact.tier_changed",
                    {"contact_id": contact_id, "from": row[0], "to": tier},
                )
        target = back if back.startswith("/contacts") else "/contacts"
        return RedirectResponse(target, status_code=303)

    @app.post("/mode")
    async def mode(text: str = Form(...), token: str = Form(...)) -> RedirectResponse:
        check_token(token)
        modes = await ModeManager.open(db_path, EventBus(), policy, clk)
        try:
            reply = await CommandRouter(modes, policy, clk).handle_text(text, source="dashboard")
        finally:
            await modes.close()
        return RedirectResponse(f"/?msg={quote(reply or 'Not a command.')}", status_code=303)

    return app
