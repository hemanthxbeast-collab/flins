# FLINS

Local-first personal voice assistant that can handle WhatsApp for you while you work.
Full design: [docs/SPEC.md](docs/SPEC.md). Build instructions for Claude Code: [CLAUDE.md](CLAUDE.md).

**Status:** Milestone M2. FLINS reads WhatsApp, follows modes, and decides what it *would* do
with each message. It **cannot send anything** yet.

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
See SPEC §14. Done: **M0**, **M1**, **M2**. Next: **M3** (LLM triage and drafting, output guard,
dashboard, eval set). Needs `ANTHROPIC_API_KEY`.
