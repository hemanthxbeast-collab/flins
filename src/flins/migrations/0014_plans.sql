-- J3b (planning). A plan is an ordered set of tasks: each step is a row in `tasks`.
CREATE TABLE plans (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    goal        TEXT NOT NULL,              -- what you asked for, in your words
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'cancelled')),
    source      TEXT NOT NULL,              -- told | cli | dashboard
    channel     TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
ALTER TABLE tasks ADD COLUMN plan_id INTEGER REFERENCES plans (id);
ALTER TABLE tasks ADD COLUMN step_no INTEGER;
CREATE INDEX idx_tasks_plan ON tasks (plan_id, step_no);
