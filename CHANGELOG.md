# Changelog

## 0.18.0: phase 6a, your chats (2026-11-12)
- **"What did Rahul say?"** reads the last messages of that chat out, word for word, with no
  AI model. Also "any messages from mom?", "read Rahul's chat".
- **"Summarise Rahul's chat" / "what does he want?"**: a short summary from the `summary`
  model plus what the person is waiting for. The chat goes in as untrusted data, with no
  tools. Without a model it reads the messages out instead.
- **"Reply to him I'll call at 8" / "tell her ..."**: your words, read back, sent only after
  yes, through the existing Outbox path. "him / her / them" is the person you last asked
  about, for 10 minutes. FLINS never drafts the reply from the contact's text.
- **Contact text can't act and doesn't linger**: it never opens a yes / no question, never
  reaches a tool or a memory, and the conversation history keeps only a placeholder.
- Codes are removed from lines that mention an OTP / PIN / password before a summary; chats
  that mention money or codes get a warning line decided by code.
- Audit rows `assistant.chat_read`: kind, contact id, count. No names or text.
- `assistant.chats` settings; prompts `thread_v1`, `chat_v7`; `CommandRouter.send`.
- Live, with the real local models on made-up chats in a throwaway database: read-out,
  summary, "reply to him" → yes → one queued row, and a scam-style message got the warning
  and triggered nothing.
- Tests: 1098 Python (47 new). Eval unchanged: 0 unsafe of 162 (the pipeline is untouched).

## 0.17.1: fixes from the first real voice session (2026-11-12)
- **Ollama starts by itself.** With local models configured but Ollama not running, every
  answer was silently coming from the cloud fallback. FLINS now starts `ollama serve` when a
  job needs it (`llm.start_local`), and `flins run` preloads the conversation model. Never
  started for a model server on another machine.
- **"Yes, add it to the notes" is a yes.** A spoken answer with extra words confirms; a leading
  "no" always cancels; "yes, but ..." / "yes, change ..." confirms nothing.
- **Fewer misrouted commands.** A model can no longer send "list the things we noted" to the
  approvals list or "summarise <name>'s chat" to the what-did-I-miss summary: both read-outs
  now need matching words, and prompt `intent_v2` sends everything else to the conversation.
- **"Hey friends" → "Hey FLINS"** after the hotkey (and Flints, Fins, Flens ...), only at the
  very start of what you said.
- `voice.vocabulary`: your own words for the speech model's spelling hint; the built-in hint
  now covers notes, reminders, tasks and weather phrases.
- Stopping FLINS ends a voice conversation in progress (no more "no active connection").
- Live, on a throwaway database: Ollama started by FLINS, then note → "Yes, add it to the
  notes." → noted; "list the things we noted" read the notes; all calls answered locally.
- Tests: 1053 Python (54 new). Eval unchanged: 0 unsafe of 162.

## 0.17.0: phase 5, conversational voice (2026-11-12)
- **Speech recognition on the GPU**: `accuracy: max` + `stt_device: auto`. Measured here:
  0.3 s per sentence (CPU model: 2.0 s), 2.1 GB video memory, five of five test sentences
  exact. `max` without a GPU uses the small CPU model instead of a large one. The model is
  warmed up at start so the first sentence is fast.
- **Follow-up listening**: after FLINS asks a question or is waiting for yes / no, it listens
  again without the hotkey (low beep). Silence or "thanks" ends it. Limited to 6 in a row.
  `voice.follow_up: question | always | off`, `voice.follow_up_s`.
- **Barge-in**: the hotkey while FLINS is talking stops it (about half a second) and it
  listens. The Windows voice now goes through SAPI directly so it can be cut off
  mid-sentence, with pyttsx3 as fallback; Piper can be stopped too.
- **Wake word**: the main speech model must also hear "FLINS" before it wakes, and its words
  are used as the command (`voice.wake_confirm`). The spotter model always runs on the CPU.
- A spoken confirmation now completes from one key press: "note down buy milk" → "Add this
  to your notes ...? Say yes or no." → "yes".
- Start-up line shows whether speech runs on `cuda` or `cpu`.
- Tests: 999 Python (24 new), 11 Node. Eval unchanged: 0 unsafe of 162.
- Not verified here: a real microphone and audible output (checked silently with generated
  speech clips and the Windows voice at volume 0).

