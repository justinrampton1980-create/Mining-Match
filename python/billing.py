"""Mining Match - placement fees and invoices, paid by bank transfer.

How it works
  1. An employer who unlocked a candidate records the hire: start date and first-year base
     salary. preview_hire() shows the fee first; record_hire() saves it.
  2. The fee follows the fee terms version the employer accepted when they unlocked the
     candidate: fee_percent of the salary, at least min_fee_aud. No fee is due if the start
     date falls after the fee window (window_months from the unlock).
  3. The invoice is issued on the start date (straight away if that date has passed) and is due
     payment_terms_days later, with GST at 10% if the business is registered for GST.
     issue_due_invoices() issues scheduled invoices: run it daily (python -m billing issue-due).
  4. The employer pays by bank transfer, using the invoice number as the reference. An admin
     records the payment with mark_paid(). Overdue invoices can get a reminder.

Money is kept in whole cents and worked out with Decimal, rounding half a cent up (ATO rule).
Bank details come only from server settings (BillingConfig), never from anything that can be
edited in the app, so a hijacked account can't redirect payments.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

import access_control as ac
import data_layer as dl

GST_RATE = Decimal("0.1")
MAX_SALARY = Decimal("5000000")      # sanity cap on a first-year base salary (AUD)
MAX_LINE_AMOUNT = Decimal("1000000")  # sanity cap on one line of a services invoice
MAX_START_AHEAD_DAYS = 548            # a start date at most about 18 months away
_ABN_WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class BillingNotSetUp(dl.DataError):
    """The server's invoicing settings are missing."""


# --------------------------------------------------------------------------
# Small helpers: ABN, money, dates
# --------------------------------------------------------------------------
def abn_digits(value: Any) -> str:
    """Check an ABN (11 digits, ATO check digits) and return it as 11 digits."""
    digits = re.sub(r"[\s-]", "", str(value or ""))
    if not re.fullmatch(r"[1-9]\d{10}", digits):
        raise ValueError("an ABN has 11 digits")
    numbers = [int(c) for c in digits]
    numbers[0] -= 1
    if sum(w * n for w, n in zip(_ABN_WEIGHTS, numbers)) % 89:
        raise ValueError("that ABN isn't valid: check the digits")
    return digits


def format_abn(digits: Optional[str]) -> Optional[str]:
    return f"{digits[:2]} {digits[2:5]} {digits[5:8]} {digits[8:]}" if digits else None


def to_cents(value: Any, label: str = "amount", maximum: Decimal = MAX_SALARY) -> int:
    """Dollars (number or text like '120,000.50') to whole cents. Must be above zero."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} must be a number")
    try:
        amount = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        raise ValueError(f"{label} must be a number") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{label} must be more than $0")
    if amount > maximum:
        raise ValueError(f"{label} looks too large")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError(f"{label} can have at most 2 decimal places")
    return int(amount * 100)


def dollars(cents: int) -> str:
    return f"${Decimal(cents) / 100:,.2f}"


def gst_on(subtotal_cents: int) -> int:
    """10% GST on a GST-exclusive amount, rounded to the nearest cent, half a cent up."""
    return int((Decimal(subtotal_cents) * GST_RATE).quantize(Decimal(1), ROUND_HALF_UP))


def add_months(d: date, months: int) -> date:
    years, month0 = divmod(d.month - 1 + months, 12)
    year, month = d.year + years, month0 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def fmt_date(value: Any) -> str:
    d = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def _parse_date(value: Any, label: str) -> date:
    try:
        iso = dl._iso(value)
    except ValueError:
        iso = None
    if not iso:
        raise ValueError(f"{label} must be a date (YYYY-MM-DD)")
    return date.fromisoformat(iso)


def _percent(value: float) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def _text(value: Any, label: str, limit: int, required: bool = True) -> Optional[str]:
    text = " ".join(str(value or "").split())
    if required and not text:
        raise ValueError(f"{label} is required")
    if len(text) > limit:
        raise ValueError(f"{label} is too long (at most {limit} characters)")
    return text or None


# --------------------------------------------------------------------------
# Settings (from the server's environment, never from the app)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class BillingConfig:
    legal_name: str
    abn: str
    address: str
    email: str
    bank_account_name: str
    bank_bsb: str
    bank_account_number: str
    gst_registered: bool = True
    phone: str = ""
    payid: str = ""
    payment_terms_days: int = 14
    invoice_prefix: str = "MM"
    timezone: str = "Australia/Brisbane"

    def __post_init__(self):
        for name in ("legal_name", "address", "email", "bank_account_name"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"billing setting {name} is required")
        if not _EMAIL.match(self.email):
            raise ValueError("billing setting email isn't a valid email address")
        object.__setattr__(self, "abn", abn_digits(self.abn))
        bsb = re.sub(r"[\s-]", "", str(self.bank_bsb or ""))
        if not re.fullmatch(r"\d{6}", bsb):
            raise ValueError("BSB must be 6 digits")
        object.__setattr__(self, "bank_bsb", f"{bsb[:3]}-{bsb[3:]}")
        account = re.sub(r"[\s-]", "", str(self.bank_account_number or ""))
        if not re.fullmatch(r"\d{5,10}", account):
            raise ValueError("bank account number must be 5 to 10 digits")
        object.__setattr__(self, "bank_account_number", account)
        if not 1 <= int(self.payment_terms_days) <= 90:
            raise ValueError("payment terms must be 1 to 90 days")
        if not re.fullmatch(r"[A-Z]{1,6}", self.invoice_prefix):
            raise ValueError("invoice prefix must be 1 to 6 capital letters")
        self.tzinfo()

    @classmethod
    def from_env(cls, env=os.environ) -> Optional["BillingConfig"]:
        """Read MM_ settings. Returns None when invoicing isn't set up (no MM_ABN)."""
        if not env.get("MM_ABN"):
            return None
        return cls(
            legal_name=env.get("MM_BUSINESS_NAME", ""), abn=env["MM_ABN"],
            address=env.get("MM_BUSINESS_ADDRESS", ""), email=env.get("MM_BILLING_EMAIL", ""),
            phone=env.get("MM_BUSINESS_PHONE", ""), gst_registered=env.get("MM_GST_REGISTERED", "1") == "1",
            bank_account_name=env.get("MM_BANK_ACCOUNT_NAME", ""), bank_bsb=env.get("MM_BANK_BSB", ""),
            bank_account_number=env.get("MM_BANK_ACCOUNT", ""), payid=env.get("MM_PAYID", ""),
            payment_terms_days=int(env.get("MM_PAYMENT_TERMS_DAYS", "14")),
            invoice_prefix=env.get("MM_INVOICE_PREFIX", "MM"), timezone=env.get("MM_TIMEZONE", "Australia/Brisbane"),
        )

    def tzinfo(self):
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(self.timezone)
        except Exception:
            if self.timezone == "Australia/Brisbane":  # no daylight saving: always UTC+10
                return dt_timezone(timedelta(hours=10))
            raise ValueError(f"unknown time zone: {self.timezone}") from None

    def today(self) -> date:
        return datetime.now(self.tzinfo()).date()

    def seller(self) -> dict:
        return {"legal_name": self.legal_name, "abn": format_abn(self.abn), "address": self.address,
                "email": self.email, "phone": self.phone or None, "gst_registered": self.gst_registered}

    def payment(self, reference: Optional[str] = None) -> dict:
        return {"method": "Bank transfer", "account_name": self.bank_account_name, "bsb": self.bank_bsb,
                "account_number": self.bank_account_number, "payid": self.payid or None, "reference": reference}


