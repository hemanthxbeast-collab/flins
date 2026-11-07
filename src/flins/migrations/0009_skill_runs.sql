-- M7: when a skill ran and which capabilities it was denied.
ALTER TABLE skill_runs ADD COLUMN denied TEXT;      -- JSON list, NULL if none
ALTER TABLE skill_runs ADD COLUMN created_at TEXT;
ALTER TABLE skill_runs ADD COLUMN decision_id INTEGER REFERENCES decisions (id);
