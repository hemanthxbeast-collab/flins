# FLINS

Local-first personal voice assistant that can handle WhatsApp for you while you work.
Full design: [docs/SPEC.md](docs/SPEC.md). Build instructions for Claude Code: [CLAUDE.md](CLAUDE.md).

**Status:** Milestone M6. FLINS handles WhatsApp while you're busy (holding replies by itself, real
answers when you approve) and you can **talk to it**: by voice, or in plain words in your
"Message yourself" chat.

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

## Dashboard
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
See SPEC §14. Done: **M0**–**M6**. Next: **M7** (skills: calendar, "what did I miss", FAQ, in a sandbox).