def _need(config: Optional[BillingConfig]) -> BillingConfig:
    if config is None:
        raise BillingNotSetUp("Invoicing isn't set up yet. Contact Mining Match.")
    return config


def billing_settings(conn, config: Optional[BillingConfig], principal) -> dict:
    """What employers need to pay, as invoices will show it."""
    ac.authorize(conn, principal, "billing.settings", "employer", "admin")
    if config is None:
        return {"enabled": False}
    return {"enabled": True, "seller": config.seller(), "payment": config.payment(),
            "payment_terms_days": config.payment_terms_days, "gst_registered": config.gst_registered}


# --------------------------------------------------------------------------
# Employers' billing details (who invoices are made out to)
# --------------------------------------------------------------------------
def get_billing_details(conn, principal, *, employer_id: Optional[str] = None) -> dict:
    action = "billing.details.view"
    p = ac.authorize(conn, principal, action, "employer", "admin")
    owner = ac.employer_scope(conn, p, action, employer_id)
    row = conn.execute(
        "SELECT e.company_name, e.contact_name, e.abn, e.billing_email, e.billing_address, u.email "
        "FROM employers e JOIN users u ON u.id = e.user_id WHERE e.id = ?", (owner,)).fetchone()
    if row is None:
        raise dl.NotFoundError(f"employer not found: {owner}")
    return {"employer_id": owner, "company_name": row["company_name"], "contact_name": row["contact_name"],
            "abn": format_abn(row["abn"]), "billing_email": row["billing_email"],
            "billing_address": row["billing_address"], "account_email": row["email"]}


def update_billing_details(conn, principal, *, abn: Any = None, billing_email: Any = None,
                           billing_address: Any = None, employer_id: Optional[str] = None) -> dict:
    """Set ABN, invoice email and postal address. Empty values clear them."""
    action = "billing.details.update"
    p = ac.authorize(conn, principal, action, "employer", "admin")
    owner = ac.employer_scope(conn, p, action, employer_id)
    abn_value = abn_digits(abn) if str(abn or "").strip() else None
    email = str(billing_email or "").strip().lower() or None
    if email and (len(email) > 254 or not _EMAIL.match(email)):
        raise ValueError("billing email isn't a valid email address")
    address = _text(billing_address, "billing address", 300, required=False)
    with conn:
        cur = conn.execute("UPDATE employers SET abn = ?, billing_email = ?, billing_address = ? WHERE id = ?",
                           (abn_value, email, address, owner))
    if cur.rowcount == 0:
        raise dl.NotFoundError(f"employer not found: {owner}")
    ac.audit_allowed(conn, p, action, "employer", owner)
    return get_billing_details(conn, p, employer_id=owner if p.role == "admin" else None)


# --------------------------------------------------------------------------
# Hires and the fee
# --------------------------------------------------------------------------
def _introduction(conn, employer_id: str, candidate_id: str):
    return conn.execute(
        "SELECT i.id, i.unlocked_at, i.terms_version, t.fee_percent, t.min_fee_aud, t.fee_basis, "
        "t.window_months, t.guarantee_days FROM introductions i JOIN fee_terms t ON t.version = i.terms_version "
        "WHERE i.employer_id = ? AND i.candidate_id = ?", (employer_id, candidate_id)).fetchone()


