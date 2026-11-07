# Decisions log

Short records of choices made while building. Newest first.

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
