-- JARVIS phase 6b: per-contact holds. While a hold is active FLINS sends that contact nothing
-- (a NOTIFY ceiling in decide()). Every hold has an end time.
CREATE TABLE contact_holds (
    id          INTEGER PRIMARY KEY,
    wa_jid      TEXT NOT NULL UNIQUE,
    until       TEXT NOT NULL,
    source      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
