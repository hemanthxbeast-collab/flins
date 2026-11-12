# FLINS → JARVIS: architecture and migration plan

Written after inspecting the repository at 0.9.1 (`f1198ab`): all of `src/flins`, the sidecar,
prompts, config, migrations 0001–0010, tests (694 Python, 11 Node), the 162-case safety eval, CI
and the git history. `docs/SPEC.md` stays the source of truth for the WhatsApp safety model.
This document describes how FLINS grows from a messaging assistant into one personal assistant
**without a rewrite**.

**Status:** built: phase 1 (assistant core, 0.10.0), phase 2 (memory, 0.11.0), phase L (local models, 0.12.0) and phase 3 (tasks and reminders 0.13.0, planning 0.14.0; [milestones/J3.md](milestones/J3.md), [J3b.md](milestones/J3b.md)) and phase 4 (tools 0.15.0, online tools 0.16.0; [J4.md](milestones/J4.md), [J4b.md](milestones/J4b.md)) and phase 5 (conversational voice, 0.17.0; [J5.md](milestones/J5.md)). Next: phase 6 (WhatsApp autonomy).

## 1. Where FLINS is today

```
                    ┌────────────── owner channels ──────────────┐
 CLI (flins mode)   voice (hotkey / wake word)   self-chat   dashboard
        │                    │                      │            │
        └────────────► CommandRouter ◄──────────────┘            │ (forms)
                        │  Tier A grammar (no AI)                │
                        │  Tier B intent (LLM) + confirm         │
                        ▼                                        ▼
                  ModeManager   ApprovalService   Outbox ◄── approvals
                        ▲
 WhatsApp sidecar ─► Ingestor ─► Pipeline: hard rules → triage → decide() → draft → guard
 (Baileys, HMAC)                 → red flag → approval / earned auto → Outbox ─► adapter.send_text
                                   └ skills (sandboxed subprocess, propose only)
 Everything writes audit_log; llm/gateway.py is the only door to a model.
```

### What already exists (and stays)
| Spec area | Already in FLINS |
|---|---|
| Modes (§9, §10) | FSM with expiry: NORMAL / WORKING / FOCUS / AWAY / PAUSED, profiles, restart-safe |
| WhatsApp as an adapter (§11) | Sidecar + `BaileysAdapter`; the pipeline decides, the adapter only sends |
| Contact trust (§12) | Tiers, per-contact max level, allowed categories, never-mention, tone |
| Earned autonomy (§13) | Per-contact opt-in + ≥20 accepted drafts + ≤10% changed; safety overrides it |
| Approvals (§14) | Expiring approvals by dashboard, self-chat (`y3`), voice; all audited |
| Voice (§15) | Push-to-talk, Whisper tuned for Indian English (large model on the GPU), SAPI / Piper, follow-up listening, barge-in, wake word confirmed by two models |
| Skills (§19, §21) | Manifest permissions, subprocess sandbox, hash-pinned approval, no send permission |
| Hermes (§20) | `flins skilldev` MCP server, review page, generated → validated → tested → approved |
| Kill switch (§29) | "stop" by voice / self-chat / CLI / dashboard → PAUSED, queued sends cancelled |
| Model abstraction (§31) | `llm/gateway.py` + providers (Anthropic, Groq, Gemini, NVIDIA, OpenAI-compatible, Ollama), per-job fallback chains, cost metering, daily budget |
| Security (§28) | Secrets only in `.env`, encrypted backups, audit log, dashboard token + Host check |
| Tests (§35) | 694 tests, 162-case eval gate (0 unsafe), replay harness, sandbox escape tests |

### What is missing
1. **No conversation.** Anything that isn't a command gets "Sorry, I didn't get that". There is
   no dialogue state beyond a 60 s follow-up question.
2. **No single core.** The router handles commands; the pipeline handles contacts; the dashboard
   has its own form handlers. Intelligence can't be added in one place.
3. **No memory.** `about_me.yaml` (facts you wrote) and `tone_examples` (your edits) are the
   only things FLINS knows. Nothing is remembered from talking to it.
4. **No tasks, reminders, planning or proactive behaviour** beyond mode-expiry warnings.
5. **Tools are WhatsApp-shaped.** Skills trigger on message categories; there is no way for the
   owner to *ask* for a tool ("what's the weather", "find the file").
6. **Model roles are fixed** to triage / draft / redflag (+ intent borrowing triage).
7. Small debts: `prompts/intent_v1.md` names the user in its text; statuses are spread over
   several CLI commands; no metrics view.

