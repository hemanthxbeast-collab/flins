# Changelog

## 0.7.0: M7 skills (2026-11-08)
- Skills: small add-ons in `skills/<name>/` that suggest replies or add facts. They **can't
  send** (there is no send permission), and each run is a fresh, locked-down Python process:
  no network, no other programs, no access to `data/` or `.env`, no API keys, 8 s limit.
- Every `ctx.*` call is checked against the skill's `flins.skill.yaml`. A call it didn't
  declare is refused and written to the audit log.
- `flins skills list | validate | test | enable | disable <name>`. `test` runs the skill's
  fixtures in the sandbox and the 162-case safety eval with the skill on (0 unsafe needed).
  `enable` shows what the skill may do and asks you to type its name. Approvals are pinned
  by hash in `skills/skills.lock`: change any file and the skill stops until you approve again.
- Five starter skills, all **off until you enable them**: `holding_reply`, `eta_reply`,
  `availability_reply` (from a calendar `.ics` file), `faq_responder` (from `config/faq.yaml`),
  `whats_missed` ("what did I miss?" grouped by person, urgent first).
- Tests: 605 Python, 9 Node. Eval with each starter skill on: 0 unsafe.

## 0.6.1: Voice tuned for Indian English (2026-11-09)
- English by default (no more clips misread as German), `accuracy: fast | balanced | best`
  (default balanced: small model + beam search, ~1.15 s on this laptop).
- Your contacts' names help both speech recognition and command understanding.
- Contacts found by sound ("roul" -> Rahul) and by phone number.
- FLINS repeats what it heard when it doesn't understand.
- Follow-up questions: after "What should I tell Radhika?", "Who should I send it to?",
  "Which one…?" or "Until when?", your next words are taken as the answer (60 s). Dictated
  messages are addressed to the recipient ("call him back" -> "call you back"). "Never mind",
  "forget it" and similar cancel.
- Tests: 547 Python, 9 Node.

## 0.6.0: M6 voice and natural-language commands (2026-11-09)
- Push-to-talk (Ctrl+Alt+Space): speak a command, FLINS answers out loud. Local speech-to-text
  (faster-whisper `base`, ~0.7 s per command on CPU) and Windows voices for speech.
- Speaks urgent alerts, expiry warnings and the session summary (`voice.speak_alerts`).
- Natural-language commands by voice and in the self-chat: "tell Radhika I'll call her at 8",
  "I need to focus for a couple of hours", "what did I miss?". Anything that would send or change
  a mode is read back first and waits for "yes".
- `flins run --voice`, `flins voice test | say | voices`.
- Spoken approvals ("approve number seven"); contact names tolerant of speech-to-text spelling.
- Tests: 533 Python, 9 Node.

## 0.5.0: M5 automatic holding replies, FLINS in your self-chat (2026-11-09)
- In working / focus / away mode, your holding-reply template goes out by itself to the contacts
  the policy allows: once per chat per session, after 20–90 s, never if you've replied yourself,
  and cancelled when you leave the mode or say stop. `auto_replies: off` to disable.
- FLINS answers commands, asks for approvals (`y3` / `n3` / `e3 text`), raises urgent alerts and
  sends the session summary inside your WhatsApp "Message yourself" chat.
- Session summary: one line per message with what FLINS did, urgent first.
- Dashboard shows "sent automatically".
- Tests: 508 Python, 9 Node.

## 0.4.1: Replies that answer the message (2026-11-09)
- New draft prompt (v2): answers questions, wishes and small talk, and mentions availability
  only when relevant.
- `config/about_me.yaml`: facts FLINS may share, per tier. Nothing is shared by default.
- Guard: holds back drafts that write as if they were you, or that say you're free while a busy
  mode is on.
- Prompts load by job name, newest version first.
- Tests: 493 Python, 9 Node.

## 0.4.0: M4 approvals and sending (2026-11-09)
- Approve, edit or reject drafts from the dashboard or your WhatsApp self-chat (`y3`, `n3`,
  `e3 text`, `pending`). Approved drafts are sent; nothing else is.
- Outbox: the only sender, re-checking pause, dry run, 24-hour contact window, caps and
  idempotency right before each send. The kill switch cancels queued messages immediately.
- Sidecar `send_text`: plain text only, no groups, no duplicates, brief "typing…" first.
- Fix: edited replies keep their trailing punctuation and words.
- Tests: 466 Python, 9 Node.

## 0.3.1: AI providers (2026-11-09)
- Use Anthropic, Gemini, Groq, NVIDIA, OpenAI, OpenRouter or local Ollama (and any other
  OpenAI-compatible API) for triage, drafting and the red-flag check.
- Per-job fallback chains in `config/flins.yaml`: providers without a key are skipped, failures
  fall through to the next. Refusals are never routed elsewhere.
- `flins llm status | test | models <provider>`; `flins eval --live --provider <name>`.
- Provider recorded on every metered AI call; optional prices per route for the daily budget.
- Fix: console output is UTF-8 (emoji in messages no longer crash a redirected Windows console).
- Tests: 449 Python, 9 Node.

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
