-- Run once on a database created before two-factor login was added (after 004).
-- New databases get all of this from schema.sql. (The app also creates these tables on start-up,
-- because they are new tables, so running this by hand is optional.)

-- Two-factor login (python/two_factor.py). secret is encrypted with the server's MM_SECRET_KEY
-- when that is set. enabled_at stays NULL until the person proves their app works.
CREATE TABLE IF NOT EXISTS two_factor (
  user_id    TEXT PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
  secret     TEXT NOT NULL,
  enabled_at INTEGER,
  last_step  INTEGER NOT NULL DEFAULT 0,           -- newest code used; no code works twice
  created_at INTEGER NOT NULL
);

-- One-time backup codes for when the phone is lost. Only their SHA-256 hash is stored.
CREATE TABLE IF NOT EXISTS recovery_codes (
  user_id   TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  code_hash TEXT NOT NULL,
  used_at   INTEGER,
  PRIMARY KEY (user_id, code_hash)
);

-- A correct password for an account with two-factor login gives one of these, not a session.
-- It is exchanged for a session together with a code, within a few minutes and a few tries.
CREATE TABLE IF NOT EXISTS login_challenges (
  token_hash TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  expires_at INTEGER NOT NULL,
  failures   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_login_challenges_user  ON login_challenges (user_id);
