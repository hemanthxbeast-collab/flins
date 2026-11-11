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
from zoneinfo import ZoneInfo

import aiosqlite
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from flins.adapters.sidecar_proc import Sidecar, SidecarSetupError
from flins.assistant.core import Assistant
from flins.assistant.timeparse import extract
from flins.core.approvals import ApprovalService
from flins.core.audit import AuditLog
from flins.core.autonomy import auto_stats, set_optin
from flins.core.clock import Clock, SystemClock, from_iso, to_iso
from flins.core.commands import CommandRouter
from flins.core.db import connect, migrate
from flins.core.events import EventBus
from flins.core.levels import Level
from flins.core.modes import ModeManager
from flins.core.policy_schema import Policy
from flins.llm.gateway import Backend
from flins.settings import Settings
from flins.skilldev.review import Reviewer
from flins.skilldev.workspace import NAME, WorkspaceError
from flins.skills.engine import SkillEngine
from flins.skills.lock import LOCK_FILE, SkillLock
from flins.web.qr import qr_svg, read_pair_state
from flins.wiring import conversation_store, memory_store, start_assistant, task_store

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
    settings: Settings | None = None,
    assistant: Assistant | None = None,
    llm_backend: Backend | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # A dashboard started on its own (`flins dashboard`) may see a database from an older
        # version: bring it up to date before any page reads the newer tables.
        conn = await connect(db_path)
        try:
            await migrate(conn)
        finally:
            await conn.close()
        yield
        if app.state.assistant_rt is not None:  # the standalone dashboard's own core
            await app.state.assistant_rt.close()

    app = FastAPI(title="FLINS", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.token = token or secrets.token_urlsafe(24)
    app.state.pairing = None  # the `--pair` sidecar process started from /pair
    app.state.assistant = assistant  # `flins run` passes its live core; else made on demand
    app.state.assistant_rt = None
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    clk = clock or SystemClock()
    reviewer = Reviewer(settings) if settings is not None else None
    tz = ZoneInfo(policy.user.timezone)

    def local(value: object, fmt: str = "%H:%M") -> str:
        """Stored times are UTC; show them in your time zone (e.g. IST)."""
        if not value:
            return ""
        try:
            return from_iso(str(value)).astimezone(tz).strftime(fmt)
        except ValueError:
            return str(value)

    TEMPLATES.env.filters["local"] = local

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
        waiting = reviewer.pending_count() if reviewer is not None else 0
        return TEMPLATES.TemplateResponse(
            request,
            name,
            {
                "token": app.state.token,
                "msg": request.query_params.get("msg"),
                "skills_waiting": waiting,
                **ctx,
            },
        )

    def need_reviewer() -> Reviewer:
        if reviewer is None:
            raise HTTPException(status_code=404, detail="skills are not available here")
        return reviewer

    def skill_name(name: str) -> str:
        if not NAME.match(name):
            raise HTTPException(status_code=404, detail="no such skill")
        return name

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
                       dr.text AS draft, dr.would_send, dr.guard_results,
                       a.id AS approval_id, a.short_id, a.status AS approval_status,
                       a.expires_at, o.status AS send_status, o.error AS send_error,
                       o.kind AS send_kind
                FROM decisions d
                JOIN messages m ON m.id = d.message_id
                LEFT JOIN contacts c ON c.wa_jid = m.chat_jid OR c.wa_lid = m.chat_jid
                LEFT JOIN drafts dr ON dr.decision_id = d.id
                LEFT JOIN approvals a ON a.draft_id = dr.id
                LEFT JOIN outbox o ON o.id = (
                    SELECT MAX(x.id) FROM outbox x WHERE x.decision_id = d.id
                )
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
            r["can_approve"] = (
                r["approval_status"] == "pending"
                and r["expires_at"] is not None
                and r["expires_at"] > to_iso(clk.now())
            )
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
            for r in rows:
                tier_pol = policy.tiers.get(r["tier"])
                r["tier_allows_auto"] = bool(tier_pol and tier_pol.ceiling >= Level.AUTO)
                r["auto"] = None
                if r["tier"] not in ("unknown", "blocked"):
                    r["auto"] = (await auto_stats(conn, r["id"])).describe(policy)
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

    @app.post("/contacts/{contact_id}/auto")
    async def set_auto(
        contact_id: int, on: str = Form(...), token: str = Form(...), back: str = Form("")
    ) -> RedirectResponse:
        check_token(token)
        async with db() as conn:
            async with conn.execute("SELECT tier FROM contacts WHERE id = ?", (contact_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="no such contact")
            if row["tier"] in ("unknown", "blocked") and on == "1":
                raise HTTPException(status_code=400, detail="unknown or blocked contacts can't")
            await set_optin(conn, contact_id, on == "1", "dashboard")
        target = back if back.startswith("/contacts") else "/contacts"
        return RedirectResponse(target, status_code=303)

    @app.post("/approvals/{approval_id}")
    async def approval(
        approval_id: int,
        action: str = Form(...),
        token: str = Form(...),
        text: str = Form(""),
    ) -> RedirectResponse:
        check_token(token)
        if action not in ("approve", "edit", "reject"):
            raise HTTPException(status_code=400, detail="unknown action")
        async with db() as conn:
            msg = await ApprovalService(conn, policy).decide(
                action, now=clk.now(), channel="dashboard", approval_id=approval_id, text=text
            )
        return RedirectResponse(f"/?msg={quote(msg)}", status_code=303)

    @app.post("/mode")
    async def mode(text: str = Form(...), token: str = Form(...)) -> RedirectResponse:
        check_token(token)
        modes = await ModeManager.open(db_path, EventBus(), policy, clk)
        try:
            reply = await CommandRouter(modes, policy, clk).handle_text(text, source="dashboard")
        finally:
            await modes.close()
        return RedirectResponse(f"/?msg={quote(reply or 'Not a command.')}", status_code=303)

    # ------------------------------------------------------------------ chat (assistant core)

    async def get_assistant() -> Assistant:
        if app.state.assistant is None:
            if settings is None:
                raise HTTPException(status_code=404, detail="chat is not available here")
            # Standalone dashboard: its own core, which can talk but not send or approve.
            app.state.assistant_rt = await start_assistant(settings, llm_backend)
            app.state.assistant = app.state.assistant_rt.assistant
        found: Assistant = app.state.assistant
        return found

    @app.get("/chat", response_class=HTMLResponse)
    async def chat_page(request: Request) -> HTMLResponse:
        if settings is None:
            raise HTTPException(status_code=404, detail="chat is not available here")
        async with db() as conn:
            turns = await conversation_store(settings, conn).history(40)
        return page(
            request,
            "chat.html",
            turns=turns,
            assistant_name=settings.app.assistant.name,
            live=assistant is not None,
            mode=await mode_text(),
        )

    @app.post("/chat")
    async def chat_say(text: str = Form(...), token: str = Form(...)) -> RedirectResponse:
        check_token(token)
        said = text.strip()[:4000]
        if said:
            await (await get_assistant()).handle(said, source="dashboard")
        return RedirectResponse("/chat#say", status_code=303)

    # ------------------------------------------------------------------ memory (J2)

    def need_settings() -> Settings:
        if settings is None:
            raise HTTPException(status_code=404, detail="not available here")
        return settings

    @app.get("/memory", response_class=HTMLResponse)
    async def memory_page(request: Request) -> HTMLResponse:
        s = need_settings()
        async with db() as conn:
            mem = memory_store(s, conn, clk)
            records = await mem.all(clk.now())
            rows = [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "content": r.content,
                    "where": r.where(mem.tz),
                    "importance": r.importance,
                    "sensitive": r.privacy == "sensitive",
                    "uses": r.uses,
                }
                for r in records
            ]
            on = await mem.enabled()
        return page(
            request,
            "memory.html",
            rows=rows,
            memory_on=on and s.app.assistant.memory.enabled,
            config_off=not s.app.assistant.memory.enabled,
            assistant_name=s.app.assistant.name,
            mode=await mode_text(),
        )

    @app.post("/memory")
    async def memory_change(
        action: str = Form(...),
        token: str = Form(...),
        text: str = Form(""),
        memory_id: int = Form(0),
    ) -> RedirectResponse:
        check_token(token)
        s = need_settings()
        msg = ""
        async with db() as conn:
            mem = memory_store(s, conn, clk)
            if action == "add":
                r = await mem.save(text, source="dashboard", channel=None, now=clk.now())
                msg = {"saved": "Saved.", "duplicate": "I already had that."}.get(
                    r.status, f"Not saved: {r.reason}."
                )
            elif action == "edit":
                r = await mem.edit(memory_id, text, clk.now())
                msg = "Updated." if r.status == "saved" else f"Not changed: {r.reason}."
            elif action == "delete":
                n = await mem.forget([memory_id], via="dashboard")
                msg = "Forgotten." if n else "That memory doesn't exist."
            elif action in ("on", "off"):
                await mem.set_enabled(action == "on")
                msg = f"Memory is {action}."
            else:
                raise HTTPException(status_code=400, detail="unknown action")
        return RedirectResponse(f"/memory?msg={quote(msg)}", status_code=303)

    # ------------------------------------------------------------------ tasks (J3)

    @app.get("/tasks", response_class=HTMLResponse)
    async def tasks_page(request: Request) -> HTMLResponse:
        s = need_settings()
        now = clk.now()
        async with db() as conn:
            tasks = task_store(s, conn)
            rows = [
                {
                    "id": t.id,
                    "title": t.title,
                    "line": t.line(now, tasks.tz),
                    "high": t.priority == "high",
                    "overdue": t.when is not None and t.when < now,
                }
                for t in await tasks.open()
            ]
            done = [t.title for t in await tasks.recently_done(8)]
        return page(
            request,
            "tasks.html",
            rows=rows,
            done=done,
            assistant_name=s.app.assistant.name,
            mode=await mode_text(),
        )

    @app.post("/tasks")
    async def tasks_change(
        action: str = Form(...),
        token: str = Form(...),
        text: str = Form(""),
        task_id: int = Form(0),
    ) -> RedirectResponse:
        check_token(token)
        s = need_settings()
        now = clk.now()
        async with db() as conn:
            tasks = task_store(s, conn)
            if action == "add":
                when = extract(text, now, tasks.tz)
                if not when.title.strip():
                    msg = "A task needs a title."
                else:
                    await tasks.add(
                        when.title,
                        source="dashboard",
                        channel="dashboard",
                        now=now,
                        at=when.at,
                        repeat=when.repeat,
                    )
                    msg = "Added."
            elif action in ("done", "delete"):
                task = await tasks.get(task_id)
                if task is None or task.status != "open":
                    msg = "That task isn't open."
                elif action == "done":
                    await tasks.complete(task, now)
                    msg = "Done."
                else:
                    await tasks.cancel(task, now, via="dashboard")
                    msg = "Deleted."
            else:
                raise HTTPException(status_code=400, detail="unknown action")
        return RedirectResponse(f"/tasks?msg={quote(msg)}", status_code=303)

    # ------------------------------------------------------------------ WhatsApp pairing

    def sidecar() -> Sidecar:
        if settings is None:
            raise HTTPException(status_code=404, detail="pairing is not available here")
        return Sidecar(settings.root, settings.data_dir, settings.app.network.sidecar_port)

    @app.get("/pair", response_class=HTMLResponse)
    async def pair_page(request: Request) -> HTMLResponse:
        sc = sidecar()
        proc = app.state.pairing
        running = proc is not None and proc.returncode is None
        state = read_pair_state(sc.pair_file) if (running or proc is not None) else None
        qr = None
        if state and state.get("state") == "qr" and running:
            try:
                qr = Markup(qr_svg(state.get("modules") or []))
            except ValueError:
                qr = None
        return page(
            request,
            "pair.html",
            paired=sc.is_paired(),
            running=running,
            qr=qr,
            state=state,
            mode=await mode_text(),
        )

    @app.post("/pair")
    async def pair_start(token: str = Form(...)) -> RedirectResponse:
        check_token(token)
        sc = sidecar()
        proc = app.state.pairing
        if sc.is_paired():
            return RedirectResponse("/pair?msg=" + quote("WhatsApp is already paired."), 303)
        if proc is None or proc.returncode is not None:
            try:
                app.state.pairing = await sc.start_pairing()
            except SidecarSetupError as exc:
                return RedirectResponse("/pair?msg=" + quote(str(exc)), 303)
        return RedirectResponse("/pair", 303)

    # ------------------------------------------------------------------ skills (M8)

    @app.get("/skills", response_class=HTMLResponse)
    async def skills_page(request: Request) -> HTMLResponse:
        r = need_reviewer()
        assert settings is not None
        engine = SkillEngine(
            settings.skills_dir, settings.skill_runs_dir, tz=r.workspace_tz, conn=None
        )
        return page(
            request,
            "skills.html",
            installed=engine.all(),
            waiting=r.waiting(),
            requests=r.workspace.requests(),
            mode=await mode_text(),
        )

    @app.get("/skills/review/{name}", response_class=HTMLResponse)
    async def skill_review(request: Request, name: str) -> Response:
        r = need_reviewer()
        try:
            view = await r.view(skill_name(name))
        except WorkspaceError as exc:
            return RedirectResponse(f"/skills?msg={quote(str(exc))}", status_code=303)
        return page(request, "skill_review.html", v=view, mode=await mode_text())

    @app.post("/skills/review/{name}")
    async def skill_decide(
        name: str,
        action: str = Form(...),
        token: str = Form(...),
        sha: str = Form(""),
        note: str = Form(""),
    ) -> RedirectResponse:
        check_token(token)
        r = need_reviewer()
        name = skill_name(name)
        async with db() as conn:
            try:
                if action == "approve":
                    ok, msg = await r.approve(conn, name, sha)
                    if not ok:
                        return RedirectResponse(
                            f"/skills/review/{name}?msg={quote(msg)}", status_code=303
                        )
                elif action == "reject":
                    msg = await r.reject(conn, name, note)
                else:
                    raise HTTPException(status_code=400, detail="unknown action")
            except WorkspaceError as exc:
                msg = str(exc)
        return RedirectResponse(f"/skills?msg={quote(msg)}", status_code=303)

    @app.post("/skills/{name}/disable")
    async def skill_disable(name: str, token: str = Form(...)) -> RedirectResponse:
        check_token(token)
        need_reviewer()
        assert settings is not None
        name = skill_name(name)
        lock = SkillLock(settings.skills_dir / LOCK_FILE)
        if lock.disable(name):
            lock.save()
            async with db() as conn:
                await AuditLog(conn).record(
                    "user", "skill.disabled", {"skill": name, "via": "dashboard"}
                )
            msg = f"Disabled {name}."
        else:
            msg = f"{name} isn't enabled."
        return RedirectResponse(f"/skills?msg={quote(msg)}", status_code=303)

    return app
