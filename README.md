# FLINS

Local-first personal voice assistant that can handle WhatsApp for you while you work.
Full design: [docs/SPEC.md](docs/SPEC.md). Build instructions for Claude Code: [CLAUDE.md](CLAUDE.md).

**Status:** Milestone M1. FLINS can read WhatsApp. It **cannot send anything** yet.

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

## Development
```powershell
uv run pytest
uv run ruff check .
uv run mypy
npm --prefix wa-sidecar test
```

## Milestones
See SPEC §14. Done: **M0**, **M1**. Next: **M2** (mode state machine, commands, policy engine).
