# Decisions log

Short records of choices made while building. Newest first.

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
