-- Run once on a database created before licence reminders were added (after 003).
-- New databases get all of this from schema.sql.
ALTER TABLE catalog_items ADD COLUMN renew_years INTEGER CHECK (renew_years IS NULL OR renew_years BETWEEN 1 AND 20);
ALTER TABLE catalog_items ADD COLUMN renew_note TEXT;
ALTER TABLE candidates ADD COLUMN expiry_reminders INTEGER NOT NULL DEFAULT 1 CHECK (expiry_reminders IN (0, 1));
ALTER TABLE candidates ADD COLUMN reminder_token TEXT;

-- -------------------------------------------------------- licence reminders
-- One row per reminder sent, so nobody gets the same one twice. stage: 90, 60 or 30 days before
-- expiry, or 0 once expired. Keyed on the expiry date, so renewing a licence starts afresh.
CREATE TABLE IF NOT EXISTS licence_reminders (
  candidate_id TEXT NOT NULL REFERENCES candidates (id) ON DELETE CASCADE,
  item_id      TEXT NOT NULL REFERENCES catalog_items (id),
  expiry_date  TEXT NOT NULL,
  stage        INTEGER NOT NULL CHECK (stage IN (90, 60, 30, 0)),
  sent_on      TEXT NOT NULL,
  PRIMARY KEY (candidate_id, item_id, expiry_date, stage)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_reminder_token ON candidates (reminder_token);
CREATE INDEX IF NOT EXISTS idx_cand_licences_expiry    ON candidate_licences (expiry_date);
CREATE INDEX IF NOT EXISTS idx_licence_reminders_sent  ON licence_reminders (sent_on);
