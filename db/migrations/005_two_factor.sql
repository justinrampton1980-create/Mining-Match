-- Two-factor login for admins (after 004). New databases get this from schema.sql, and
-- data_layer.init_schema() also creates these tables on existing databases at start-up.

-- ------------------------------------------------------- two-factor login (admins)
-- One row per account with two-factor on: the authenticator app's shared secret and the last
-- 30-second step used, so a code can't be used twice.
CREATE TABLE IF NOT EXISTS two_factor (
  user_id    TEXT PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
  secret     TEXT NOT NULL,                      -- base32 TOTP secret
  enabled_at TEXT,
  last_step  INTEGER
);

-- One-time recovery codes, hashed.
CREATE TABLE IF NOT EXISTS recovery_codes (
  code_hash TEXT PRIMARY KEY,
  user_id   TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  used_at   TEXT
);

-- The second step of a login: issued after a correct password, used once. 'required' = enter a
-- code; 'setup' = first login, set up the app (pending_secret holds the secret being set up).
CREATE TABLE IF NOT EXISTS login_challenges (
  token_hash     TEXT PRIMARY KEY,
  user_id        TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  purpose        TEXT NOT NULL CHECK (purpose IN ('required', 'setup')),
  expires_at     INTEGER NOT NULL,                -- unix seconds
  tries          INTEGER NOT NULL DEFAULT 0,
  pending_secret TEXT
);
