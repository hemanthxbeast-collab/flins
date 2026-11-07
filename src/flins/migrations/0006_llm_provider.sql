-- 0006: several LLM providers can be configured; record which one answered each call.
ALTER TABLE llm_calls ADD COLUMN provider TEXT;
