-- Run once on a database created before invoicing was added (after 002).
-- New databases get all of this from schema.sql.
ALTER TABLE employers ADD COLUMN abn TEXT;
ALTER TABLE employers ADD COLUMN billing_email TEXT;
ALTER TABLE employers ADD COLUMN billing_address TEXT;

-- ------------------------------------------------------------------- billing
-- Placement fees are invoiced and paid by bank transfer. Money is stored in whole cents.
-- A placement is a hire the employer (or an admin) reported for a candidate they unlocked.
-- No ON DELETE CASCADE: these are business and tax records and must outlive accounts.
CREATE TABLE IF NOT EXISTS placements (
  id                TEXT PRIMARY KEY,
  introduction_id   INTEGER NOT NULL REFERENCES introductions (id),
  candidate_id      TEXT NOT NULL,                 -- no FK: survives account deletion
  candidate_name    TEXT NOT NULL,                 -- as at the hire, for the invoice
  employer_id       TEXT NOT NULL REFERENCES employers (id),
  job_id            TEXT,
  job_title         TEXT,
  start_date        TEXT NOT NULL,
  base_salary_cents INTEGER NOT NULL CHECK (base_salary_cents > 0),
  terms_version     INTEGER NOT NULL REFERENCES fee_terms (version),
  fee_cents         INTEGER NOT NULL CHECK (fee_cents >= 0),  -- 0 when hired outside the fee window
  status            TEXT NOT NULL DEFAULT 'recorded' CHECK (status IN ('recorded', 'cancelled')),
  reported_by       TEXT NOT NULL,                 -- user id
  reported_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  cancelled_reason  TEXT
);
-- one active hire per introduction (a cancelled one can be recorded again)
CREATE UNIQUE INDEX IF NOT EXISTS idx_placements_active ON placements (introduction_id) WHERE status = 'recorded';

-- An invoice is 'scheduled' (not yet issued, no number) until its issue date, then 'issued'
-- with the next number, then 'paid' or 'void'. Once issued, amounts, dates, number and the
-- seller, buyer and bank details can't change (triggers below): fix mistakes by voiding and
-- issuing a new one.
CREATE TABLE IF NOT EXISTS invoices (
  id               TEXT PRIMARY KEY,
  seq              INTEGER UNIQUE,                 -- 1, 2, 3... set when issued
  number           TEXT UNIQUE,                    -- e.g. MM-00001, set when issued
  employer_id      TEXT NOT NULL REFERENCES employers (id),
  placement_id     TEXT REFERENCES placements (id),
  kind             TEXT NOT NULL CHECK (kind IN ('placement', 'services')),
  status           TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'issued', 'paid', 'void')),
  issue_date       TEXT NOT NULL,
  due_date         TEXT NOT NULL,
  subtotal_cents   INTEGER NOT NULL CHECK (subtotal_cents >= 0),
  gst_cents        INTEGER NOT NULL DEFAULT 0 CHECK (gst_cents >= 0),
  total_cents      INTEGER NOT NULL CHECK (total_cents >= 0),
  gst_registered   INTEGER CHECK (gst_registered IN (0, 1)),
  seller_json      TEXT,                           -- snapshots taken when issued
  buyer_json       TEXT,
  payment_json     TEXT,
  created_by       TEXT,
  created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  issued_at        TEXT,
  paid_on          TEXT,
  paid_recorded_by TEXT,
  payment_note     TEXT,
  voided_at        TEXT,
  void_reason      TEXT,
  reminders_sent   INTEGER NOT NULL DEFAULT 0,
  last_reminder_on TEXT,
  CHECK (total_cents = subtotal_cents + gst_cents),
  CHECK (status IN ('scheduled', 'void') OR number IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS invoice_lines (
  invoice_id   TEXT NOT NULL REFERENCES invoices (id),
  line_no      INTEGER NOT NULL,
  description  TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),  -- excluding GST
  PRIMARY KEY (invoice_id, line_no)
);

-- SQLite triggers; in PostgreSQL use trigger functions with the same conditions.
CREATE TRIGGER IF NOT EXISTS invoices_locked_once_issued
BEFORE UPDATE OF seq, number, employer_id, placement_id, kind, issue_date, due_date, subtotal_cents,
                 gst_cents, total_cents, gst_registered, seller_json, buyer_json, payment_json ON invoices
WHEN OLD.status <> 'scheduled'
BEGIN SELECT RAISE(ABORT, 'issued invoices cannot be changed: void it and issue a new one'); END;
CREATE TRIGGER IF NOT EXISTS invoices_void_is_final BEFORE UPDATE OF status ON invoices
WHEN OLD.status = 'void' AND NEW.status <> 'void'
BEGIN SELECT RAISE(ABORT, 'void invoices cannot be reopened'); END;
CREATE TRIGGER IF NOT EXISTS invoices_never_deleted BEFORE DELETE ON invoices
WHEN OLD.status <> 'scheduled'
BEGIN SELECT RAISE(ABORT, 'issued invoices cannot be deleted: void them instead'); END;
CREATE TRIGGER IF NOT EXISTS invoice_lines_locked_insert BEFORE INSERT ON invoice_lines
WHEN (SELECT status FROM invoices WHERE id = NEW.invoice_id) <> 'scheduled'
BEGIN SELECT RAISE(ABORT, 'lines of an issued invoice cannot be changed'); END;
CREATE TRIGGER IF NOT EXISTS invoice_lines_locked_update BEFORE UPDATE ON invoice_lines
WHEN (SELECT status FROM invoices WHERE id = OLD.invoice_id) <> 'scheduled'
BEGIN SELECT RAISE(ABORT, 'lines of an issued invoice cannot be changed'); END;
CREATE TRIGGER IF NOT EXISTS invoice_lines_locked_delete BEFORE DELETE ON invoice_lines
WHEN (SELECT status FROM invoices WHERE id = OLD.invoice_id) <> 'scheduled'
BEGIN SELECT RAISE(ABORT, 'lines of an issued invoice cannot be changed'); END;

CREATE INDEX IF NOT EXISTS idx_placements_employer      ON placements (employer_id, start_date);
CREATE INDEX IF NOT EXISTS idx_invoices_employer        ON invoices (employer_id, issue_date);
CREATE INDEX IF NOT EXISTS idx_invoices_status_due      ON invoices (status, due_date);
