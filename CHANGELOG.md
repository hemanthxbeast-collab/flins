# Changelog

## 0.3.0: M3 AI triage, drafts, output guard, dashboard, eval (2026-11-09)
- LLM gateway: structured JSON output, timeouts, retries, validation, refusal handling, cost
  metering per call, daily budget. Versioned prompts in `prompts/`.
- Triage (Claude Haiku 5.5), drafting (Claude Sonnet 5.5), red-flag check (Haiku) for replies
  that would go out unreviewed. Holding replies stay template-only.
- Deterministic output guard: leaks (phone, email, link, codes, money), commitments,
  unprovided facts, never-mention topics, length, language, "I'm not a bot", injection echoes.
- Message bursts (debounce), with urgent messages handled at once.
- Local dashboard at http://127.0.0.1:8765: inbox with "why?" traces and would-send drafts,
  contact tiers, stop/resume buttons.
- 162-case WhatsApp eval with fooled/leaky/failing AI answers; CI gate: 0 unsafe sends.
  `flins eval --live` runs the real models.
- New hard rule: media without text is never auto-answered.
- Tests: 425 Python, 9 Node.

## 0.2.0: M2 modes, commands, hard rules, decide() (2026-11-09)
- Mode state machine (NORMAL, WORKING, FOCUS, AWAY, PAUSED) with an explicit transition
  table, mandatory end times (max 12 h), expiry warnings, cancel, resume, and safe restarts.
- Tier-A command grammar shared by the CLI and the WhatsApp self-chat ("I'm in working mood
  till 6pm", "focus mode for 1h", "stop", "resume", "I'm done", "status").
- Hard rules (paused, blocked, group, credentials, money, emergency, rate, loop, backlog) and a
  pure `decide()` that records every ceiling it applied.
- Shadow pipeline: every incoming message gets a recorded, explained decision. Nothing is sent.
- `flins run`, `flins mode <text>`, `flins stop`.
- Replay harness (`tests/replay/*.jsonl`) with the full CLAUDE.md working-mood path.
- Tests: 361 Python, 9 Node.

## 0.1.0: M1 WhatsApp sidecar, read-only (2026-11-07)
- `wa-sidecar/`: Node + Baileys linked-device client. Pairs by QR, receives messages and
  contacts, reconnects with backoff, stops cleanly on logout or conflict. No send capability.
- Signed, replay-protected WebSocket protocol between core and sidecar (loopback only).
- Python `BaileysAdapter` (read-only), `FakeAdapter`, sidecar process manager.
- Ingest: deduplicated message storage, contact creation and address-book import,
  phone-number/LID identity handling, bus events, connection audit.
- `flins wa pair` and `flins wa tail`. `flins doctor` now checks the sidecar build and pairing.
- Fix after first real pairing: WhatsApp's ID-mapping list no longer creates ~1,000 nameless
  "unknown" contacts. Safe automatic merge of duplicate phone/LID rows. Migration `0003`
  cleans up rows created before the fix.
- Tests: 85 Python (including 2 against the real Node sidecar) and 9 Node.

## 0.0.1: M0 skeleton (2026-11-07)
- uv project, `flins` CLI with a working `doctor` command and stubs for later milestones.
- `policy.yaml` schema with safety clamps (money/OTP/sensitive → NOTIFY, PAUSED → IGNORE),
  required mode expiry, and `dry_run` on by default.
- SQLite schema for every SPEC §11 table, a migration runner, WAL, and an append-only audit log.
- In-process async event bus.
- 53 tests. ruff and mypy (strict on core) clean. GitHub Actions CI config.
