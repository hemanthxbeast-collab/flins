# Changelog

## 0.1.0: M1 WhatsApp sidecar, read-only (2026-11-07)
- `wa-sidecar/`: Node + Baileys linked-device client. Pairs by QR, receives messages and
  contacts, reconnects with backoff, stops cleanly on logout or conflict. No send capability.
- Signed, replay-protected WebSocket protocol between core and sidecar (loopback only).
- Python `BaileysAdapter` (read-only), `FakeAdapter`, sidecar process manager.
- Ingest: deduplicated message storage, contact creation and address-book import,
  phone-number/LID identity handling, bus events, connection audit.
- `flins wa pair` and `flins wa tail`. `flins doctor` now checks the sidecar build and pairing.
- Fix after first real pairing: WhatsApp's ID-mapping list no longer creates ~1,000 nameless
  "unknown" contacts. Safe automatic merge of duplicate phone/LID rows. Migration `0003`
  cleans up rows created before the fix.
- Tests: 85 Python (including 2 against the real Node sidecar) and 9 Node.

## 0.0.1: M0 skeleton (2026-11-07)
- uv project, `flins` CLI with a working `doctor` command and stubs for later milestones.
- `policy.yaml` schema with safety clamps (money/OTP/sensitive → NOTIFY, PAUSED → IGNORE),
  required mode expiry, and `dry_run` on by default.
- SQLite schema for every SPEC §11 table, a migration runner, WAL, and an append-only audit log.
- In-process async event bus.
- 53 tests. ruff and mypy (strict on core) clean. GitHub Actions CI config.
