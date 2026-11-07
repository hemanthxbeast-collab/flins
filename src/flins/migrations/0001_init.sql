-- 0001: initial schema (SPEC §11). Timestamps are ISO-8601 UTC strings.

CREATE TABLE contacts (
    id                  INTEGER PRIMARY KEY,
    wa_jid              TEXT NOT NULL UNIQUE,
    display_name        TEXT,
    tier                TEXT NOT NULL DEFAULT 'known'
                        CHECK (tier IN ('inner', 'trusted', 'known', 'unknown', 'blocked')),
    tone_profile        TEXT,
    language            TEXT,
    max_level           INTEGER CHECK (max_level BETWEEN 0 AND 4),
    disclose_ai         INTEGER NOT NULL DEFAULT 1,
    notes               TEXT,
    never_mention       TEXT NOT NULL DEFAULT '[]',   -- JSON array
    allowed_categories  TEXT,                         -- JSON array, NULL = tier defaults
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE bursts (
    id          INTEGER PRIMARY KEY,
    chat_jid    TEXT NOT NULL,
    first_ts    TEXT NOT NULL,
    last_ts     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'processed'))
);
CREATE INDEX idx_bursts_chat ON bursts (chat_jid, status);

CREATE TABLE messages (
    id          INTEGER PRIMARY KEY,
    wa_msg_id   TEXT NOT NULL UNIQUE,
    chat_jid    TEXT NOT NULL,
    sender_jid  TEXT NOT NULL,
    direction   TEXT NOT NULL CHECK (direction IN ('IN', 'OUT')),
    type        TEXT NOT NULL,
    text        TEXT,
    quoted_id   TEXT,
    ts          TEXT NOT NULL,
    raw         TEXT,                                 -- JSON
    burst_id    INTEGER REFERENCES bursts (id)
);
CREATE INDEX idx_messages_chat_ts ON messages (chat_jid, ts);

CREATE TABLE decisions (
    id              INTEGER PRIMARY KEY,
    burst_id        INTEGER NOT NULL REFERENCES bursts (id),
    mode            TEXT NOT NULL,
    contact_tier    TEXT NOT NULL,
    hard_rule       TEXT,
    triage          TEXT,                             -- JSON
    ceilings        TEXT NOT NULL,                    -- JSON: every ceiling that went into min()
    final_level     INTEGER NOT NULL CHECK (final_level BETWEEN 0 AND 4),
    action          TEXT NOT NULL,
    skill           TEXT,
    prompt_version  TEXT,
    model           TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE drafts (
    id              INTEGER PRIMARY KEY,
    decision_id     INTEGER NOT NULL REFERENCES decisions (id),
    text            TEXT NOT NULL,
    confidence      REAL,
    guard_results   TEXT,                             -- JSON
    source          TEXT NOT NULL                     -- 'llm' | 'template' | 'skill:<name>'
);

CREATE TABLE approvals (
    id          INTEGER PRIMARY KEY,
    short_id    INTEGER NOT NULL,
    draft_id    INTEGER NOT NULL REFERENCES drafts (id),
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected', 'edited', 'expired')),
    channel     TEXT,
    final_text  TEXT,
    expires_at  TEXT NOT NULL,
    decided_at  TEXT
);
CREATE INDEX idx_approvals_status ON approvals (status, expires_at);

CREATE TABLE outbox (
    id              INTEGER PRIMARY KEY,
    client_msg_id   TEXT NOT NULL UNIQUE,
    chat_jid        TEXT NOT NULL,
    text            TEXT NOT NULL,
    level           INTEGER NOT NULL CHECK (level BETWEEN 0 AND 4),
    not_before      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'sent', 'failed', 'cancelled', 'dry_run')),
    wa_msg_id       TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX idx_outbox_status ON outbox (status, not_before);

CREATE TABLE mode_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),   -- single row
    mode        TEXT NOT NULL,
    profile     TEXT,
    until       TEXT,
    started_at  TEXT NOT NULL,
    started_by  TEXT NOT NULL,
    prev_mode   TEXT
);

CREATE TABLE mode_history (
    id          INTEGER PRIMARY KEY,
    from_mode   TEXT NOT NULL,
    to_mode     TEXT NOT NULL,
    event       TEXT NOT NULL,
    source      TEXT NOT NULL,
    ts          TEXT NOT NULL
);

CREATE TABLE skills (
    name            TEXT PRIMARY KEY,
    version         TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('incoming', 'validated', 'tested', 'approved', 'enabled', 'disabled')),
    sha256          TEXT,
    permissions     TEXT NOT NULL DEFAULT '[]',       -- JSON
    approved_at     TEXT
);

CREATE TABLE skill_runs (
    id          INTEGER PRIMARY KEY,
    skill       TEXT NOT NULL,
    burst_id    INTEGER REFERENCES bursts (id),
    input_hash  TEXT NOT NULL,
    output      TEXT,                                 -- JSON
    ms          INTEGER,
    error       TEXT
);

CREATE TABLE tone_examples (
    id          INTEGER PRIMARY KEY,
    contact_id  INTEGER NOT NULL REFERENCES contacts (id),
    draft       TEXT NOT NULL,
    final       TEXT NOT NULL,
    ts          TEXT NOT NULL
);

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL                         -- JSON
);
CREATE INDEX idx_audit_ts ON audit_log (ts);

-- Audit rows are append-only. Retention may delete old rows, but nothing may rewrite them.
CREATE TRIGGER audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;