## 2. Target architecture

One core that every **owner** interaction passes through, built around the pieces that already
work. Nothing below is replaced; the boxes marked ★ are new.

```
 voice ─┐                                                     ┌─ voice (TTS)
 self-chat ─┤                                                 ├─ self-chat note
 CLI ───────┼─► Input adapter ─► ★ Assistant core ─► Output ──┼─ CLI
 dashboard ─┘        (Request)        │      (Reply)          └─ dashboard
                                      │
        1 safety commands (Tier A grammar: stop / resume / status … no AI, always first)
        2 ★ context builder  (identity, profile, mode, pending, memories, summary, recent turns)
        3 intent understanding (Tier B, cheap model)  ──► command executor (existing router:
        4 policy / permission check (existing engine)       modes, approvals, send-with-confirm)
        5 ★ planner (phase 3)  ─► ★ tool/skill selection (phase 4) ─► sandboxed execution
        6 ★ conversation engine (chat / reasoning model, no tools of its own)
        7 result validation (reply guard: no claimed actions that didn't happen)
        8 ★ memory update (conversation turns now; long-term memory in phase 2)
        9 audit + metrics

 contacts ─► WhatsApp adapter ─► Ingestor ─► Pipeline (unchanged safety path) ─► Outbox
                                                 ▲ shares modes, policy, memory (read), skills
```

### The twelve concerns and where they live
| Concern | Home |
|---|---|
| Identity / personality | `assistant:` in `flins.yaml` (name, style, humour, notes) + `prompts/chat_v*.md` |
| Personal context | `policy.user`, `about_me.yaml` now; memory store from phase 2. Never in code |
| Conversation | `assistant/conversation.py`: sessions + turns in SQLite, rolling summary |
| Memory | `assistant/memory.py`: `MemoryStore` interface (phase 1), real store (phase 2) |
| State | `ModeManager`, approvals, outbox, tasks (phase 3) |
| Intent | Tier A grammar, then `IntentParser` (Tier B) |
| Planning | `assistant/plans.py` (phase 3b): a plan is ordered tasks, saved on your yes. Tool steps with input/output/retry and approval gates come with phase 4 |
| Tool selection | `assistant/tools/`: the chat model asks for a tool by name, the core checks and runs it (phase 4a). Online tools (weather, Wikipedia, GitHub) go through one fetcher with a fixed host list (phase 4b). Owner-callable sandboxed skills later |
| Execution | Command executor (router) now; skill sandbox for tools |
| Permissions / safety | Policy engine, capability scopes on skills, approvals, kill switch |
| Communication | Output adapters: TTS, self-chat notifier, CLI, dashboard |
| Observability | `audit_log`, `llm_calls`, `assistant.turn` rows; metrics view (phase 9) |

### Two trust zones (unchanged principle)
- **Owner zone**: voice, CLI, dashboard, and self-chat messages *you* typed. The assistant core
  serves these. Actions still confirm before they send or change a mode.
- **Contact zone**: everything other people write. It stays untrusted data inside
  `<untrusted_thread>`, handled only by the pipeline, with the policy engine deciding the level
  and the Outbox as the only way to send. The conversation model never gets a tool that sends.

When owner questions need contact data ("what did Rahul say?", phase 6), it enters the core as
quoted, neutralised data from a read-only capability, never as instructions.

### Model roles
`triage`, `draft`, `redflag` stay required. New optional roles borrow routes when not configured:

| Role | Used for | Falls back to |
|---|---|---|
| `intent` | command understanding | `triage` |
| `chat` | ordinary conversation, voice answers | `draft` |
| `reason` | long or planning-style requests | `chat` → `draft` |
| `summary` | conversation summaries | `triage` |
| `embed` (phase 2) | memory retrieval | (off: keyword retrieval) |

Routing is config, not code: `llm.roles.<role>` lists providers in order, as today.

### Context budget
The model never gets the whole history. Each turn is built from: persona, profile facts, time,
mode, pending approvals, relevant memories (phase 2), the session summary, and the last N turns.
Older turns are folded into the summary by the cheap `summary` model.

## 3. Migration plan (phases)
Each phase: inspect → smallest compatible change → tests → full suite + eval → docs → commit.

