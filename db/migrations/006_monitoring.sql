-- Security monitoring (after 005). New databases get this from schema.sql, and the app
-- and `python -m monitoring` also create it on existing databases.

-- Security monitoring (python/monitoring.py): what each check last found, so an alert is sent
-- when something breaks, repeated every few hours, and once more when it's fixed.
CREATE TABLE IF NOT EXISTS monitor_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL                              -- JSON
);
