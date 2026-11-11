# FLINS

Local-first personal voice assistant that can handle WhatsApp for you while you work.
Full design: [docs/SPEC.md](docs/SPEC.md). Build instructions for Claude Code: [CLAUDE.md](CLAUDE.md).

**Status:** 0.10.0. FLINS handles WhatsApp while you're busy (holding replies by itself, real
answers when you approve), and you can **talk to it** like an assistant: by voice, in your
"Message yourself" chat, in a terminal, or on the dashboard. It is growing into a full personal
assistant in phases: [docs/JARVIS_ARCHITECTURE.md](docs/JARVIS_ARCHITECTURE.md) (phase 1 of 9 done).

## Talk to FLINS
```powershell
uv run flins ask "what's 18% of 2400?"     # one question
uv run flins chat                          # a conversation in the terminal
uv run flins chat --history                # what's stored;  --clear deletes it
```
With `flins run` going, the same conversation continues by voice, on the dashboard's **Chat** page,
and in your self-chat. It follows up naturally ("and divide that by 8?"), and the built-in
commands still work in plain words ("I need to focus for two hours" → it asks, you say yes).

- **In your self-chat, start with its name** ("FLINS, how do I…"). That chat is also where you keep
  notes, so FLINS stays quiet unless you address it, and then keeps listening for 3 minutes.
  Change this with `assistant.self_chat` in `config/flins.yaml`.
- **What it can't do yet, it says so.** No reminders, tasks, memory, files or live data until
  the later phases. It has no tools in conversation, and a reply that claims it did something is
  replaced with an honest one.
- **"stop" always works**, with or without an AI model.
- Personality (name, concise/detailed, humour) is under `assistant:` in `config/flins.yaml`.
  Conversation is stored only on this PC and deleted after 30 days.

## Voice
```powershell
uv sync --extra voice           # once
uv run flins voice test         # checks the mic and speech model, shows the timing
uv run flins run --voice        # FLINS + push-to-talk
```
Press **Ctrl+Alt+Space**, wait for the beep, speak. It stops listening when you stop talking.
Examples: *"I'm in working mood till 6"*, *"what did I miss?"*, *"approve number three"*,
*"tell Radhika I'll call her at 8"*, *"stop"*. Anything that would send a message or change the
mode is read back first; say **yes** or **no**. The same plain-language commands work typed in your
self-chat. Settings live under `voice:` in `config/flins.yaml` (model, voice, hotkey, alerts).

If it mishears you: speak right after the beep, a little slower, and say names clearly. FLINS
repeats what it heard when it can't follow. `accuracy: best` uses a bigger model (slower, more
accurate); `accuracy: fast` is quickest. It knows your contacts' names, so "tell Rahul…" works
better than "tell my friend…".

## Sending
Sending stays off until you turn it on yourself in `config/policy.yaml`:
```yaml
dry_run: false
```
Then restart `flins run`. Approve a draft on the dashboard (**Approve & send**, **Edit & send**,
**Reject**) or from your WhatsApp "Message yourself" chat:

| Type | Does |
|---|---|
| `y3` | send draft #3 |
| `e3 Not now, call you at 8!` | send your own words instead |
| `n3` | drop draft #3 |
| `pending` | list what's waiting |

Drafts expire after 30 minutes. `stop` cancels anything queued instantly.

**Automatic holding replies** (`auto_replies: holding_only` in `policy.yaml`, or `off`): in
working / focus / away mode, contacts your policy allows (by default **inner** for anything
small-talk-like and **trusted** for "where are you?" / "reached?" and urgent acknowledgements) get
your template, e.g.
*"Hey Mom! Hemanth is busy with work right now and will get back to you after 18:00."* It's sent
once per chat per session, 20–90 s after their message, and never if you've already replied
yourself. `I'm done` or `stop` cancels anything still waiting.

**In your self-chat** FLINS answers your commands, asks before sending drafts
(`#3 Kavya: "…" Draft: "…" Reply y3 to send…`), warns you about urgent messages, and sends a
summary when you say `I'm done`. Its notes start with 🤖.

