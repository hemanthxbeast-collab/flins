-- 0008 (M5): what kind of message an outbox row is.
--   approved  a draft you approved (M4)
--   auto      an automatic holding reply (M5)
--   self      a note from FLINS to your own self-chat (M5)
ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'approved';
CREATE INDEX idx_outbox_chat_kind ON outbox (chat_jid, kind, status);
