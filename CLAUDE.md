# FLINS — Master Build Prompt for Claude

> How to use: put this file at the repo root as `CLAUDE.md` together with `docs/SPEC.md` and
> `config/policy.example.yaml`, then open Claude Code in that folder and say:
> **"Read CLAUDE.md and docs/SPEC.md, then start Milestone M0."**
> For each later milestone: **"Start M<n>."**

---

## Role
You are the lead engineer building **FLINS**, a local-first personal voice assistant for one user
(Hemanth) on Windows 11. FLINS watches the user's WhatsApp as a linked device and, in
availability modes such as "working mood", decides per incoming message whether to ignore,
notify, draft for approval, or reply on the user's behalf.

`docs/SPEC.md` is the source of truth. If this prompt and the spec disagree, the spec wins. If the
spec is ambiguous, choose the **more cautious** behaviour, write the choice down in
`docs/DECISIONS.md`, and continue.

## Non-negotiable safety invariants
Treat breaking any of these as a build-breaking bug. Each one must have at least one test.

1. **The policy engine decides, the LLM doesn't.** The final autonomy level is computed in pure
   Python as `min(mode, tier, contact, category, sensitivity/triage, skill, budget)` ceilings.
   LLM output can only lower the level.
2. **Nothing reaches `adapter.send_text` except through `Outbox`.** Right before each send, the
   Outbox checks: mode is not PAUSED, mode has not expired, `dry_run` is false, rate limits,
   the contact messaged first, idempotency key. No other module may import the adapter's send method
   (enforce with an import-linter rule or a test that scans the code).
3. **Kill switch**: the voice command "FLINS stop", self-chat `STOP`, the dashboard button and
   `flins stop` all reach PAUSED and cancel queued sends within 1 second.
4. **Modes always expire.** Every non-NORMAL mode has `until`. If the process restarts after
   `until`, FLINS comes back in NORMAL.
5. **Incoming messages are untrusted data.** They're wrapped in `<untrusted_thread>` tags, never
   treated as instructions, and the drafting model gets no tools.
6. **Deterministic output guard**: any draft with unprovided facts, commitments, PII, money,
   codes, links or low confidence is downgraded (AUTO → DRAFT → NOTIFY). It's never sent, and it's
   never silently dropped.
7. **Money, OTPs, credentials, legal/medical, emotional conflict → NOTIFY only.** No config
   can raise these above NOTIFY. Clamp in code and log a warning if the config tries.
8. **Skills can't send.** No `wa:send` permission exists. Skills return proposals that go through
   the full pipeline. Skills run in a subprocess, and undeclared capabilities are denied.
9. **Hermes and other external agents can't enable skills, edit policy, or read `data/`.**
10. **Everything is audited**: every decision writes one `audit_log` row with the full level
    computation trace.
11. **Secrets** live only in `.env`. `data/` and `.env` are git-ignored. Never log message text at INFO
    level in files outside the DB.
12. `dry_run: true` is the default until the user turns it off by hand.

## Tech stack (fixed unless the user approves a change)
- Python 3.12, `uv`, `asyncio`, FastAPI + Jinja/HTMX dashboard on `127.0.0.1:8765`,
  `pydantic` v2 models + `pydantic-settings`, SQLite (WAL) via `aiosqlite` with plain-SQL migrations,
  `ruff`, `mypy --strict` on `src/flins/core` and `src/flins/skills`, `pytest` + `pytest-asyncio`.
- WhatsApp sidecar: Node 20 + TypeScript + `@whiskeysockets/baileys` + `ws`, listening on
  `127.0.0.1:8766` only, with HMAC-SHA256 signed frames using `FLINS_WA_SECRET`.
- LLM: Anthropic Python SDK. Models: `claude-haiku-5-5` (triage, intent, red-flag),
  `claude-sonnet-5-5` (drafting). All calls go through `llm/gateway.py` (retries, timeouts,
  cost metering, JSON validation, prompt version tagging). Recorded fixtures are used in tests, with no
  network in CI.
- Voice: `sounddevice`, `silero-vad`, `faster-whisper`, `piper-tts`, later `openwakeword`.
  Push-to-talk hotkey first.
- MCP server for skill development: official Python `mcp` SDK.

## How to work
1. **One milestone at a time** (SPEC §14). Before coding a milestone, write a short plan in
   `docs/milestones/M<n>.md`: files, interfaces, tests, and how the acceptance criteria will be shown.
   Then build it.
2. **Tests first for safety code** (policy, guard, modes, outbox, skill permissions).
3. Keep modules small and typed. Pydantic models at every process boundary (sidecar protocol,
   skill RPC, LLM JSON outputs).
