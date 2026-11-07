-- 0007 (M4): link approvals and outbox rows to their decisions, record when things happened.

ALTER TABLE approvals ADD COLUMN decision_id INTEGER REFERENCES decisions (id);
ALTER TABLE approvals ADD COLUMN created_at TEXT;

ALTER TABLE outbox ADD COLUMN approval_id INTEGER REFERENCES approvals (id);
ALTER TABLE outbox ADD COLUMN decision_id INTEGER REFERENCES decisions (id);
ALTER TABLE outbox ADD COLUMN sent_at TEXT;

-- One send per approval, ever (idempotency on top of client_msg_id).
CREATE UNIQUE INDEX idx_outbox_approval ON outbox (approval_id) WHERE approval_id IS NOT NULL;
