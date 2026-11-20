-- Run once on a database created before access control was added.
-- (New databases get all of this from schema.sql.)
ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1));

-- Contact details stay hidden from an employer until the candidate agrees. An employer asks
-- ('requested'); the candidate grants or declines. A candidate can also grant without being asked,
-- and can take it back later (status goes to 'declined', which also stops repeat requests).
CREATE TABLE IF NOT EXISTS contact_releases (
  candidate_id TEXT NOT NULL REFERENCES candidates (id) ON DELETE CASCADE,
  employer_id  TEXT NOT NULL REFERENCES employers (id) ON DELETE CASCADE,
  job_id       TEXT REFERENCES jobs (id) ON DELETE SET NULL,     -- job the request was about
  status       TEXT NOT NULL DEFAULT 'requested' CHECK (status IN ('requested', 'granted', 'declined')),
  requested_at TEXT,
  decided_at   TEXT,
  PRIMARY KEY (candidate_id, employer_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  user_id       TEXT,
  role          TEXT,
  action        TEXT NOT NULL,
  outcome       TEXT NOT NULL CHECK (outcome IN ('allowed', 'denied')),
  resource_type TEXT,
  resource_id   TEXT,
  detail        TEXT
);

CREATE INDEX IF NOT EXISTS idx_releases_employer ON contact_releases (employer_id);
CREATE INDEX IF NOT EXISTS idx_audit_user        ON audit_log (user_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_resource    ON audit_log (resource_type, resource_id);
