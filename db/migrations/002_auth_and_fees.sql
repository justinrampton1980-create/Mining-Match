-- Run once on a database created before login, fee terms and introductions were added
-- (after 001_access_control.sql). New databases get all of this from schema.sql.
-- Existing accounts are marked verified so nobody already using the system is locked out.
ALTER TABLE users ADD COLUMN email_verified_at TEXT;
UPDATE users SET email_verified_at = CURRENT_TIMESTAMP;

-- ------------------------------------------------------------ login and sessions
-- Times in these auth tables are unix seconds (INTEGER), which makes expiry checks simple.
-- Tokens are random and only their SHA-256 hash is stored, so a copy of the database can't be
-- used to log in.
CREATE TABLE IF NOT EXISTS sessions (
  token_hash   TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  created_at   INTEGER NOT NULL,
  expires_at   INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL
);

-- One-time links sent by email.
CREATE TABLE IF NOT EXISTS auth_tokens (
  token_hash TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  purpose    TEXT NOT NULL CHECK (purpose IN ('verify_email', 'reset_password')),
  expires_at INTEGER NOT NULL,
  used_at    INTEGER
);

-- Counts recent attempts (failed logins, reset requests, sign-ups) for rate limiting.
CREATE TABLE IF NOT EXISTS rate_events (
  bucket TEXT NOT NULL,
  key    TEXT NOT NULL,
  at     INTEGER NOT NULL
);

-- --------------------------------------------------------- placement fee terms
-- Each published version is permanent: it is the record of what employers agreed to.
-- terms_text is the full wording shown to employers (to be drafted by a lawyer).
CREATE TABLE IF NOT EXISTS fee_terms (
  version        INTEGER PRIMARY KEY,
  fee_percent    REAL NOT NULL CHECK (fee_percent > 0 AND fee_percent <= 100),
  min_fee_aud    REAL CHECK (min_fee_aud IS NULL OR min_fee_aud >= 0),
  fee_basis      TEXT NOT NULL,                    -- what salary the % applies to
  window_months  INTEGER NOT NULL CHECK (window_months > 0),
  guarantee_days INTEGER NOT NULL CHECK (guarantee_days >= 0),
  terms_text     TEXT NOT NULL,
  published_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  published_by   TEXT
);

-- SQLite triggers; in PostgreSQL use a BEFORE UPDATE OR DELETE trigger function, or revoke
-- UPDATE/DELETE on this table from the application's database role.
CREATE TRIGGER IF NOT EXISTS fee_terms_no_update BEFORE UPDATE ON fee_terms
BEGIN SELECT RAISE(ABORT, 'published fee terms cannot be changed'); END;
CREATE TRIGGER IF NOT EXISTS fee_terms_no_delete BEFORE DELETE ON fee_terms
BEGIN SELECT RAISE(ABORT, 'published fee terms cannot be deleted'); END;

-- Which employer accepted which version, and who clicked accept. No ON DELETE CASCADE:
-- these are contract records and must not vanish with an account.
CREATE TABLE IF NOT EXISTS terms_acceptances (
  employer_id   TEXT NOT NULL REFERENCES employers (id),
  terms_version INTEGER NOT NULL REFERENCES fee_terms (version),
  accepted_by   TEXT NOT NULL,                     -- user id
  accepted_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (employer_id, terms_version)
);

-- An introduction is the moment an employer unlocked a candidate's details: the event that
-- starts the placement fee window. No foreign keys, so the record survives if the candidate
-- deletes their account (ids only, no personal details). One per candidate and employer.
CREATE TABLE IF NOT EXISTS introductions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id  TEXT NOT NULL,
  employer_id   TEXT NOT NULL,
  job_id        TEXT,
  terms_version INTEGER NOT NULL,
  unlocked_by   TEXT NOT NULL,                     -- user id
  unlocked_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (candidate_id, employer_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user           ON sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_user        ON auth_tokens (user_id, purpose);
CREATE INDEX IF NOT EXISTS idx_rate_events             ON rate_events (bucket, key, at);
CREATE INDEX IF NOT EXISTS idx_introductions_employer  ON introductions (employer_id, unlocked_at);