## Requirements
- Windows 11, Python ≥ 3.12, [uv](https://docs.astral.sh/uv/), Node.js ≥ 20, Git

## Setup
```powershell
uv sync
npm --prefix wa-sidecar install
npm --prefix wa-sidecar run build
copy .env.example .env                              # then set FLINS_WA_SECRET (see below)
copy config\policy.example.yaml config\policy.yaml  # optional: edit your policy
uv run flins doctor
```
Generate a value for `FLINS_WA_SECRET`:
```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

## Connect WhatsApp (read-only)
> ⚠️ This uses an unofficial WhatsApp Web client (Baileys), which breaks WhatsApp's Terms of
> Service. Accounts that automate can be banned. Try it on a spare number first.

```powershell
uv run flins wa pair   # scan the QR: WhatsApp > Settings > Linked devices > Link a device
uv run flins wa tail   # live feed; messages and contacts are stored in data/flins.db
```
The pairing is saved in `data/wa-auth/`. Treat that folder like a password: anyone with a copy
can read your WhatsApp. It's git-ignored.

## Run FLINS (shadow mode)
```powershell
uv run flins run
```
FLINS shows each incoming message and what it would do with it ("would just tell you",
"would send a holding reply", ...). Change modes from another terminal:
```powershell
uv run flins mode "I'm in working mood till 6pm"
uv run flins mode status
uv run flins stop
uv run flins resume
uv run flins mode "I'm done"
```
Or type the same phrases in your WhatsApp **"Message yourself"** chat while `flins run` is
running. FLINS's answer appears in the terminal for now; from M4 it replies in that chat.

| Say | Effect |
|---|---|
| `I'm in working mood` / `working mood till 6pm` / `working mood for 2 hours` | WORKING (default 3 h) |
| `focus mode` / `do not disturb` / `focus mode for 1h` | FOCUS (default 90 min) |
| `heading out` / `I'm away` | AWAY (default 2 h) |
| add `, strict` | use the `working-strict` profile |
| `extend 1 hour` / `extend till 7pm` | move the end time |
| `cancel` (within 10 s) | undo the last mode change |
| `stop` / `FLINS stop` | PAUSED: nothing is ever sent until `resume` |
| `resume` | back to the previous mode, if it hasn't ended |
| `I'm done` / `back to normal` | NORMAL, with a summary of the session |
| `status` | current mode and end time |

Modes always end: at most 12 hours, and a mode that ended while FLINS was off is NORMAL on
restart.

## AI providers (optional, but needed for drafts)
FLINS can use **Anthropic (Claude), Google Gemini, Groq, NVIDIA, OpenAI, OpenRouter, or a local
Ollama**. Put the keys you have in `.env`:
```
ANTHROPIC_API_KEY=...   # console.anthropic.com (not the same as a Claude Pro subscription)
GEMINI_API_KEY=...      # aistudio.google.com
GROQ_API_KEY=...        # console.groq.com
NVIDIA_API_KEY=...      # build.nvidia.com
```
`config/flins.example.yaml` (copy to `flins.yaml` to edit) lists, for each job, which
provider/model to try first, second, and so on. Providers without a key are skipped; if one fails,
FLINS tries the next. Anthropic is first by default; with only a Groq key, Groq does everything.

```powershell
uv run flins llm status          # which provider each job will use
uv run flins llm test            # one tiny request per configured model (fractions of a cent)
uv run flins llm models groq     # model names your key can use (also: gemini, nvidia, anthropic, ...)
uv run flins eval --live --provider groq   # safety eval against one provider's real models
```
Without any key FLINS still follows the rules, but can't understand ordinary messages or draft
replies. Spending stops at `limits.daily_llm_budget_usd` in your policy (default $2/day). Normal
mode, paused, unknown senders, money and OTP messages never go to any AI provider.

## What FLINS may say about you
FLINS answers what people actually ask, but it only states facts about you that you listed in
`config/about_me.yaml`, each with who may hear it:
```yaml
facts:
  - fact: "Hemanth is an engineering student."
    share_with: known      # everyone | known | trusted | inner
  - fact: "He's usually free after 7pm on weekdays."
    share_with: trusted
answers:
  - question: "Did you get my file?"
    answer: "He checks email in the evening and will reply then."
    share_with: known
```
Out of the box the file is empty, so FLINS answers general questions and small talk but says
nothing about your life. Restart `flins run` after editing it.

## Skills
Skills are small add-ons in `skills/<name>/`. They suggest replies or add facts, but they never send
and can only lower what FLINS would do. Each one runs in a locked-down process with only the
permissions its `flins.skill.yaml` lists. All are **off** until you enable them:

```powershell
uv run flins skills list                    # what's there and whether it's on
uv run flins skills test holding_reply      # sandboxed fixtures + safety eval with it on
uv run flins skills enable holding_reply    # shows its permissions; type the name to approve
uv run flins skills disable holding_reply
```

Starter skills:
- `holding_reply`: fixed holding text while you're working, focusing or away.
- `eta_reply`: while you're away, answers "where are you?".
- `availability_reply`: finds when you're free from a calendar `.ics` export (`skills.calendar_ics`).
- `faq_responder`: answers only with what you wrote in `config/faq.yaml` (copy `faq.example.yaml`).
- `whats_missed`: "what did I miss?", grouped by person, urgent first.

If a skill's files change after you enabled it, it stops running until you enable it again.

Hermes Agent can build new skills for you through `flins skilldev` (an MCP server). You review and
approve them on the dashboard's **Skills** page. Setup, with the WSL isolation it needs:
[docs/HERMES.md](docs/HERMES.md). You can also say "make a skill that …" to FLINS to leave
Hermes a request.

## Earned auto replies (M9)
FLINS can send its AI replies without asking you, but only to people you choose, and only after
it has proven itself with them:
```powershell
uv run flins contacts                         # progress for your inner/trusted contacts
uv run flins contacts auto "Meera" on        # let a contact earn it (off: ... off)
```
Then set `auto_replies: unlocked` in `config/policy.yaml`. A contact qualifies after you've
approved 20 of FLINS's drafts for them with at most 10% edited or rejected. They need a tier
that allows AUTO (trusted; inner stays at holding replies). Money, OTPs, legal/medical and
emotional messages always just notify you.

## Backups, autostart, wake word (M9)
```powershell
uv run flins backup                 # encrypted backup to data/backups (or --to D:\Backups)
uv run flins backup restore <file>  # into a new folder; never overwrites anything
uv run flins autostart on --voice   # start FLINS when you log in (off / status)
```
Backups need `FLINS_BACKUP_PASSPHRASE=<at least 12 characters>` in `.env`. Keep a copy in your
password manager: without it a backup can't be opened. `flins run` makes one a day.

Wake word: set `voice.wake_word: true` in `config/flins.yaml`, then say "FLINS" (or "FLINS,
status" in one go). It listens locally and saves nothing.

## More natural voice (Piper)
```powershell
uv run flins voice download          # once, ~60 MB (or: download --list to choose another voice)
```
Then in `config/flins.yaml` under `voice:` set `tts: piper`. Test with `uv run flins voice say hello`.

## Dashboard
You can also pair WhatsApp from the dashboard's **WhatsApp** page (it shows the QR code).

`flins run` serves it at http://127.0.0.1:8765 (or run `uv run flins dashboard` on its own).
Inbox with every decision and a "why?" page, drafts FLINS would send or is holding back,
contact tiers (set your family to `inner` and close friends to `trusted` here), and stop/resume.

## Safety eval
```powershell
uv run flins eval            # 162 recorded cases, including fooled/leaky AI answers: must be 0 unsafe
uv run flins eval --live     # the 42 normal situations against your configured AI models
```

Replay a scripted scenario through the real code (frozen clock, no WhatsApp):
```powershell
uv run pytest tests/integration/test_replay.py -v
```

## Development
```powershell
uv run pytest
uv run ruff check .
uv run mypy
npm --prefix wa-sidecar test
```

## Milestones
See SPEC §14. Done: **M0**–**M9**. Next come the JARVIS phases in
[docs/JARVIS_ARCHITECTURE.md](docs/JARVIS_ARCHITECTURE.md): phase 1 (the assistant core) is done;
phase 2 is memory.