| Phase | Deliverable | Builds on | Main risk |
|---|---|---|---|
| **1 Core** | `flins.assistant`: core, context builder, conversation store, chat engine, memory/mode interfaces, model roles, persona config. `flins ask`, `flins chat`, dashboard Chat. Voice and self-chat go through the core | router, gateway | chatty self-chat; a model claiming it did something |
| **2 Memory** | `memories` table (type, content, source, importance, confidence, expiry, privacy), remember / forget / "what do you remember", "why do you know that", importance scoring, memory page | phase 1 interface | storing too much; wrong memories. Writes are explicit or confirmed |
| **L Local models** (added 2026-11-11, next) | FLINS answers from models running on this laptop: a small everyday / voice model, a reasoning model and a coding model, one in video memory at a time, with the cloud as optional fallback. Details in §6 | gateway roles, Ollama provider | small models are weaker at strict JSON and at safety judgement; swap delay |
| **3 Tasks + planning** | tasks, reminders, recurring, scheduler, plan steps with approval gates | modes ticker | reminders firing during FOCUS; interrupt policy |
| **4 Tools** | owner-callable skills (calculator, weather, files in approved folders, search, GitHub, notes), capability scopes with risk levels | skill sandbox | scope creep; every scope explicit, dangerous ones need approval |
| **5 Voice** | conversational voice, barge-in, better wake word | voice loop | false wakes |
| **6 WhatsApp autonomy** | more modes (DND, SLEEP, TRAVEL, custom), per-contact topic permissions, HOLD, "what did X say / reply to him" | pipeline, autonomy | most safety-sensitive phase; eval grows first |
| **7 Proactive** | briefings, deadline nudges, end-of-day summary, interrupt policy | scheduler | spam; hard caps |
| **8 Hermes** | "no skill for that → request one", permission analysis in review | skilldev | generated code; review flow stays mandatory |
| **9 Polish** | home / status / activity / memory browser / automations | dashboard | none |

## 4. Risks and how they're handled
| Risk | Mitigation |
|---|---|
| The assistant says it did something it can't do | Chat model has no tools; replies that claim actions are replaced with an honest "can't do that yet" (model self-report + regex backstop) |
| Kill switch depends on an LLM | Tier A grammar always runs first; "stop" never touches a model (tested) |
| Self-chat is also your notes app | FLINS only chats there when addressed ("FLINS, …") or while a conversation is open (3 min); configurable |
| Private facts sent to a provider | Same providers as drafting; `assistant.use_about_me: false` turns it off; a local model can be the `chat` role |
| Conversation storage grows forever | Turns pruned after `assistant.keep_days` (30); `flins chat --clear` deletes them |
| Prompt injection from contacts reaching the core | Contact text never enters the conversation context in phase 1; later only as neutralised quoted data |
| One huge diff | One phase per commit; the eval gate (0 unsafe) and all existing tests must stay green |
| Cost / latency | Cheap model for intent and summaries, `chat` for talk, `reason` only on long/planning requests; daily budget applies to everything |

## 5. Phase 1 in detail
New package `src/flins/assistant/`:

| File | Purpose |
|---|---|
| `core.py` | `Assistant.handle(text, source) -> Reply`: the pipeline in §2, with a stage trace per turn |
| `context.py` | `ContextBuilder`: builds the model input from typed blocks, everything neutralised |
| `conversation.py` | `ConversationStore`: sessions, turns, summary pointer, pruning, clear |
| `chat.py` | `ChatEngine` (reply, kind, claims_action) and `Summariser`; prompts in `prompts/` |
| `memory.py` | `MemoryStore` protocol, `ProfileMemory` (reads `about_me.yaml`), `NoMemory` |
| `models.py` | `Request`, `Reply`, `Turn`, `MemoryItem` |

Changes to existing code: `CommandRouter(conversational=True)` reports "not a command" instead
of the help text and understands free-form commands on every owner channel; `gateway.json(...,
role=)` and role fallbacks in `providers.py`; `AssistantConfig` in settings; migration 0011;
`flins ask` / `flins chat`; dashboard Chat page; `flins run` hands the core to voice, self-chat
and the dashboard.

Not in phase 1: long-term memory, tasks, tools, contact questions, new modes.

