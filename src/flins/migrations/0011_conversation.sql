-- J1 (assistant core): your conversation with FLINS across voice, self-chat, CLI and dashboard.
-- Turns are pruned after assistant.keep_days; `flins chat --clear` deletes them all.
CREATE TABLE conversation_sessions (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    last_at       TEXT NOT NULL,
    summary       TEXT,                        -- rolling summary of turns up to summary_upto
    summary_upto  INTEGER NOT NULL DEFAULT 0   -- conversation_turns.id
);

CREATE TABLE conversation_turns (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES conversation_sessions (id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    source      TEXT NOT NULL,                 -- voice | self_chat | cli | dashboard
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    route       TEXT,                          -- command | chat | error (assistant turns)
    text        TEXT NOT NULL
);
CREATE INDEX idx_conversation_turns_session ON conversation_turns (session_id, id);