## 0.16.0: phase 4b, online tools (2026-11-12)
- **Three tools that go online, off by default**: `weather` (Open-Meteo: now and three days),
  `wikipedia` (an article's introduction) and `github` (read-only: repository summary, open
  issues, pull requests, commits, releases, a user's repositories). No account is needed.
  Switch on with `assistant.tools.online.enabled: true`.
- **One door out** (`tools/net.py`): GET only, https only, four hosts fixed in code, no
  redirects, 1 MB and 5 s limits. A test fails if any other tool file imports a network library.
- **What is sent is checked by code**: short, and never an email address, phone number, long
  number or anything that looks like a secret. `weather` takes only a place name, `github` only
  its fixed forms; the model never builds a URL.
- **Results are outside content**: no further tool and no memory suggestion in that turn. A
  file's text therefore can't be sent online in the same turn.
- **Wikipedia needs a contact** (it answers 403 without a URL or email in the User-Agent). The
  tool exists only when you set `wikipedia_contact`; it is sent to Wikipedia only.
- Optional `GITHUB_TOKEN` in `.env`, sent only to `api.github.com`, never shown or logged.
- New risk level `online`; a tool can have its own time limit (online tools: 12 s).
- Prompt `chat_v6`; `flins tools` lists the online tools and says why one is off.
- Live check against the real services: weather (2 places, 1 unknown place), Wikipedia
  (2 topics, 1 with no result), GitHub (summary, releases, issues, a user's repositories, a
  missing repository) all returned what they should.
- Not built: general web search and news (need an account with a search provider).
- Tests: 975 Python (61 new, no network), 11 Node. Eval unchanged: 0 unsafe of 162.

## 0.15.0: phase 4a, tools (2026-11-11)
- **FLINS can use tools to answer you.** The conversation model may ask for a tool; code checks
  the name against the allowed list, applies the risk gate, runs it and hands the result back
  as data. No extra model call on turns that need no tool; at most 2 tool uses per message.
- **calculator**: exact arithmetic with a hand-written evaluator (no `eval`): operators,
  brackets, percent, sqrt / log / trig, pi, e; size limits.
- **date_math**: now in a timezone, days between dates, add days / weeks / months / years,
  weekday of a date.
- **Notes**: `note_add` appends to `data/notes.md` only after you confirm the exact text;
  `notes_read` shows or searches it. Secrets are refused.
- **Files** (off by default): list, search and read text files in folders you approve. Paths
  are resolved before the check; hidden, credential, binary and oversized files are refused;
  FLINS's own `data/` and `.env` are never readable; nothing is written.
- **Outside content rule**: after a file is read, that turn can't use another tool or suggest a
  memory, and the content is neutralised and labelled as data.
- Risk levels `read` / `private` / `write`; write tools never run without your yes.
- `flins tools` (list, run); config `assistant.tools`; prompt `chat_v5`; audit rows
  `assistant.tool` record which tool ran, never its input or output.
- Live check (cloud fallback; Ollama was not running): the model chose calculator, date_math
  and no tool correctly on four questions.
- Tests: 914 Python (69 new, 1 skipped here: symlinks need admin on Windows), 11 Node. Eval
  unchanged: 0 unsafe of 162.

## 0.14.0: phase 3b, planning (2026-11-11)
- **Plans.** "make a plan to prepare for my physics exam" proposes 2–8 ordered steps and asks
  "Save this plan?". Only "yes" saves it; each step then becomes a task.
- "show my plans", "what's my plan for …", "what's next", "delete the plan …" (asks first):
  fixed phrases, no model. Finishing a step tells you the next one; finishing the last step
  closes the plan.
- One model call per plan (`reason` job, prompt `plan_v1`, no tools). It sees the time, your
  open tasks and the goal, nothing else. Its steps are cleaned in code (one line, clipped, no
  repeats, at most 10) before they are shown or stored. A vague goal gets one question back.
- Steps never get invented times, and no step can act: FLINS has no tools yet (phase 4).
- Open-task lines now say which plan and step they belong to, also for the conversation model
  (prompt `chat_v4`).
- `flins plans` (list, show, delete); the dashboard Tasks page lists plans in progress.
- New table `plans`, `tasks.plan_id` / `step_no` (migration 0014).
- Tests: 845 Python (28 new), 11 Node. Eval unchanged: 0 unsafe of 162.

## 0.13.0: phase 3a, tasks and reminders (2026-11-11)
- **Reminders and tasks.** "remind me to call the bank at 7", "remind me every weekday at 8am
  to …", "add a task …", "what are my tasks", "done with …", "delete the task …", "snooze 20
  minutes". Fixed phrases, no AI model: they work offline and cost nothing.
- **Time phrases** understood without a model: clock times, "in 20 minutes", today / tomorrow /
  weekdays, morning / evening / tonight, and repeats (every day, weekday, week, or named day).
  No clear time means FLINS asks; it never guesses one.
- **Delivery:** `flins run` checks every second and sends a due reminder to your self-chat (and
  speaks it with voice on). Each fires once. They wait while FLINS is stopped and are marked
  "(was due …)" when late; in focus mode they are written, not spoken, unless high priority.
- The conversation model sees your open tasks (`<tasks>` block, prompt `chat_v3`), so "what
  should I work on?" is answered from the real list. It still cannot create one itself; if a
  request reaches it, it tells you the phrase to say.
- "remember to …" now sets a reminder (it used to answer "I can't set reminders yet").
- `flins tasks` (list, add, done, delete) and a dashboard **Tasks** page.
- New table `tasks` (migration 0013). Audit rows name a task by id, never by title.
- Groundwork only, not switched on: a `max` speech-recognition preset (Whisper large-v3-turbo
  on the GPU) and the NVIDIA runtime libraries in the `voice` extra. Your config is unchanged.
- Tests: 817 Python (39 new), 11 Node. Eval unchanged: 0 unsafe of 162.

## 0.12.0: phase L, FLINS runs on local models (2026-11-11)
- **Local models work.** Conversation, command understanding, summaries, reasoning and coding
  can all run on this PC through Ollama, with a cloud provider as optional fallback. Tested
  end to end on an RTX 4060 (8 GB): every call local, none failed.
- The local backend now uses Ollama's own chat API: answers constrained to the JSON schema, a
  12k-token context window (the default cut reasoning answers off), and thinking switched per
  call. Local models **think only for the `reason` job**; everything else answers directly.
- Measured with `qwen3.5:4b`: 24/24 simple questions right at ~2.3 s per turn; two reasoning
  puzzles wrong without thinking and right with it (15–60 s). `qwen2.5-coder:7b` answers coding
  questions in ~1–2 s once loaded; switching models costs 7–15 s.
- Lower randomness (temperature 0.2) for direct answers, after the default produced a wrong
  "25 times 4".
- A bare "yes" / "no" with nothing pending is never sent to a model to guess a command (a small
  model had read a stray "no" as "stop").
- "One moment." is spoken before a slower reasoning or coding answer on voice.
- Coding and reasoning cues match whole words ("plan" no longer matches "explanations").
- Tests: 777 Python (9 new since 0.11.0), 11 Node. Eval unchanged: 0 unsafe of 162.

## 0.11.1: phase L groundwork, local models (2026-11-11)
- **One local model in video memory at a time:** before a local (Ollama) model is used, FLINS
  unloads any other loaded model (`llm/local.py`). Ollama answers are now constrained to the
  JSON schema, with a longer timeout for the first call after a model switch.
- **New `code` role** for programming questions (borrows from `reason` when not configured).
  The core routes each message to `chat`, `reason` or `code`; `assistant.voice_big_models`
  lets spoken questions use the bigger models too.
- `flins local` shows Ollama's version, downloaded models, what's loaded and how much of it is
  in video memory, and which roles use which model. `flins local unload` frees the memory.
- First real test with `qwen3.5:4b` failed (empty answers after ~32 s of "thinking"), so local
  models are **not switched on** yet: see docs/DECISIONS.md and docs/JARVIS_ARCHITECTURE.md §6.
- Tests: 774 Python (6 new), 11 Node. Eval unchanged: 0 unsafe of 162.

## 0.11.0: JARVIS phase 2, memory (2026-11-11)
- **FLINS remembers what you ask it to.** "Remember that …", "remember this", "forget that",
  "forget (everything) about X", "forget everything", "what do you remember (about X)", "why do
  you know that", "memory on / off". All fixed phrases: they work with the AI down.
- Every memory keeps how and when it was learned ("you told me on 11 Nov 2026 by voice"). Lists
  and "why do you know that" are built from the stored rows, never worded by a model.
- **Suggestions with consent:** the conversation model may propose a memory; FLINS asks "Should
  I remember this: …?" and saves exactly that text only on "yes" (importance threshold and a
  10-minute cooldown in config). A reply that just claims "I'll remember that" is replaced by
  that question.
