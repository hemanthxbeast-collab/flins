# FLINS

Local-first personal voice assistant that can handle WhatsApp for you while you work.
Full design: [docs/SPEC.md](docs/SPEC.md). Build instructions for Claude Code: [CLAUDE.md](CLAUDE.md).

**Status:** Milestone M0 (skeleton). Nothing connects to WhatsApp or sends anything yet.

## Requirements
- Windows 11, Python ≥ 3.12, [uv](https://docs.astral.sh/uv/), Node.js ≥ 20 (from M1), Git

## Setup
```powershell
uv sync
copy .env.example .env                              # fill in later (M1 / M3)
copy config\policy.example.yaml config\policy.yaml  # optional: edit your policy
uv run flins doctor
```

## Development
```powershell
uv run pytest
uv run ruff check .
uv run mypy
```

## Milestones
See SPEC §14. Done: **M0**. Next: **M1** (WhatsApp sidecar, read-only).
