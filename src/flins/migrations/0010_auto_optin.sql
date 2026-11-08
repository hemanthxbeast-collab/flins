-- M9: you opt a contact in to unreviewed AI replies (they still have to earn it, see autonomy.py).
ALTER TABLE contacts ADD COLUMN auto_optin INTEGER NOT NULL DEFAULT 0;
