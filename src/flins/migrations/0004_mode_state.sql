-- 0004: mode state needs a revision counter (so a running FLINS notices changes made by
-- `flins stop` / `flins mode` from another process) and the full previous state (for
-- `resume` after a stop and `cancel` right after a change).

ALTER TABLE mode_state ADD COLUMN rev INTEGER NOT NULL DEFAULT 0;
ALTER TABLE mode_state ADD COLUMN prev_until TEXT;
ALTER TABLE mode_state ADD COLUMN prev_profile TEXT;

ALTER TABLE mode_history ADD COLUMN until TEXT;
ALTER TABLE mode_history ADD COLUMN readback TEXT;

-- Decisions point at the message they were made for, and keep the category and flags
-- outside the JSON blobs so the dashboard and debrief can query them.
ALTER TABLE decisions ADD COLUMN message_id INTEGER REFERENCES messages (id);
ALTER TABLE decisions ADD COLUMN category TEXT;
ALTER TABLE decisions ADD COLUMN flags TEXT NOT NULL DEFAULT '[]';      -- JSON array
ALTER TABLE decisions ADD COLUMN escalate INTEGER NOT NULL DEFAULT 0;
ALTER TABLE decisions ADD COLUMN interrupt INTEGER NOT NULL DEFAULT 0;
CREATE INDEX idx_decisions_created ON decisions (created_at);
