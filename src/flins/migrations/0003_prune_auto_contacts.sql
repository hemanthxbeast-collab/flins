-- 0003: remove contacts that M1's first version created from WhatsApp ID-mapping data.
-- These are people you've never chatted with and never saved. From now on, ingest only
-- creates contacts from the address book or from an actual message.
--
-- A row is removed only if ALL of these hold:
--   it was created automatically, is 'unknown', has no saved name,
--   the user has set nothing on it, and no stored message involves it.

DELETE FROM contacts
WHERE tier = 'unknown'
  AND source = 'message'
  AND display_name IS NULL
  AND notes IS NULL
  AND max_level IS NULL
  AND tone_profile IS NULL
  AND language IS NULL
  AND allowed_categories IS NULL
  AND never_mention = '[]'
  AND NOT EXISTS (SELECT 1 FROM tone_examples t WHERE t.contact_id = contacts.id)
  AND NOT EXISTS (
      SELECT 1 FROM messages m
      WHERE m.chat_jid IN (contacts.wa_jid, contacts.wa_lid)
         OR m.sender_jid IN (contacts.wa_jid, contacts.wa_lid)
  );
