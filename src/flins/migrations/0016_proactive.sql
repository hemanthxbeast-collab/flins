-- JARVIS phase 7: what FLINS said on its own initiative (briefings, nudges). `key` makes each
-- one happen at most once ("morning:2026-11-13", "nudge:12:2026-11-13T10:00..."); rows with
-- outcome 'sent' are what the daily cap counts. No text is stored.
CREATE TABLE proactive_log (
    id       INTEGER PRIMARY KEY,
    kind     TEXT NOT NULL CHECK (kind IN ('morning', 'evening', 'nudge')),
    key      TEXT NOT NULL UNIQUE,
    outcome  TEXT NOT NULL CHECK (outcome IN ('sent', 'empty', 'capped', 'late')),
    spoken   INTEGER NOT NULL DEFAULT 0,
    at       TEXT NOT NULL
);
CREATE INDEX idx_proactive_at ON proactive_log (at);
