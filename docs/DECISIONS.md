# Decisions log

Short records of choices made while building. Newest first.

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