- Passwords, PINs, OTPs, API keys and card numbers are refused. Health, legal and money memories
  are marked sensitive and reach the model only when your message is about them.
- Kind (semantic / episodic / relationship) and importance are scored on save; duplicates are
  merged; memories can expire.
- `flins memory list | add | forget | edit | clear | on | off`, and a **Memory** page on the
  dashboard. Deleting many memories always asks first.
- Forgetting says so when the fact is still in the recent conversation.
- The dashboard now applies database migrations when it starts on its own.
- Prompt `chat_v2`; migration 0012 (`memories`, `assistant_state`).
- Tests: 768 Python (37 new), 11 Node. Eval unchanged: 0 unsafe of 162.

## 0.10.0: JARVIS phase 1, the assistant core (2026-11-11)
- **FLINS holds a conversation.** Everything you say to it, on any channel, now goes through one
  core (`flins.assistant`): fixed commands first (no AI), then free-form commands with
  confirmation, then a conversation model with context: who it is, who you are, the time, the
  current mode, pending drafts, your about_me facts, a running summary and the last few turns.
- New: `flins ask "<text>"`, `flins chat` (`--history`, `--clear`), and a **Chat** page on the
  dashboard. Voice and self-chat use the same conversation.
- Self-chat stays your notes app: FLINS only talks there when addressed by name, or for 3 minutes
  after it answered (`assistant.self_chat: addressed | all | off`).
