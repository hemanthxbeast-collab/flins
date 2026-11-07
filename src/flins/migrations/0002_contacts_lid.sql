-- 0002: WhatsApp identities. A person can have a phone-number JID and a LID (…@lid).
-- contacts.wa_jid holds the phone-number JID when known, otherwise the LID.

ALTER TABLE contacts ADD COLUMN wa_lid TEXT;
ALTER TABLE contacts ADD COLUMN push_name TEXT;       -- name the contact set for themselves
ALTER TABLE contacts ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'
    CHECK (source IN ('address_book', 'message', 'manual'));

CREATE UNIQUE INDEX idx_contacts_lid ON contacts (wa_lid) WHERE wa_lid IS NOT NULL;
