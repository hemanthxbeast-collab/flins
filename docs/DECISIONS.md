# Decisions log

Short records of choices made while building. Newest first.

## Phase L groundwork: local models (2026-11-11)
Plan: `docs/JARVIS_ARCHITECTURE.md` §6.
- **Unloading is explicit, not left to Ollama's defaults.** `LocalBackend` asks Ollama what is
  loaded and unloads the others (`keep_alive: 0`) before each call to a different model. It
  never raises: if Ollama can't be asked, the real request reports the problem.
- **`code` is checked before `reason`**, by whole-word cues ("java" doesn't match
  "javascript", "api" doesn't match "capital") or a code fence.
- **Voice stays on the fast model unless `voice_big_models` is on.** Switching models costs
  seconds; with cloud models that wait isn't worth it, with local ones it may be.
- **WhatsApp jobs are not moved to local models here.** That needs a 0-unsafe live eval on the
  local model first.
- **First real test (qwen3.5:4b on Ollama 0.31.2): not usable yet.** The model loads fully into
  video memory (3.1 GB), but each call took ~32 s and returned an empty answer: it spends the
  whole token budget on "thinking" and never writes the JSON. Next step: turn thinking off for
  the everyday model (Ollama's `think` / reasoning setting), read the answer from the right
  field, and retest after updating Ollama. Until then keep `chat` on the cloud provider.
- **Still open (not done in this change):** testing with real models (JSON reliability,
  speed, whether "thinking" output needs switching off for the small model), a spoken "one
  moment" before a model switch, `flins local setup`, a male Piper voice as the default
  suggestion, and updating Ollama (0.31.2 installed; 0.40.16 offered).

## JARVIS phase 2: memory (2026-11-11)
Plan: `docs/milestones/J2.md`.
- **Writes are explicit or confirmed, never silent.** The model can only fill a `remember`
  field; the core turns it into a yes/no question using the router's existing confirmation, and
  saves the exact text shown. This is the cautious reading of "learn preferences" + "don't
  blindly save every conversation".
- **Memory commands are regexes, not AI.** They run before the router's free-form step, so they
  work offline and can't be misread into something else. Trade-off: unusual phrasings ("could
  you hold on to the fact that…") fall through to conversation, where the model proposes it.
- **"Remember to …" is a reminder, not a memory.** FLINS says reminders don't exist yet instead
  of storing a to-do as a fact (phase 3).
- **An open question beats a memory command** ("What should I tell Rahul?" → "remember to bring
  the charger" is the message), but a pending yes/no doesn't block them.
- **Self-chat:** memory commands follow the same rule as conversation: only when addressed or
  while the conversation is open, so a note like "remember milk" isn't stored.
- **Secrets are refused by pattern** (password, OTP, CVV, PIN but not "pin code", API key,
  12–19 digit numbers). The database isn't a vault; this errs toward refusing.
- **Sensitive = health / legal / money** (the same patterns as the WhatsApp hard rule). Such
  memories are never in the always-included set; they reach the model only on a keyword match.
- **Retrieval is keyword scoring** (rarer shared words score higher) plus up to 6 standing
  high-importance preferences. No embeddings yet: one less dependency and nothing extra sent
  anywhere; the `embed` role is reserved.
- **Memories stay in the owner zone.** The WhatsApp pipeline never reads them (tested by a code
  scan). Whether contacts' replies may use any is a phase-6 decision.
- **Forgetting is a hard delete**, audited by id without content. The audit log never holds
  memory text.
- **Found by the live test (Groq):** (1) the model answered a stated preference with "I'll
  remember that", and the action-claim guard replaced the whole reply with "can't do that",
  losing the suggestion; now the suggestion question replaces the reply. (2) A forgotten fact
  was still visible in recent turns; the forget reply now says so. (3) Some chat calls took
  3–12 s with no error or fallback: Groq-side latency (likely free-tier rate limiting), not
  FLINS.
- Not done here: voice-friendly shortening of long memory lists, correcting a memory by voice
  in one step (use "forget that" + "remember that …", or the dashboard), automatic episodic
  memories from conversation summaries.

## JARVIS phase 1: assistant core (2026-11-11)
Plan and target architecture: `docs/JARVIS_ARCHITECTURE.md`.
- **The core wraps the router; it doesn't replace it.** `CommandRouter` keeps all command
  behaviour (and its tests). A `conversational` flag makes it return "not a command" instead of
  the help text, so the core can talk. Without the flag it behaves exactly as before.
- **Two model calls per spoken sentence** (intent on the small model, then chat): ~1.1–1.4 s on
  Groq in a live test. Merging them into one call would be faster but would tie command
  understanding to the chat prompt; left for the voice phase.
- **The conversation model gets no tools, and may not claim actions.** Checked twice: the model
  reports `claims_action`, and a regex catches "I've set / I'll remember / Done." even if it
  doesn't. A flagged reply is replaced, never shown or stored. An honest "I can't do that yet"
  (kind `cannot`) is kept.
- **Self-chat default is `addressed`.** The spec wants natural conversation everywhere, but the
  "Message yourself" chat is also a notes app; answering every note would be noisy and would
  store notes as conversation. Addressing FLINS by name opens a 3-minute window. Command replies
  don't open it. (Unaddressed notes still pass through the intent model, as they did since M6.)
- **Processes without `flins run` can't send.** `flins ask`, `flins chat` and the standalone
  dashboard build the core with `can_send=False` and no approvals: nothing may sit in the outbox
  and go out hours later.
- **All about_me facts are available to the conversation**, whatever their `share_with` (that
  field is about other people; here you are the one asking). They go to the `chat` provider;
  `assistant.use_about_me: false` turns that off, and a local model can be the `chat` role.
- **One conversation across channels**, split into sessions by a 30-minute quiet gap. A new
  session starts with the previous summary, not the old turns. Turns are pruned after
  `keep_days` (30) and `flins chat --clear` deletes everything.
- **Contact messages never enter the conversation context** in this phase. "What did Rahul
  say?" needs a read-only, neutralised view of WhatsApp data and is planned for phase 6.
- **Role routing is deliberately simple:** `reason` for messages over 280 characters or with a
  planning cue word; both configurable. Voice always uses `chat`.
- Known debt, unchanged here: `prompts/intent_v1.md` still names the user in its text.
- **Live check (Groq, on a database copy):** follow-ups resolved, a self-chat note was ignored,
  reminder and weather requests were declined honestly, a focus request asked for confirmation,
  a planning request used the reasoning route (4.9 s).

## 0.9.1: triage, times, Piper, pairing (2026-11-08)
- **Health/legal is a hard rule now, not only an AI judgement.** Live testing showed Groq
  missing "results came back, call when you can". Invariant 7 says such messages are NOTIFY
  only, so a deterministic list of specific phrases does it (bare "report" or "scan" are too
  common and left out). "Hospital", "ambulance" and "ICU" stay with the emergency rule, which
  alerts you and sends the urgent acknowledgement; flagging them as medical would have blocked
  that (the eval caught it).
- **Piper** comes from the voice extra (`piper-tts` 1.8, on the stack list). Voice models aren't
  bundled: `flins voice download` fetches one from Piper's official collection when you ask.
  Not tried with a real voice model during development, only with a fake synthesiser.
- **QR on the dashboard without a new dependency:** `qrcode-terminal` already contains a QR
  encoder. The sidecar writes the grid of squares to `data/pairing.json`; the dashboard draws
  inline SVG from it after checking its shape. Starting pairing needs the page token, like every
  other dashboard action.

## M9: earned autonomy, tone, backups, autostart, wake word (2026-11-08)
- **What counts toward earning auto replies:** only FLINS's AI drafts (not templates or skill
  replies). "Sent" = approved or edited; the 10% limit counts edits **and rejections** against
  all drafts you decided on (SPEC said only edits; rejections are a stronger "no", so the
  cautious reading counts them). Opt-in is per contact and never automatic.
- **Decisions keep their level.** An AUTO decision for a contact who hasn't earned it still shows
  AUTO in the trace, with a note "held for your approval: …", and goes to approval as before.
  Not lowering the level keeps the eval and existing traces unchanged.
- **No red-flag check, no unreviewed send.** If `drafting.redflag_check` is off or no AI is
  configured, earned contacts still get drafts for approval.
- **Inner tier stays at holding replies** (its ceiling in policy). A contact needs a tier
  whose ceiling allows AUTO (trusted by default) for earned replies; the CLI and dashboard say so.
- **Backups use `cryptography`** (you approved the dependency): AES-256-GCM, scrypt key from a
  passphrase, header authenticated, restore only into a new folder with an integrity check and
  zip-path checks. Chosen over Windows DPAPI so a backup survives a broken Windows install.
- **Wake word without openwakeword:** it needs `tflite-runtime`, which has no build for
  Python 3.12+, and has no "FLINS" model. A loudness-gated listener plus local Whisper `tiny`
  checks bursts for "FLINS" and sound-alikes. Tested with the Windows test voice and `small`:
  "FLINS, status" and "Hey FLINS, what did I miss?" were heard right. A false wake ("Flints are
  rocks") only produces "Sorry, I didn't get that"; anything that acts still asks for "yes".
- **Autostart** creates a logon task as you (`/RL LIMITED`, 30 s delay, minimised window). If
  Windows refuses, a `FLINS.cmd` in your Startup folder does the same.

## M8: Hermes and skill review (2026-11-08)
- **The dashboard token doesn't stop local programs.** Any program that can reach
  127.0.0.1:8765 can read the page, take the token and approve drafts or skills. WSL interop
  (`flins.exe` through `/mnt/c`) and the mounted `C:` drive (`.env`, `data/`) are the same kind
  of hole. So Hermes runs in WSL with drives off, interop off, default NAT networking (no
  route to Windows' 127.0.0.1) and only `skills/_incoming` mounted. `flins doctor` warns about
  `networkingMode=mirrored`. This is the cautious reading of invariant 9; SPEC §10 only said
  "in WSL2".
- **So the MCP server runs inside WSL** (a copy of the repo), over stdio, not as a Windows
  network service on `skilldev_port`. It never opens `data/` or your config, and its safety
  eval uses `config/policy.example.yaml`. The port setting stays unused for now.
- **Submissions are files** (`_incoming/_submissions/<name>.json`), not database rows: the
  WSL side has no database. They're Hermes' claims. The dashboard shows the summary marked as
  unchecked and re-runs validation, fixtures and (on approve) the eval with **your** policy.
- **Approval is tied to a hash.** The review page carries the hash of the files you saw.
  Approve refuses if the folder changed since (Hermes can edit the shared folder directly),
  re-checks after the eval, and checks the installed copy matches. Any edit through the tools
  withdraws a submission.
- **Requests** go to `_incoming/_requests/` (so the one shared folder is enough), not
  `skills/_requests/` as in SPEC §10.
- **The `mcp` SDK is 2.x** (`MCPServer`, formerly FastMCP). Refusals are raised as `ToolError`
  so Hermes sees the reason.
- Web search wasn't available while writing `docs/HERMES.md`, so Hermes-specific key names are
  marked as "check your Hermes version's MCP docs"; the command line given is what matters.

## M7: skills (2026-11-08)
- **Where skill replies are allowed (found by the eval gate).** With `holding_reply` first
  allowed to stand in for any draft, the eval found 16 cases above their allowed level. The skill
  ran when the AI was down (the message fell back to "unknown"), and its fixed reply went around
  the drafter's low-confidence and red-flag checks. The more cautious rules now are:
  - Skills run only on messages the AI actually classified.
  - A skill's reply can only stand in for a **fixed template** (AUTO_SAFE).
  - Drafts and AUTO replies are always written by the AI drafter with every check; there,
    skills contribute **facts** (`free_at`, `faq_answer`) that the drafter and the guard may use.

  Result: 0 unsafe with every starter skill on.
- **Imports: an allowlist, not a ban list.** SPEC §9.4 lists banned imports. The validator
  instead allows ~30 harmless standard modules (json, re, datetime, …) and the skill's own
  files. It also bans `eval/exec/open/__import__/getattr` and any `__dunder__` attribute.
  Network access is only `ctx.http.get` to listed hosts (https only), never a library.
- **Two walls at run time.** The core checks every `ctx` call and validates the output, so the
  child process isn't trusted. The child also has a Python audit hook (sockets, subprocesses,
  ctypes, sqlite, writes outside the run folder, reads outside the skill and Python's own files)
  and no site-packages. This isn't an OS sandbox: a Windows Job Object / WSL container comes
  later (SPEC §9.3). A run costs ~0.17 s.
- **Fixture tests instead of `tests/test_skill.py`.** Running a skill's own pytest file would
  execute unreviewed code outside the sandbox. `fixtures/*.json` (input, fake ctx data,
  expected reply/facts/denials) run through the real sandbox, and every reply must also pass
  the output guard.
- **`skills/skills.lock` is yours and git-ignored**, like policy.yaml. The starter skills ship
  without approval, so nothing new runs until you enable it. Enabling needs a person at an
  interactive terminal (typing the skill's name); the dashboard approval page comes in M8.
- **Not available yet:** `llm:complete` (AI calls from skills: refused at validation) and
  prompt-only skills. Calendar: one-off events from an `.ics` export; repeating events
  (RRULE) are skipped for now, and Google/Outlook sign-in comes later.
- **Time inside skills is local text** ("2026-11-08 14:00"). Time zones need the `tzdata`
  package, which skills can't load without site-packages.

## Voice: better with Indian English (2026-11-09)
From the first real session ("hier schon da", "10 roul", "tell, I should have…"):
- **Language fixed to English by default** (`voice.language: en`). Auto-detection on short clips
  picked German. Setting it also skips the detection pass, which made `small` *faster*
  (2 s -> 1.15 s on this laptop).
- **Accuracy presets** (`voice.accuracy`): fast = base / greedy; **balanced (default)** = small
  with beam search 5; best = medium. `stt_model` still overrides.
- **Contact names as hints.** Whisper gets the names of your close contacts and recent chats as a
  spelling prompt, and the intent model gets the same **first names (never numbers)** so it can
  map "roul" to Rahul. That's a small privacy change: those names now go to the AI provider with
  free-form voice/self-chat commands.
- **Sound-alike and phone-number matching** in contact lookup ("roul"/"raul" -> Rahul,
  "9666666666" -> the contact with that number).
- When FLINS doesn't understand, it says what it heard ("I heard '…'. Sorry…").
- **Follow-up questions are remembered** per source (voice / self-chat) for 60 s: the next
  words answer the question FLINS just asked. A clear fixed-phrase command ("stop", "status")
  still takes priority, and "no / never mind / forget it" cancels. Found in the second voice
  session: "What should I tell Radhika K?" -> "I'll call him back at 8" was treated as a new,
  unrecognised command.
- **Wrong guesses are caught by the read-back:** replaying the session with Groq, a misheard
  "Send each other a message…" was guessed as Meera, and "10 roul" as Arjun. Neither would be
  sent without your "yes", because the read-back names the contact.
- Measured with Windows' (American) test voice: balanced got 5/5 test commands exactly, including
  Rahul / Radhika / Meera, at ~1.15 s each; fast mangled two of the names. Real Indian-English
  accuracy can only be judged with your voice.

## M6: voice and natural-language commands (2026-11-09)
- **Voice is an optional install** (`uv sync --extra voice`): sounddevice, numpy, faster-whisper,
  pyttsx3, pynput. Not on the original stack list: `pyttsx3` (Windows voices, offline, nothing to
  download) instead of Piper for now, and `pynput` for the global hotkey. No `silero-vad` /
  PyTorch: faster-whisper's built-in VAD plus a simple loudness check is enough for push-to-talk.
- **Measured on this laptop (CPU, int8):** `small` ~2 s per command, `base` ~0.7 s with the same
  accuracy on test commands. Default is **`base`**, silence wait **0.9 s**, so speech end to answer
  is about 1.6 s plus the AI for free-form commands (~0.5 s on Groq): under the 2.5 s target.
  `small` stays available for better Hinglish.
- **CPU by default.** The laptop's NVIDIA GPU failed with "cublas64_12.dll not found" (CUDA
  libraries not installed). `stt_device: cuda` is an option; a CUDA failure falls back to CPU.
- **Tier B (free-form) only for voice and self-chat**, never the CLI, and only when no fixed
  phrase matches. It uses the triage providers unless an `intent` route is configured.
- **Everything Tier B would *do* is read back and waits for "yes"** (60 s): sending a message,
  changing or extending a mode, resume, approving or editing a draft. Exceptions: `stop`
  (always safe), and read-only answers (status, pending, summary). Rejecting a draft is also
  immediate, because it only prevents a send.
- **Messages you dictate are worded by FLINS:** "Hemanth asked me to tell you: …" plus the
  signature. They go through the outbox like approved drafts, so only to people who wrote within
  24 h (for now).
- **Found by the real-model checks:** Groq kept "call *her*" in a message *to* her, and passed
  "a couple of hours" through unparsed. The intent prompt now addresses messages to the recipient
  and normalises times. Whisper heard "Radhika" as "Radhi Ga"/"Radhiga", so contact matching
  ignores spaces and allows small misspellings, and fixed commands accept "approved number seven".

## M5: automatic holding replies, self-chat, summary (2026-11-09)
- **Only holding replies go out by themselves** (AUTO_SAFE: your third-person templates). Replies
  the AI writes still need your approval; per-contact unlock is M9. `auto_replies: off` turns
  automatic sending off without touching anything else.
- **Extra checks for automatic replies, right before sending:** the busy mode is still on and
  hasn't ended, **you haven't replied to that person yourself** since, at most one holding reply
  per chat per session (a session runs from leaving NORMAL, across stop/resume), the per-contact
  hourly cap, plus everything approved sends already check. Random 20–90 s delay first.
- **Leaving the mode cancels queued holding replies**; `stop` cancels everything except notes to you.
- **Self-chat notes** (command answers, approval prompts, urgent alerts, expiry warning, summary)
  start with "🤖". FLINS ignores self-chat messages with that prefix or that the outbox sent, so
  a note like "🤖 Back to normal" can never be read as a command. Notes skip the contact checks
  (it's you) and are delivered even while paused. Dry run still blocks them. `self_chat_notes:
  false` turns them off.
- **Summary** lists one line per message, urgent first: who, what they said (50 chars), and
  what happened (sent a holding reply / sent your approved reply / draft #n waiting / told you /
  ignored, plus "looked like a scam"). At most 10 lines in WhatsApp; the dashboard has the rest.
- **Debounce left at 25 s quiet / 90 s max.** No real-use data yet to tune it; it's a policy
  setting (`limits.debounce_*`).

## Replies that answer the message (2026-11-09)
- **Draft prompt v2** answers what the person said (questions, wishes, small talk, plans) and
  mentions availability only when it matters. v1 stays on disk; prompts now load by job name and
  the newest version wins (`draft` -> `draft_v2.md`).
- **`config/about_me.yaml`** is the only source of facts about the user, each with `share_with`
  (everyone / known / trusted / inner). The example shares nothing: every fact is opt-in.
  General knowledge ("what's the full form of CPU?") is allowed; anything about the user's life
  that isn't in the file is not.
- **Found with real Groq drafts:** the model wrote as if it were the user ("I'm travelling",
  "main kaam kar raha hoon"), accepted birthday wishes as the user, and once said "he's free now"
  during working mode. Fixes: prompt rules for speaking as FLINS in the third person, plus two
  deterministic guard checks:
  - **writing as the user** (English and Hindi first-person markers): held back as a draft for
    approval. "I'll pass it on" / "I'm FLINS" are fine. Templates are exempt because they're the
    user's own words.
  - **claiming the user is free while a busy mode is on**: straight to NOTIFY.
- Recorded eval answers now match a job by name, so tests and evals don't depend on a prompt
  version.
- **Note:** contacts in the `inner` tier still get only the fixed holding reply for small talk
  while in working mode (policy: inner = AUTO_SAFE). Set someone to `trusted` for real answers.

## M4: approvals and sending (2026-11-09)
- **Only drafts you approve are sent.** Automatic replies (AUTO / AUTO_SAFE) still only show as
  "would send" until M5, as SPEC §14 plans. Each draft gets a daily number (#1, #2, ...).
- **Ways to approve:** dashboard buttons (Approve / Edit / Reject), or the self-chat: `y3`,
  `n3`, `e3 <your text>`, `pending`, or plain `send it` when exactly one draft is waiting.
- **The outbox is the only sender** (invariant 2, tested by a code scan). Right before each send
  it re-checks from the database: not paused, dry run off, the contact wrote within 24 h, the
  hourly/daily caps, one send per approval. A row is marked before the send call, so a crash
  mid-send never causes a second attempt.
- **Kill switch:** pausing cancels every queued message at once (the in-process event, measured
  < 1 s in tests), and the worker re-reads the mode before each send, so `flins stop` from another
  terminal also works.
- **Approved texts get the disclosure signature** ("— FLINS, Hemanth's assistant") unless the
  contact has disclosure off. Text you edit is sent exactly as typed, without a signature.
  Your edits are saved as tone examples for later.
- **Approvals expire after 30 min** (`approvals.expiry_minutes`). A stale "I'm busy till 18:00"
  is worse than none. Drafts made before M4 have no approval and can't be sent.
- **The sidecar has one send path:** plain text only (no media, no link previews), never to
  groups, broadcasts or channels, refuses duplicate ids, shows "typing…" for up to 4 s first.
- **Fix found by the tests:** command parsing dropped trailing "!", "?", "please" and "thanks",
  which would have changed edited texts. Edited text is now kept exactly as typed.
- **Deferred to M5:** FLINS's answers to self-chat commands are still printed in the terminal
  rather than sent back into the self-chat.

## Multiple AI providers (2026-11-09)
- **Supported:** Anthropic (native SDK) plus any OpenAI-compatible chat API through one adapter:
  Gemini, Groq, NVIDIA, OpenAI, OpenRouter, and local Ollama built in; others can be added in
  `flins.yaml` with `base_url` + `key_env`. Endpoints were checked against each provider's docs
  on 2026-11-09 (Groq `api.groq.com/openai/v1`, Gemini
  `generativelanguage.googleapis.com/v1beta/openai`, NVIDIA `integrate.api.nvidia.com/v1`).
- **Raw HTTP (`httpx`), not the `openai` package.** One small adapter covers every provider and
  avoids another SDK. `httpx` moved from a test-only dependency to a runtime one.
- **Per-job fallback chains** (`llm.roles.triage|draft|redflag` in flins.yaml): providers without
  a key are skipped; errors, timeouts, cut-off answers and invalid JSON fall through to the next.
  Each attempt is metered with its provider.
- **Refusals are never routed to another provider** (same rule as M3).
- **JSON handling per provider:** `json_schema` where supported (Gemini, Groq, OpenAI,
  OpenRouter), `json_object` for Ollama, and schema-in-prompt for NVIDIA, whose API reference
  doesn't document `response_format`. Fenced or chatty JSON is unwrapped. Every answer is still
  validated by the same strict Pydantic models, and drafts still go through the output guard
  and red flag, whatever the provider.
- **Thinking models get room:** Gemini and Groq calls use at least 4096 output tokens, so
  reasoning doesn't cut the JSON off. `reasoning_effort` is sent where supported.
- **Budget:** prices are known for Claude and Groq's gpt-oss models. Others count as $0 unless
  `price_in` / `price_out` are set on the route, and `flins llm status` shows this.
- **Privacy:** every provider you add receives the same minimal, name-only context as Claude did.
  Messages that rules already cap at NOTIFY still go to no provider at all.
- **Console output is UTF-8.** Found while testing: a Windows console with output redirected
  crashed on "≈", and WhatsApp emoji would have done the same.

## M3 (2026-11-09)
- **Dependencies added** (all on the CLAUDE.md stack list, or required by it): `anthropic`
  (SDK 2.6), `fastapi`, `uvicorn`, `jinja2`, `python-multipart` (FastAPI's form parser);
  `httpx` for FastAPI's test client (dev only).
- **Models:** triage and red flag use `claude-haiku-5-5` at effort `low`; drafting uses
  `claude-sonnet-5-5` at effort `medium`. Thinking can't be switched off on these models, so
  effort is the speed/cost lever. Prices are in `llm/gateway.py` for the budget.
- **Structured output** via `output_config.format` (JSON schema), validated again with strict
  Pydantic models. Anything off-schema, cut off, timed out or refused → `LLMError` → the
  cautious path.
- **No refusal fallback model.** If Claude declines to triage or draft, FLINS tells you
  (NOTIFY) instead of retrying the message on another model. For a personal assistant, a
  declined reply is a signal, not something to route around.
- **No LLM call when the rules already cap at NOTIFY** (normal mode, paused, unknown sender,
  money, OTP, groups, media-only). Saves cost and keeps the most sensitive messages off the API.
- **Contacts reach the LLM by name only.** No phone numbers or WhatsApp IDs, and `<`/`>` in
  message text are replaced so a message can't break out of `<untrusted_thread>`.
- **Guard failures step down one level** (AUTO → DRAFT, DRAFT/AUTO_SAFE → NOTIFY). Pretending to
  be human, injection echoes and very low confidence go straight to NOTIFY. The red flag check
  only runs for AUTO (unreviewed) sends, and its veto or failure means DRAFT.
- **New hard rule `media_only`.** Found by the eval: a fooled triage could label a voice note
  "small talk" and FLINS would have auto-replied to audio it can't hear. A message without
  text is now structurally capped at NOTIFY.
- **Unsaved contacts show as "~Name"** (like WhatsApp) with "not in your contacts". Found on the
  dashboard: the OTP scammer had named themselves "Hemanth".
- **Eval design:** recorded LLM answers include deliberately fooled, leaky, lying and failing
  answers. A drafter that invents a plausible plain-text fact *and* lies about it *and* fools
  the red flag can't be caught deterministically, so it isn't in the recorded set. `flins eval
  --live` measures it against the real models.
- **Debounce:** quiet 25 s / max 90 s (policy). Emergency keywords skip the wait. Bursts still
  waiting when FLINS stops are stored but undecided.
- **Dashboard:** server-rendered pages, no JavaScript, strict CSP, Host-header check,
  per-run token on every write. Times are shown in UTC for now.
- **Deferred:** the pairing QR in the dashboard (M1 leftover), approval actions (M4),
  local-time display.

## M2 (2026-11-09)
- **Added `tzdata`.** Windows has no built-in timezone database, and "till 6pm" has to mean 6pm
  in `policy.user.timezone`. It's the official data package `zoneinfo` uses.
- **Modes last at most 12 hours.** SPEC says autonomy must always end but gives no upper bound.
  12 h covers a long working day; anything longer must be re-entered.
- **Hard rules: all are checked and the lowest cap wins** (SPEC says "first match wins"). For
  example, "urgent, send money" is both money (NOTIFY) and emergency (escalate). Taking the
  minimum keeps it at NOTIFY and still escalates. This is never less safe than first-match.
- **The category is the most cautious of all candidates** (hard-rule category, triage category,
  structural fallback). A friendly triage label can't hide a money keyword.
- **Built-in money, credential and emergency keywords** are always checked, on top of
  `policy.yaml`, so emptying the lists in config can't switch the safety net off. Accepted
  false positives: "pin the location" → credentials (NOTIFY), "transfer the file" → money
  (NOTIFY), "not urgent" → escalate. All err on the cautious side.
- **New category `group_mention` (NOTIFY)** for an @mention in an allow-listed group. Without
  it, the general `group_message: IGNORE` swallowed those mentions.
- **AUTO_SAFE without a usable template becomes NOTIFY**, never a free-form reply.
- **Commands must be the whole message** (with an optional "FLINS," and "please"), so notes to
  self like "I'm leaving at 5 for the airport" never change modes. A time that can't be parsed
  is an error message, never a silent default.
- **PAUSED answers "resume" only.** "working mood" while paused is refused with "Say 'resume'
  first", so a stray message can't undo the kill switch.
- **Cancel** works within 10 s of a change and only from a timed mode. You can't "cancel" your
  way out of NORMAL or PAUSED back into autonomy.
- **Only live self-chat messages are commands.** Self-chat history synced from the phone
  (`append`) is never executed.
- **The exit summary covers the whole session**, from the last time FLINS left NORMAL, so a
  stop/resume in between doesn't drop the earlier messages.
- **The mode is shared through SQLite.** `ModeManager` uses its own connection and
  `BEGIN IMMEDIATE`. `flins run` re-reads the mode before every decision and every second, so
  `flins stop` from another terminal takes effect immediately.
- **Deferred:** replies to self-chat commands are printed where FLINS runs, because sending them
  back into WhatsApp needs the M4 outbox. Cancelling queued sends on stop also comes with the
  M4 outbox. Each message is its own burst for now; debouncing arrives with the M3 pipeline.
  The fast-reply loop check reads `outbox.not_before` until M4 adds a real `sent_at`.

## M1 (2026-11-07)
- **Baileys `7.0.0-rc14`, pinned exactly.** It's npm's `latest` tag. Versions before rc12 /
  6.7.22 have a message-spoofing vulnerability (GHSA-qvv5-jq5g-4cgg). v7 is still labelled
  "rc", so read the release notes before upgrading.
- **Added `websockets` (Python)** for the core ↔ sidecar link. It's the WebSocket library FastAPI
  and uvicorn already use, so it isn't a new kind of dependency.
- **Frame signing signs the exact body string**, not re-serialised JSON, so Python and Node never
  disagree about key order or escaping. Shared vectors in `tests/fixtures/protocol_vectors.json`
  keep the two implementations honest.
- **Phone number vs LID:** the contact key is the phone-number JID when known, otherwise the LID.
  If a person ends up with two rows (one per identity) before the mapping is known, ingest
  merges them **only if at least one row has nothing the user set** (tier other than
  unknown/known, notes, max level, tone, language, allowed categories, never-mention, or tone
  examples). The edited row survives, and stored messages are re-pointed to the phone number.
  If both rows were edited, nothing is merged and a `contacts.merge_needed` audit row is written
  for the M3 dashboard. (Revised after the first real pairing: the original "never auto-merge"
  rule produced 12 false flags, all for pairs of untouched rows.)
- **Contacts are created only from the address book or from a real message.** WhatsApp sends
  a large ID-mapping list on pairing (about 1,000 entries, mostly group members). Nameless
  entries now only fill in details on existing contacts. Migration `0003` removed the rows the
  first version had created, under strict rules (automatic, unknown, nameless, nothing set by
  the user, no messages). A backup was taken first in `data/backups/`.
- **Automatic tier change, one case only:** `unknown` → `known` when a person shows up in the
  address book. Ingest never touches any other tier.
- **History sync is used for contacts only.** Old messages aren't imported, so nothing from
  before FLINS was running can trigger anything later.
- **`markOnlineOnConnect: false`, no read receipts.** The phone keeps getting notifications and
  contacts don't see blue ticks for messages you haven't read.
- **The sidecar stops instead of retrying on `connectionReplaced` (440)**, so it never fights
  another WhatsApp Web client (for example a Hermes gateway) for the session.
- **A brief "busy" from the sidecar is retried up to 5 times.** A previous connection can still
  be closing when the core reconnects.
- **Pairing QR is terminal-only for now.** The dashboard arrives in M3.

## M0 (2026-11-07)
- **Python ≥3.12, running on 3.13.** SPEC says 3.12, but 3.13 is what's installed locally and
  everything we use supports it. `requires-python = ">=3.12"`, `.python-version = 3.13`.
- **argparse for the CLI** instead of Typer/Click. It's stdlib, so no new dependency (CLAUDE.md:
  ask before adding dependencies).
- **Repo root is `Documents\Flins`** and has its own git repo. The user's home folder is also a
  git repo, so a nested repo keeps FLINS history separate.
- **Unsafe policy values are clamped, not rejected** (money/OTP/sensitive/conflict categories and
  flags → NOTIFY, PAUSED → IGNORE). Each clamp produces a warning shown by `flins doctor`. Missing
  hard categories get added at NOTIFY. Structural mistakes (missing modes, missing or zero
  durations, unknown levels, unknown keys, bad regexes, unknown templates) are rejected outright.
- **Policy keys are strict** (`extra="forbid"`): a typo like `per_contact_max_auto_per_hr`
  fails loudly instead of silently falling back to a default.
- **Audit log is append-only at the database level** through a `BEFORE UPDATE` trigger. DELETE
  stays allowed so retention can purge old rows.
- **The event bus never logs event data**, only the topic, because data may hold message text
  (invariant 11).
- `flins.yaml` and `policy.yaml` are git-ignored. Only the `*.example.yaml` files are committed.