def _quote(conn, config: BillingConfig, employer_id: str, candidate_id: str, start_date: Any,
           base_salary: Any, today: date) -> dict:
    intro = _introduction(conn, employer_id, candidate_id)
    if intro is None:
        raise dl.DataError("you haven't unlocked this candidate, so there's no placement to record")
    start = _parse_date(start_date, "start date")
    unlocked = date.fromisoformat(intro["unlocked_at"][:10])
    if start < unlocked:
        raise ValueError("the start date is before you unlocked this candidate. "
                         "If they already worked for you, contact Mining Match.")
    if start > today + timedelta(days=MAX_START_AHEAD_DAYS):
        raise ValueError("the start date is more than 18 months away")
    salary = to_cents(base_salary, "first-year base salary")
    window_ends = add_months(unlocked, intro["window_months"])
    in_window = start <= window_ends
    percent_fee = int((Decimal(salary) * Decimal(str(intro["fee_percent"])) / 100).quantize(Decimal(1), ROUND_HALF_UP))
    min_fee = to_cents(intro["min_fee_aud"], "minimum fee", Decimal("10000000")) if intro["min_fee_aud"] else 0
    fee = max(percent_fee, min_fee) if in_window else 0
    gst = gst_on(fee) if config.gst_registered else 0
    issue = max(start, today)
    return {
        "introduction_id": intro["id"], "terms_version": intro["terms_version"],
        "fee_percent": intro["fee_percent"], "fee_basis": intro["fee_basis"], "min_fee_cents": min_fee,
        "guarantee_days": intro["guarantee_days"], "unlocked_on": unlocked.isoformat(),
        "window_ends": window_ends.isoformat(), "in_window": in_window, "start_date": start.isoformat(),
        "base_salary_cents": salary, "percent_fee_cents": percent_fee,
        "minimum_applied": in_window and min_fee > percent_fee,
        "fee_cents": fee, "gst_cents": gst, "total_cents": fee + gst, "gst_registered": config.gst_registered,
        "issue_date": issue.isoformat() if fee else None,
        "due_date": (issue + timedelta(days=config.payment_terms_days)).isoformat() if fee else None,
    }


def _placement_line(q: dict, name: str, job_title: Optional[str]) -> str:
    role = f", {job_title}" if job_title else ""
    if q["minimum_applied"]:
        how = f"Minimum placement fee (fee terms version {q['terms_version']})"
    else:
        basis = q["fee_basis"][:1].lower() + q["fee_basis"][1:]
        how = (f"{_percent(q['fee_percent'])}% of {basis}, {dollars(q['base_salary_cents'])} "
               f"(fee terms version {q['terms_version']})")
    return f"Placement fee: {name}{role}, starting {fmt_date(q['start_date'])}. {how}."


def preview_hire(conn, config: Optional[BillingConfig], principal, candidate_id: str, start_date: Any,
                 base_salary: Any, *, employer_id: Optional[str] = None, today: Optional[date] = None) -> dict:
    """Work out the fee and invoice dates for a hire, without saving anything."""
    action = "billing.preview_hire"
    p = ac.authorize(conn, principal, action, "employer", "admin")
    owner = ac.employer_scope(conn, p, action, employer_id)
    cfg = _need(config)
    return _quote(conn, cfg, owner, candidate_id, start_date, base_salary, today or cfg.today())