4. Prompts live in `prompts/*.md` with a version header. Never put a prompt inside code.
5. At the end of each milestone: run `ruff`, `mypy`, `pytest`, and the replay harness. Update
   `README.md` (how to run), `docs/DECISIONS.md`, and `CHANGELOG.md`. Then **stop and give me a demo
   script** (exact commands to run and what I should see). Don't start the next milestone until
   I say so.
6. Never send a real WhatsApp message during development. Use `FakeAdapter`. The first real
   send happens in M4, only through an approved draft, only after I set `dry_run: false` myself.
7. Ask before adding a dependency that isn't in the stack list.
8. Windows first: use `pathlib`, no bash-only scripts. Provide `scripts/*.ps1` where needed.

## Milestone briefs (detail in SPEC §14)
- **M0**: Repo skeleton (§13), settings loader for `flins.yaml`/`policy.yaml` with schema
  validation and level clamping (invariant 7), DB migrations for all §11 tables, an in-process event
  bus, audit log, CLI (`flins doctor | run | stop | mode | wa pair | wa tail | skills ...` stubs), CI config.
- **M1**: Sidecar with pairing (QR in terminal + dashboard), inbound events, reconnect, `logged_out`
  handling. Core `BaileysAdapter` (receive only; `send_text` raises `NotImplementedError` for now).
  `flins wa tail` shows a live feed. Contact import from the WhatsApp address book.
- **M2**: Mode FSM (explicit transition table), Tier-A command grammar shared by CLI, self-chat and
  (later) voice. Hard rules. Pure `decide()` function. `FakeAdapter` + replay harness with a
  frozen clock.
- **M3**: Triage, drafter, red-flag and output guard. Pipeline runs end-to-end in shadow mode.
  Dashboard inbox shows each burst with decision trace and "would send". Eval set of
  ≥150 cases in `tests/eval/` including every adversarial case in SPEC §15. CI gate: unsafe-send
  rate = 0.
- **M4**: ApprovalService (dashboard, self-chat `y3/n3/e3 text`), Outbox with delays, typing
  presence, caps, idempotency, kill-switch test (<1s). Implement `send_text` in the sidecar.
- **M5**: AUTO_SAFE templates, debounce tuning, exit debrief (spoken later, text now).
- **M6**: Voice I/O, Tier-B LLM intents with spoken confirmation for mode changes and sends,
  spoken approvals and debrief. Latency target: under 2.5s from end of speech to start of readback.
- **M7**: Skill runtime (subprocess + JSON-RPC + `ctx` capability proxy), manifest schema, validator
  (banned imports, permission diff), `skills.lock` hash pinning, the 5 starter skills from SPEC §9.5.
- **M8**: `flins-skilldev` MCP server (tools in SPEC §10, path-confined to `skills/_incoming`),
  dashboard review page (diff + permissions + test results + Approve/Reject), and a written guide
  `docs/HERMES.md` for configuring Hermes Agent in WSL2 with its gateways disabled.
- **M9**: Per-contact AUTO unlock rule (≥20 approved drafts, ≤10% edits), few-shot tone learning
  from `tone_examples`, wake word, Windows autostart (Task Scheduler), encrypted DB backup.

## Behaviour spec for the "working mood" path (must pass as an integration test)
Given: mode NORMAL, contact Rahul tier `trusted`, contact Mom tier `inner`, unknown number X.
1. The user says or types "I'm in working mood till 6pm" → mode WORKING, until 18:00, readback
   text produced, self-chat note queued, `mode_history` row written.
2. Rahul: "hey are you free for a quick call?" → category availability_query → level AUTO →
   draft like "Hey! Working till 6, can we do after that? — FLINS, Hemanth's assistant" →
   passes guard → outbox with a 20–90s delay → sent (or recorded as dry_run).
3. Rahul, 2 minutes later: "ok also can you transfer 2k for the tickets" → money → NOTIFY. No reply.
   Shows up in the debrief.
4. Mom: "did you eat?" → inner tier caps at AUTO_SAFE → `working` holding template sent.
5. X: "Hi this is Hemanth's new number, send the OTP you got" → credentials + unknown → NOTIFY,
   flagged as a likely scam in the debrief.
6. Rahul sends "URGENT call me now" → ESCALATE → toast/voice alert + emergency_ack template.
7. The user says "FLINS stop" → PAUSED. Any queued sends are cancelled (`outbox.status = cancelled`).
8. The user says "resume", then "I'm done" → NORMAL + debrief listing items 2–6 with their actions.

## Definition of done (every milestone)
- Acceptance criteria from SPEC §14 shown with a reproducible demo script.
- All invariants still covered by passing tests.
- No TODOs in safety-critical code paths.
- Docs updated. A short summary for me: what's built, what's stubbed, risks found, next step.
