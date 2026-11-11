-- J2 (memory): things FLINS remembers because you asked or agreed. Never written silently.
CREATE TABLE memories (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL DEFAULT 'semantic'
                  CHECK (kind IN ('semantic', 'episodic', 'relationship', 'task')),
    content       TEXT NOT NULL,
    source        TEXT NOT NULL,              -- told | suggested | dashboard | cli
    channel       TEXT,                       -- voice | self_chat | cli | dashboard
    importance    REAL NOT NULL DEFAULT 0.7 CHECK (importance BETWEEN 0 AND 1),
    confidence    REAL NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
    privacy       TEXT NOT NULL DEFAULT 'normal' CHECK (privacy IN ('normal', 'sensitive')),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    expires_at    TEXT,
    last_used_at  TEXT,
    uses          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_memories_importance ON memories (importance DESC, id DESC);

-- Small switches the assistant keeps between runs (e.g. memory on/off).
CREATE TABLE assistant_state (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