def record_hire(conn, config: Optional[BillingConfig], mailer, base_url: str, principal, candidate_id: str,
                start_date: Any, base_salary: Any, *, job_id: Optional[str] = None,
                employer_id: Optional[str] = None, today: Optional[date] = None) -> dict:
    """Record a hire and schedule (or issue) its placement fee invoice.

    Returns {"placement": ..., "invoice": ... or None}. No invoice when the hire falls outside
    the fee window.
    """
    action = "billing.record_hire"
    p = ac.authorize(conn, principal, action, "employer", "admin")
    owner = ac.employer_scope(conn, p, action, employer_id)
    cfg = _need(config)
    today = today or cfg.today()
    q = _quote(conn, cfg, owner, candidate_id, start_date, base_salary, today)
    job_title = None
    if job_id is not None:
        job = conn.execute("SELECT employer_id, title FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise dl.NotFoundError(f"job not found: {job_id}")
        if job["employer_id"] != owner:
            ac.deny(conn, p, action, "job", job_id, "not this employer's job")
        job_title = job["title"]
    candidate = conn.execute("SELECT name FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    name = candidate["name"] if candidate else ac.display_label(candidate_id)
    placement_id = uuid.uuid4().hex
    invoice_id = None
    try:
        with conn:
            conn.execute(
                "INSERT INTO placements (id, introduction_id, candidate_id, candidate_name, employer_id, job_id, "
                "job_title, start_date, base_salary_cents, terms_version, fee_cents, reported_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (placement_id, q["introduction_id"], candidate_id, name, owner, job_id, job_title, q["start_date"],
                 q["base_salary_cents"], q["terms_version"], q["fee_cents"], p.user_id))
            if q["fee_cents"] > 0:
                invoice_id = _create_scheduled(conn, cfg, owner, "placement", placement_id, q["issue_date"],
                                               q["due_date"], [(_placement_line(q, name, job_title), q["fee_cents"])],
                                               p.user_id)
    except sqlite3.IntegrityError as exc:
        if "placements.introduction_id" in str(exc):
            raise dl.DuplicateError("this hire is already recorded") from None
        raise
    ac.audit_allowed(conn, p, action, "placement", placement_id, "fee due" if invoice_id else "no fee: outside the fee window")
    invoice = None
    if invoice_id:
        if q["issue_date"] <= today.isoformat():
            invoice = _issue(conn, cfg, mailer, base_url, invoice_id, today)
        else:
            invoice = _load_invoice(conn, invoice_id, today)
    return {"placement": _load_placement(conn, placement_id), "invoice": invoice}


def cancel_hire(conn, config: Optional[BillingConfig], principal, placement_id: str, reason: Any,
                *, today: Optional[date] = None) -> dict:
    """Cancel a recorded hire (e.g. they didn't start) and void its invoice.

    Employers can cancel until the invoice is issued; after that only an admin can, and never
    once the invoice is paid.
    """
    action = "billing.cancel_hire"
    p = ac.authorize(conn, principal, action, "employer", "admin")
    why = _text(reason, "reason", 200)
    row = conn.execute("SELECT employer_id, status FROM placements WHERE id = ?", (placement_id,)).fetchone()
    if row is None:
        raise dl.NotFoundError(f"placement not found: {placement_id}")
    if p.role == "employer" and row["employer_id"] != p.employer_id:
        ac.deny(conn, p, action, "placement", placement_id, "not this employer's placement")
    if row["status"] == "cancelled":
        raise dl.DataError("this hire is already cancelled")
    invoice = conn.execute("SELECT id, status FROM invoices WHERE placement_id = ? AND status != 'void'",
                           (placement_id,)).fetchone()
    if invoice is not None and invoice["status"] == "paid":
        raise dl.DataError("the invoice for this hire is paid, so it can't be cancelled here")
    if invoice is not None and invoice["status"] == "issued" and p.role != "admin":
        ac.deny(conn, p, action, "placement", placement_id, "invoice already issued")
    with conn:
        conn.execute("UPDATE placements SET status = 'cancelled', cancelled_reason = ? WHERE id = ?", (why, placement_id))
        if invoice is not None:
            conn.execute("UPDATE invoices SET status = 'void', voided_at = CURRENT_TIMESTAMP, void_reason = ? WHERE id = ?",
                         (f"Hire cancelled: {why}", invoice["id"]))
    ac.audit_allowed(conn, p, action, "placement", placement_id)
    return _load_placement(conn, placement_id)


def list_placements(conn, principal, *, employer_id: Optional[str] = None) -> list:
    action = "billing.placements.list"
    p = ac.authorize(conn, principal, action, "employer", "admin")
    if p.role == "employer":
        employer_id = ac.employer_scope(conn, p, action, employer_id)
    where, params = ("WHERE pl.employer_id = ?", [employer_id]) if employer_id else ("", [])
    rows = conn.execute(
        "SELECT pl.id FROM placements pl " + where + " ORDER BY pl.start_date DESC, pl.reported_at DESC", params).fetchall()
    return [_load_placement(conn, r["id"]) for r in rows]


def _load_placement(conn, placement_id: str) -> dict:
    row = conn.execute(
        "SELECT pl.*, e.company_name, i.id AS invoice_id, i.number AS invoice_number, i.status AS invoice_status "
        "FROM placements pl JOIN employers e ON e.id = pl.employer_id "
        "LEFT JOIN invoices i ON i.placement_id = pl.id AND (i.status != 'void' OR pl.status = 'cancelled') "
        "WHERE pl.id = ? ORDER BY i.created_at DESC LIMIT 1", (placement_id,)).fetchone()
    keep = ("id", "candidate_id", "candidate_name", "employer_id", "company_name", "job_id", "job_title", "start_date",
            "base_salary_cents", "terms_version", "fee_cents", "status", "reported_at", "cancelled_reason",
            "invoice_id", "invoice_number", "invoice_status")
    return {k: row[k] for k in keep}


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------
def _create_scheduled(conn, cfg: BillingConfig, employer_id: str, kind: str, placement_id: Optional[str],
                      issue_date: str, due_date: str, lines: list, created_by: Optional[str]) -> str:
    """Insert a scheduled invoice and its lines (inside the caller's transaction)."""
    invoice_id = uuid.uuid4().hex
    subtotal = sum(amount for _, amount in lines)
    gst = gst_on(subtotal) if cfg.gst_registered else 0  # GST on the total of the lines (ATO total invoice rule)
    conn.execute(
        "INSERT INTO invoices (id, employer_id, placement_id, kind, status, issue_date, due_date, subtotal_cents, "
        "gst_cents, total_cents, gst_registered, created_by) VALUES (?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?, ?, ?)",
        (invoice_id, employer_id, placement_id, kind, issue_date, due_date, subtotal, gst, subtotal + gst,
         1 if cfg.gst_registered else 0, created_by))
    for i, (description, amount) in enumerate(lines, start=1):
        conn.execute("INSERT INTO invoice_lines (invoice_id, line_no, description, amount_cents) VALUES (?, ?, ?, ?)",
                     (invoice_id, i, description, amount))
    return invoice_id


def _issue(conn, cfg: BillingConfig, mailer, base_url: str, invoice_id: str, today: date) -> dict:
    """Give a scheduled invoice its number, freeze the details, email it."""
    for _ in range(5):
        try:
            with conn:
                row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
                if row is None or row["status"] != "scheduled":
                    break
                buyer = conn.execute(
                    "SELECT e.company_name, e.contact_name, e.abn, e.billing_email, e.billing_address, u.email "
                    "FROM employers e JOIN users u ON u.id = e.user_id WHERE e.id = ?", (row["employer_id"],)).fetchone()
                seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM invoices").fetchone()[0]
                number = f"{cfg.invoice_prefix}-{seq:05d}"
                planned_issue = date.fromisoformat(row["issue_date"])
                terms = (date.fromisoformat(row["due_date"]) - planned_issue).days
                issue = max(planned_issue, today)  # never backdated
                gst = gst_on(row["subtotal_cents"]) if cfg.gst_registered else 0
                conn.execute(
                    "UPDATE invoices SET status = 'issued', seq = ?, number = ?, issue_date = ?, due_date = ?, "
                    "gst_cents = ?, total_cents = ?, gst_registered = ?, seller_json = ?, buyer_json = ?, "
                    "payment_json = ?, issued_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'scheduled'",
                    (seq, number, issue.isoformat(), (issue + timedelta(days=terms)).isoformat(), gst,
                     row["subtotal_cents"] + gst, 1 if cfg.gst_registered else 0, json.dumps(cfg.seller()),
                     json.dumps({"company_name": buyer["company_name"], "contact_name": buyer["contact_name"],
                                 "abn": format_abn(buyer["abn"]), "address": buyer["billing_address"],
                                 "email": buyer["billing_email"] or buyer["email"]}),
                     json.dumps(cfg.payment(number)), invoice_id))
            break
        except sqlite3.IntegrityError as exc:  # another process took that number first: try the next
            if "invoices.seq" not in str(exc) and "invoices.number" not in str(exc):
                raise
    invoice = _load_invoice(conn, invoice_id, today)
    if invoice["status"] == "issued":
        invoice["emailed"] = _email_invoice(mailer, base_url, invoice, reminder=False)
        ac.audit_allowed(conn, None, "billing.invoice.issue", "invoice", invoice_id,
                         invoice["number"] + ("" if invoice["emailed"] else ": email failed"))
    return invoice


def _display_status(row, today: date) -> str:
    if row["status"] == "issued":
        return "overdue" if row["due_date"] < today.isoformat() else "due"
    return row["status"]


def _load_invoice(conn, invoice_id: str, today: date) -> dict:
    row = conn.execute(
        "SELECT i.*, e.company_name, pl.terms_version, t.guarantee_days, pl.start_date FROM invoices i "
        "JOIN employers e ON e.id = i.employer_id LEFT JOIN placements pl ON pl.id = i.placement_id "
        "LEFT JOIN fee_terms t ON t.version = pl.terms_version WHERE i.id = ?", (invoice_id,)).fetchone()
    if row is None:
        raise dl.NotFoundError(f"invoice not found: {invoice_id}")
    lines = conn.execute("SELECT line_no, description, amount_cents FROM invoice_lines WHERE invoice_id = ? "
                         "ORDER BY line_no", (invoice_id,)).fetchall()
    invoice = {k: row[k] for k in (
        "id", "number", "employer_id", "company_name", "placement_id", "kind", "status", "issue_date", "due_date",
        "subtotal_cents", "gst_cents", "total_cents", "paid_on", "payment_note", "voided_at", "void_reason",
        "reminders_sent", "last_reminder_on", "issued_at", "start_date", "guarantee_days")}
    invoice["gst_registered"] = bool(row["gst_registered"])
    invoice["display_status"] = _display_status(row, today)
    invoice["paid_on_time"] = bool(row["paid_on"] and row["paid_on"] <= row["due_date"])
    invoice["lines"] = [dict(r) for r in lines]
    for key in ("seller", "buyer", "payment"):
        invoice[key] = json.loads(row[f"{key}_json"]) if row[f"{key}_json"] else None
    invoice["title"] = "Tax invoice" if invoice["gst_registered"] else "Invoice"
    return invoice


def _invoice_access(conn, p, action: str, invoice_id: str):
    row = conn.execute("SELECT employer_id FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row is None:
        raise dl.NotFoundError(f"invoice not found: {invoice_id}")
    if p.role == "employer" and row["employer_id"] != p.employer_id:
        ac.deny(conn, p, action, "invoice", invoice_id, "not this employer's invoice")


def list_invoices(conn, config: Optional[BillingConfig], principal, *, status: Optional[str] = None,
                  employer_id: Optional[str] = None, today: Optional[date] = None) -> list:
    """Employer: own invoices. Admin: all, or one employer's. status: scheduled, due, overdue, paid, void."""
    action = "billing.invoices.list"
    p = ac.authorize(conn, principal, action, "employer", "admin")
    if p.role == "employer":
        employer_id = ac.employer_scope(conn, p, action, employer_id)
    today = today or (config.today() if config else date.today())
    where, params = [], []
    if employer_id:
        where.append("i.employer_id = ?")
        params.append(employer_id)
    if status in ("scheduled", "paid", "void"):
        where.append("i.status = ?")
        params.append(status)
    elif status in ("due", "overdue"):
        where.append("i.status = 'issued' AND i.due_date " + ("<" if status == "overdue" else ">=") + " ?")
        params.append(today.isoformat())
    elif status:
        raise ValueError("status must be scheduled, due, overdue, paid or void")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        "SELECT i.id, i.number, i.kind, i.status, i.issue_date, i.due_date, i.subtotal_cents, i.gst_cents, "
        "i.total_cents, i.paid_on, i.placement_id, i.employer_id, e.company_name FROM invoices i "
        f"JOIN employers e ON e.id = i.employer_id {clause} "
        "ORDER BY (i.status = 'scheduled') DESC, i.issue_date DESC, i.seq DESC", params).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["display_status"] = _display_status(r, today)
        out.append(item)
    return out


def get_invoice(conn, config: Optional[BillingConfig], principal, invoice_id: str, *, today: Optional[date] = None) -> dict:
    action = "billing.invoice.view"
    p = ac.authorize(conn, principal, action, "employer", "admin")
    _invoice_access(conn, p, action, invoice_id)
    return _load_invoice(conn, invoice_id, today or (config.today() if config else date.today()))


def invoice_pdf(conn, config: Optional[BillingConfig], principal, invoice_id: str) -> tuple:
    """(filename, PDF bytes) for an issued, paid or void invoice."""
    action = "billing.invoice.pdf"
    p = ac.authorize(conn, principal, action, "employer", "admin")
    _invoice_access(conn, p, action, invoice_id)
    invoice = _load_invoice(conn, invoice_id, config.today() if config else date.today())
    if invoice["number"] is None:
        raise dl.DataError("this invoice hasn't been issued yet")
    return f"{invoice['number']}.pdf", render_pdf(invoice)


def issue_due_invoices(conn, config: Optional[BillingConfig], mailer, base_url: str, principal=None,
                       *, today: Optional[date] = None) -> list:
    """Issue every scheduled invoice whose date has come. Returns their numbers.

    principal=None is the daily job on the server (python -m billing issue-due); from the
    app, only admins can run it.
    """
    if principal is not None:
        p = ac.authorize(conn, principal, "billing.issue_due", "admin")
        ac.audit_allowed(conn, p, "billing.issue_due", None, None)
    cfg = _need(config)
    today = today or cfg.today()
    due = conn.execute("SELECT id FROM invoices WHERE status = 'scheduled' AND issue_date <= ? "
                       "ORDER BY issue_date, created_at", (today.isoformat(),)).fetchall()
    return [inv["number"] for inv in (_issue(conn, cfg, mailer, base_url, r["id"], today) for r in due) if inv["number"]]


def create_services_invoice(conn, config: Optional[BillingConfig], mailer, base_url: str, principal, employer_id: str,
                            lines: list, *, due_days: Optional[int] = None, today: Optional[date] = None) -> dict:
    """Admin: invoice an employer for other services (e.g. a subscription), issued straight away.

    lines: [{"description": ..., "amount": dollars excluding GST}, ...] (1 to 10 lines)
    """
    action = "billing.invoice.create"
    p = ac.authorize(conn, principal, action, "admin")
    cfg = _need(config)
    today = today or cfg.today()
    if conn.execute("SELECT 1 FROM employers WHERE id = ?", (employer_id,)).fetchone() is None:
        raise dl.NotFoundError(f"employer not found: {employer_id}")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 10:
        raise ValueError("an invoice needs 1 to 10 lines")
    clean = [(_text(line.get("description"), "description", 200),
              to_cents(line.get("amount"), "amount", MAX_LINE_AMOUNT)) for line in lines]
    days = cfg.payment_terms_days if due_days is None else due_days
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
        raise ValueError("due in must be 1 to 90 days")
    with conn:
        invoice_id = _create_scheduled(conn, cfg, employer_id, "services", None, today.isoformat(),
                                       (today + timedelta(days=days)).isoformat(), clean, p.user_id)
    ac.audit_allowed(conn, p, action, "invoice", invoice_id)
    return _issue(conn, cfg, mailer, base_url, invoice_id, today)


def mark_paid(conn, config: Optional[BillingConfig], principal, invoice_id: str, paid_on: Any,
              note: Any = None, *, today: Optional[date] = None) -> dict:
    """Admin: record that the full amount arrived in the bank account."""
    action = "billing.invoice.paid"
    p = ac.authorize(conn, principal, action, "admin")
    today = today or (config.today() if config else date.today())
    received = _parse_date(paid_on, "date received")
    if received > today:
        raise ValueError("the date received can't be in the future")
    remark = _text(note, "note", 200, required=False)
    row = conn.execute("SELECT status, number FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row is None:
        raise dl.NotFoundError(f"invoice not found: {invoice_id}")
    if row["status"] != "issued":
        raise dl.DataError(f"only an issued, unpaid invoice can be marked paid (this one is {row['status']})")
    with conn:
        conn.execute("UPDATE invoices SET status = 'paid', paid_on = ?, paid_recorded_by = ?, payment_note = ? "
                     "WHERE id = ? AND status = 'issued'", (received.isoformat(), p.user_id, remark, invoice_id))
    ac.audit_allowed(conn, p, action, "invoice", invoice_id, row["number"])
    return _load_invoice(conn, invoice_id, today)


def void_invoice(conn, config: Optional[BillingConfig], principal, invoice_id: str, reason: Any,
                 *, today: Optional[date] = None) -> dict:
    """Admin: cancel an invoice that shouldn't be paid. It keeps its number; issue a new one if needed."""
    action = "billing.invoice.void"
    p = ac.authorize(conn, principal, action, "admin")
    why = _text(reason, "reason", 200)
    row = conn.execute("SELECT status, number FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row is None:
        raise dl.NotFoundError(f"invoice not found: {invoice_id}")
    if row["status"] not in ("scheduled", "issued"):
        raise dl.DataError(f"a {row['status']} invoice can't be voided")
    with conn:
        conn.execute("UPDATE invoices SET status = 'void', voided_at = CURRENT_TIMESTAMP, void_reason = ? WHERE id = ?",
                     (why, invoice_id))
    ac.audit_allowed(conn, p, action, "invoice", invoice_id, row["number"])
    return _load_invoice(conn, invoice_id, today or (config.today() if config else date.today()))


def send_reminder(conn, config: Optional[BillingConfig], mailer, base_url: str, principal, invoice_id: str,
                  *, today: Optional[date] = None) -> dict:
    """Admin: email the employer about an unpaid invoice (at most once a day)."""
    action = "billing.invoice.remind"
    p = ac.authorize(conn, principal, action, "admin")
    cfg = _need(config)
    today = today or cfg.today()
    invoice = _load_invoice(conn, invoice_id, today)
    if invoice["status"] != "issued":
        raise dl.DataError("reminders are only for issued, unpaid invoices")
    if invoice["last_reminder_on"] == today.isoformat():
        raise dl.DataError("a reminder was already sent today")
    if not _email_invoice(mailer, base_url, invoice, reminder=True):
        raise dl.DataError("the reminder email couldn't be sent: try again later")
    with conn:
        conn.execute("UPDATE invoices SET reminders_sent = reminders_sent + 1, last_reminder_on = ? WHERE id = ?",
                     (today.isoformat(), invoice_id))
    ac.audit_allowed(conn, p, action, "invoice", invoice_id, invoice["number"])
    return _load_invoice(conn, invoice_id, today)


# --------------------------------------------------------------------------
# Email and PDF
# --------------------------------------------------------------------------
FRAUD_WARNING = ("Our bank details never change by email. If you get a message saying they have, "
                 "don't pay: call us, or check the details in your Mining Match account.")


def _guarantee_text(invoice: dict) -> Optional[str]:
    if invoice["kind"] != "placement" or invoice.get("guarantee_days") is None:
        return None
    return (f"Replacement guarantee: {invoice['guarantee_days']} days from the start date, "
            f"provided this invoice is paid by the due date.")


def _email_invoice(mailer, base_url: str, invoice: dict, *, reminder: bool) -> bool:
    buyer, seller, pay = invoice["buyer"], invoice["seller"], invoice["payment"]
    overdue = invoice["display_status"] == "overdue"
    if reminder:
        subject = f"Reminder: invoice {invoice['number']} " + ("is overdue" if overdue else f"is due {fmt_date(invoice['due_date'])}")
    else:
        subject = (f"{invoice['title']} {invoice['number']} from {seller['legal_name']}: "
                   f"{dollars(invoice['total_cents'])} due {fmt_date(invoice['due_date'])}")
    gst_line = f" (includes GST of {dollars(invoice['gst_cents'])})" if invoice["gst_registered"] else ""
    body = [
        f"Hello {buyer.get('contact_name') or buyer['company_name']},",
        "",
        (f"This is a reminder that {invoice['title'].lower()} {invoice['number']} "
         + ("is now overdue." if overdue else "is due soon.")) if reminder
        else f"{invoice['title']} {invoice['number']} from {seller['legal_name']} is attached.",
        "",
        f"Amount: {dollars(invoice['total_cents'])}{gst_line}",
        f"Due: {fmt_date(invoice['due_date'])}",
        "",
        "Pay by bank transfer:",
        f"  Account name: {pay['account_name']}",
        f"  BSB: {pay['bsb']}",
        f"  Account number: {pay['account_number']}",
        f"  Reference: {pay['reference']}",
    ]
    if pay.get("payid"):
        body.append(f"  Or PayID: {pay['payid']}")
    body += ["", FRAUD_WARNING + (f" Phone: {seller['phone']}." if seller.get("phone") else "")]
    guarantee = _guarantee_text(invoice)
    if guarantee:
        body += ["", guarantee]
    body += ["", f"See this invoice in Mining Match: {base_url}/#/invoices/{invoice['id']}", "",
             f"{seller['legal_name']}, ABN {seller['abn']}"]
    try:
        mailer.send(buyer["email"], subject, "\n".join(body),
                    attachments=[(f"{invoice['number']}.pdf", render_pdf(invoice), "application/pdf")])
        return True
    except Exception:  # the invoice stands even if the email fails; an admin can send a reminder
        return False


def render_pdf(invoice: dict) -> bytes:
    """A4 PDF of an issued invoice (needs the reportlab package)."""
    from io import BytesIO
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle

    slate, line = colors.HexColor("#1f2b33"), colors.HexColor("#d3d9dc")
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=9.5, leading=13, textColor=slate)
    small = ParagraphStyle("small", parent=base, fontSize=8.5, leading=11.5, textColor=colors.HexColor("#56636b"))
    bold = ParagraphStyle("bold", parent=base, fontName="Helvetica-Bold")
    title = ParagraphStyle("title", parent=base, fontName="Helvetica-Bold", fontSize=20, leading=24)
    right = ParagraphStyle("right", parent=base, alignment=2)
    right_bold = ParagraphStyle("right_bold", parent=bold, alignment=2)

    def para(text, style=base):  # anything from users is escaped: Paragraph reads markup
        return Paragraph(escape(str(text)).replace("\n", "<br/>"), style)

    seller, buyer, pay = invoice["seller"], invoice["buyer"], invoice["payment"]
    out = BytesIO()
    doc = BaseDocTemplate(out, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                          bottomMargin=16 * mm, title=f"{invoice['title']} {invoice['number']}",
                          author=seller["legal_name"])
    # No frame padding, so text and tables share the same left and right edges
    doc.addPageTemplates([PageTemplate(id="page", frames=[Frame(
        doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="body")])])
    width = doc.width
    story = []
    status_note = {"paid": f"Paid {fmt_date(invoice['paid_on'])}" if invoice.get("paid_on") else "Paid",
                   "void": f"Void: {invoice.get('void_reason') or ''}"}.get(invoice["status"])
    head = Table([[para(seller["legal_name"], title), para(invoice["title"], ParagraphStyle("t2", parent=title, alignment=2))]],
                 colWidths=[width * 0.6, width * 0.4])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 2 * mm)]
    if status_note:
        story += [para(status_note, ParagraphStyle("stamp", parent=bold, fontSize=12, leading=15,
                                                   textColor=colors.HexColor("#b42318" if invoice["status"] == "void" else "#1d7347"))),
                  Spacer(1, 2 * mm)]
    seller_lines = [f"ABN {seller['abn']}", seller["address"], seller["email"]] + ([seller["phone"]] if seller.get("phone") else [])
    meta = [["Invoice number", invoice["number"]], ["Issue date", fmt_date(invoice["issue_date"])],
            ["Due date", fmt_date(invoice["due_date"])], ["Payment reference", pay["reference"]]]
    top = Table([[para("\n".join(seller_lines), small),
                  Table([[para(k, small), para(v, right_bold)] for k, v in meta], colWidths=[width * 0.2, width * 0.2])]],
                colWidths=[width * 0.6, width * 0.4])
    top.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [top, Spacer(1, 7 * mm), para("Bill to", bold)]
    buyer_lines = [buyer["company_name"]] + ([f"ABN {buyer['abn']}"] if buyer.get("abn") else []) \
        + ([buyer["address"]] if buyer.get("address") else []) + [buyer["email"]]
    story += [para("\n".join(buyer_lines)), Spacer(1, 7 * mm)]

    rows = [[para("Description", bold), para("Amount" + (" (excl. GST)" if invoice["gst_registered"] else ""), right_bold)]]
    rows += [[para(l["description"]), para(dollars(l["amount_cents"]), right)] for l in invoice["lines"]]
    totals = [["Subtotal", dollars(invoice["subtotal_cents"])]]
    if invoice["gst_registered"]:
        totals.append(["GST (10%)", dollars(invoice["gst_cents"])])
    totals.append(["Total (AUD)", dollars(invoice["total_cents"])])
    rows += [[para(k, right_bold if k.startswith("Total") else right), para(v, right_bold if k.startswith("Total") else right)]
             for k, v in totals]
    table = Table(rows, colWidths=[width * 0.75, width * 0.25])
    n = len(invoice["lines"])
    table.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, 0), 1, slate), ("LINEBELOW", (0, 1), (-1, n), 0.5, line),
        ("LINEABOVE", (0, -1), (-1, -1), 1, slate), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [table, Spacer(1, 3 * mm)]
    story.append(para("Total price includes GST." if invoice["gst_registered"] else "No GST has been charged.", small))
    story.append(Spacer(1, 7 * mm))

    pay_lines = [f"Account name: {pay['account_name']}", f"BSB: {pay['bsb']}", f"Account number: {pay['account_number']}",
                 f"Reference: {pay['reference']}"] + ([f"Or PayID: {pay['payid']}"] if pay.get("payid") else [])
    box = Table([[para("How to pay: bank transfer", bold)], [para("\n".join(pay_lines))], [para(FRAUD_WARNING, small)]],
                colWidths=[width])
    box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1, slate), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                             ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 5),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.append(box)
    guarantee = _guarantee_text(invoice)
    if guarantee:
        story += [Spacer(1, 4 * mm), para(guarantee, small)]
    doc.build(story)
    return out.getvalue()


# --------------------------------------------------------------------------
# Daily job
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    """python -m billing issue-due   (run once a day, e.g. from cron at 7am)"""
    import argparse

    import auth

    parser = argparse.ArgumentParser(prog="python -m billing", description="Mining Match invoicing jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("issue-due", help="issue scheduled invoices whose date has come, and email them")
    parser.parse_args(argv)
    config = BillingConfig.from_env()
    if config is None:
        print("Invoicing isn't set up: set MM_ABN and the other MM_ billing settings (see README).")
        return 1
    conn = dl.connect(os.environ.get("MM_DB", "mining_match.db"))
    dl.init_schema(conn)
    issued = issue_due_invoices(conn, config, auth.mailer_from_env(), os.environ.get("MM_BASE_URL", "http://localhost:8000"))
    print(f"Issued {len(issued)} invoice(s): {', '.join(issued)}" if issued else "No invoices due today.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