- The conversation model has no tools. Replies that claim an action ("I've set a reminder") are
  replaced with an honest "can't do that yet". Model failures and a used-up budget are said out
  loud.
- Personality in config (`assistant:` name, style, humour, notes). No personal details in prompts.
- Model roles `chat`, `reason`, `intent`, `summary` (optional; they borrow from draft / triage).
  Long or planning-style requests use `reason`; voice always uses the fast `chat` model.
- Free-form commands now also work from the CLI and dashboard. `flins ask` and a dashboard without
  `flins run` can change modes but never send or approve.
- Migration 0011 (conversation sessions and turns; pruned after `assistant.keep_days`).
- Fixed a dashboard test that depended on the real date.
- Tests: 731 Python (37 new), 11 Node. Eval unchanged: 0 unsafe of 162.

## 0.9.1: Safer triage, IST times, Piper voice, QR pairing on the dashboard (2026-11-08)
- **Health and legal messages are caught without the AI:** a built-in rule (doctor, medicines,
  test results, "reports are ready", fever, police, court, lawyer, legal notice, …) caps them at
  "just tell you", even if triage misreads them. Add your own words in
  `hard_rules.sensitive_patterns`. Emergencies ("dad is in hospital") still get the urgent
  acknowledgement. Triage prompt `triage_v2` also names vague health cues.
- **Dashboard times are in your time zone** (IST) instead of UTC.
- **Piper voice** (`voice.tts: piper`): a more natural offline voice. Download one with
  `flins voice download` (default `en_US-lessac-medium`; `--list` shows all).
- **Pair WhatsApp from the dashboard:** the new WhatsApp page shows the QR code. The sidecar
  draws it with the encoder it already had, so no new package and no outside scripts.
- CI: GitHub Actions updated off Node.js 20 (checkout v7, setup-node v7, setup-uv v10).
- Tests: 693 Python, 11 Node.

## 0.9.0: M9 earned auto replies, tone learning, backups, autostart, wake word (2026-11-08)
- **Earned auto replies:** with `auto_replies: unlocked`, FLINS sends its AI replies without
  review to contacts you opted in (`flins contacts auto "<name>" on`, or the Contacts page),
  once you've approved at least 20 of its drafts for them with at most 10% edited or rejected.
  Guard, red-flag check, mode, tier and money/OTP rules all still apply. `flins contacts` shows
  each contact's progress. policy.yaml can only make the bar higher.
- **Tone learning:** your edits to drafts are shown to the drafter as style examples
  (prompt `draft_v3`): style only, never facts.
- **Encrypted backups:** `flins backup | backup list | backup restore <file>`. Database
  snapshot plus your personal config, AES-256-GCM with a passphrase from
  `FLINS_BACKUP_PASSPHRASE` (`.env` is never included). One a day while `flins run` runs.
- **Autostart:** `flins autostart on [--voice] | off | status` (Task Scheduler, not elevated;
  Startup folder as a fallback), plus `scripts/autostart.ps1`.
- **Wake word:** say "FLINS" (or "FLINS, status" in one go) with `voice.wake_word: true`.
  Local Whisper `tiny`, nothing saved, ignores FLINS's own voice.
- New dependency: `cryptography`. Tests: 672 Python, 9 Node.

## 0.8.0: M8 Hermes builds skills, you approve them (2026-11-08)
- `flins skilldev`: an MCP server (official `mcp` SDK, stdio) for Hermes Agent: read the skill
  spec and examples, scaffold, write files, validate, test and submit skills. Writes are confined
  to `skills/_incoming/<name>/`. There's no tool to enable a skill or to reach your data.
- Dashboard **Skills** page: installed skills (with Disable), skills waiting for review, and your
  requests. The review page shows what a skill may do, what's new since your last approval, the
  diff and test results, all re-run on Windows. **Approve and enable** re-runs the safety eval,
  installs the skill and pins it; **Reject** leaves a note for Hermes.
- "Make a skill that …" by voice or self-chat saves a request for Hermes.
- `docs/HERMES.md`: running Hermes in WSL2 with no Windows drives, no Windows programs, NAT
  networking and its messaging gateways off. `flins doctor` warns if WSL uses mirrored networking.
- Tests: 639 Python, 9 Node.

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