## 6. Phase L: local models (added 2026-11-11)
Why: cloud replies are sometimes slow (3–12 s seen on Groq's free tier) and paid APIs cost
money. Running models locally also keeps your conversation on this PC.

### The laptop
Intel i7-14650HX, 16 GB RAM, RTX 4060 Laptop with 8 GB video memory (about 1.4 GB already used
by Windows), 261 GB free on C:. Ollama 0.31.2 is installed with no models yet. FLINS already has
an `ollama` provider, so local models plug into the existing roles.

Rule of thumb: a model's file must fit in the ~6.5 GB of free video memory to run fully on the
GPU. Only **one** language model is kept loaded at a time.

### Models (checked on ollama.com on 2026-11-11; the picks can change in config)
| Job | Role(s) | Model | Download | Why |
|---|---|---|---|---|
| Everyday talk and **voice** answers, understanding commands, summaries | `chat`, `intent`, `summary` | `qwen3.5:4b` | 3.3 GB | current small general model; fast; leaves video memory free |
| Logical reasoning, planning, maths | `reason` | `qwen3.5:9b` | 6.6 GB | strongest that still fits (tight: it nearly fills the card). Fallback pick: `deepseek-r1:8b` |
| Coding questions | `code` (new role) | `qwen2.5-coder:7b` | 4.7 GB | the dedicated coding model in this size |
| Even lighter option for voice | `chat` | `qwen3.5:2b` | 2.7 GB | if 4b feels slow |

Total for the first three: about 14.6 GB.

### Voice: what is and isn't a language model
- **Listening** is Whisper (speech-to-text), already local. It runs on the CPU and uses RAM
  (about 1 GB), not video memory, so it doesn't compete with the language models.
- **Speaking** is Piper (text-to-speech), already supported, on the CPU. A **male voice** is a
  Piper voice choice (e.g. `en_US-ryan-medium` or `en_GB-alan-medium`; `flins voice download
  --list` shows what exists), set as `voice.piper_voice`.
- **Thinking** about what you said is the small `chat` model above.
So a spoken reasoning question goes: Whisper hears it → FLINS sees it's a reasoning question →
the reasoning model is loaded (the small model is unloaded first) → Piper speaks the answer.

### How it will work
1. **Task routing.** The core already picks `chat` or `reason`. It gains `code` (detected from
   code fences, error messages, language and tool names). Voice currently always uses `chat`;
   with local models a spoken reasoning or coding question may use the bigger model, after
   saying "one moment" so the wait isn't silent.
2. **One model in video memory.** A `LocalModels` manager asks Ollama what is loaded and unloads
   the others before a different model is used, and keeps the everyday model warm otherwise.
   Switching costs a few seconds (loading from disk), so FLINS switches only when the task
   needs it and switches back lazily.
3. **Local first, cloud optional.** Each role lists providers in order, as today: put `ollama`
   first and a cloud provider after it (fallback when the local model fails or returns unusable
   output), or leave the cloud out entirely.
4. **Strict JSON.** FLINS needs structured answers. Ollama supports schema-constrained output,
   which the provider will use instead of free-form JSON, and "thinking" text is kept out of
   the answer.
5. **Commands:** `flins local status` (installed models, what's loaded, memory use),
   `flins local setup` (writes the role list; downloads only with your approval), plus the
   existing `flins llm test`.

### Status (2026-11-11)
**Working in 0.12.0** (measured results in docs/DECISIONS.md): conversation, commands, summaries, reasoning and coding run locally. `reason` uses the 4B model with thinking on instead of the 9B model (which took 152 s per puzzle). Open: male Piper voice, `flins local setup`, one combined intent+chat call, the local safety eval.

Earlier, built in 0.11.1: the `code` role, task routing, one-model-at-a-time unloading, schema-constrained Ollama output, `flins local`. Not done: verification with real models, `flins local setup`, the spoken "one moment", moving any WhatsApp job.

### Safety
- WhatsApp jobs (`triage`, `draft`, `redflag`) stay on your current provider at first. They move
  to a local model only if `flins eval --live --provider ollama` shows 0 unsafe. The hard
  rules, output guard and approvals apply whichever model is used.
- A smaller model will sometimes be wrong or vague. FLINS's rules don't depend on the model
  being right: it still can't act or claim actions, and memory still needs your yes.

### Risks
| Risk | Handling |
|---|---|
| The 9B model nearly fills the card and spills to the CPU (slow) | measure in setup; fall back to `deepseek-r1:8b` or the 4b model in thinking mode |
| Switching models takes seconds | route to the big models only when needed; "one moment" on voice |
| 16 GB RAM is shared with Whisper, the sidecar and your apps | one language model at a time; unload on idle |
| Small models break JSON more often | schema-constrained output; cloud fallback if configured |
| Ollama 0.31.2 may be too old for the newest models | update Ollama first (0.40.16 is offered) |

