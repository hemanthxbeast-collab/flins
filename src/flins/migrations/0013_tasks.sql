-- J3 (tasks and reminders). A reminder is a task with remind_at set.
CREATE TABLE tasks (
    id                INTEGER PRIMARY KEY,
    title             TEXT NOT NULL,
    notes             TEXT,
    priority          TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high')),
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'cancelled')),
    due_at            TEXT,
    remind_at         TEXT,                       -- next time to remind you; NULL = no reminder
    repeat            TEXT NOT NULL DEFAULT 'none'
                      CHECK (repeat IN ('none', 'daily', 'weekdays', 'weekly')),
    project           TEXT,
    source            TEXT NOT NULL,              -- told | cli | dashboard
    channel           TEXT,                       -- voice | self_chat | cli | dashboard
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    completed_at      TEXT,
    last_reminded_at  TEXT
);
CREATE INDEX idx_tasks_open ON tasks (status, remind_at);
