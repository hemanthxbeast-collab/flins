-- 0005: one row per LLM call (cost metering, budget, debugging). Never stores message text.

CREATE TABLE llm_calls (
    id              INTEGER PRIMARY KEY,
    ts              TEXT NOT NULL,
    purpose         TEXT NOT NULL,            -- triage | draft | redflag
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    ok              INTEGER NOT NULL,
    error           TEXT,                     -- error class / short reason, no content
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_read      INTEGER NOT NULL DEFAULT 0,
    cache_write     INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0,
    latency_ms      INTEGER
);
CREATE INDEX idx_llm_calls_ts ON llm_calls (ts);

-- Drafts record why they were produced and whether they'd go out unreviewed.
ALTER TABLE drafts ADD COLUMN would_send INTEGER NOT NULL DEFAULT 0;
ALTER TABLE drafts ADD COLUMN created_at TEXT;
