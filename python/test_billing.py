"""Tests for billing (placement fees, invoices, bank transfer). Run: python -m unittest -v"""
import inspect
import io
import json
import sqlite3
import unittest
from datetime import date

import access_control as ac
import auth
import billing
import data_layer as dl
from test_access_control import World

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None

CFG = billing.BillingConfig(
    legal_name="Mining Match Pty Ltd", abn="51 824 753 556", address="PO Box 1, Emerald QLD 4720",
    email="accounts@miningmatch.example", phone="07 4900 0000", bank_account_name="Mining Match Pty Ltd",
    bank_bsb="123456", bank_account_number="12345678")
URL = "https://miningmatch.example"
UNLOCKED = "2026-11-19 09:00:00"
TODAY = date(2026, 11, 25)


def pdf_text(data: bytes) -> str:
    return " ".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)


def billing_world(record=True):
    """World + c1 unlocked by e1 on 19 Nov 2026 (+ a hire that started then, invoiced on TODAY)."""
    w = World()
    w.unlock()
    w.conn.execute("UPDATE introductions SET unlocked_at = ?", (UNLOCKED,))
    w.conn.commit()
    w.outbox = auth.Outbox()
    if record:
        r = billing.record_hire(w.conn, CFG, w.outbox, URL, w.p["e1"], w.c1, "2026-11-19", 120000,
                                job_id=w.j1, today=TODAY)
        w.placement_id, w.invoice_id = r["placement"]["id"], r["invoice"]["id"]
    return w


class Helpers(unittest.TestCase):
    def test_abn_check_digits(self):
        self.assertEqual(billing.abn_digits("51 824 753 556"), "51824753556")
        self.assertEqual(billing.format_abn("51824753556"), "51 824 753 556")
        for bad in ("51 824 753 557", "1234", "01824753556", "", None, "5182475355a"):
            with self.subTest(abn=bad), self.assertRaises(ValueError):
                billing.abn_digits(bad)

    def test_money_parsing(self):
        self.assertEqual(billing.to_cents("120,000"), 12_000_000)
        self.assertEqual(billing.to_cents("$1,234.56"), 123_456)
        self.assertEqual(billing.to_cents(95000.5), 9_500_050)
        for bad in (0, -5, "1.234", True, "abc", None, 6_000_000, float("nan")):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                billing.to_cents(bad)
        self.assertEqual(billing.dollars(1_584_000), "$15,840.00")

    def test_gst_rounds_half_a_cent_up(self):
        self.assertEqual(billing.gst_on(123_445), 12_345)  # 12,344.5 cents
        self.assertEqual(billing.gst_on(123_444), 12_344)
        self.assertEqual(billing.gst_on(5), 1)
        self.assertEqual(billing.gst_on(4), 0)

    def test_add_months_handles_short_months(self):
        self.assertEqual(billing.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(billing.add_months(date(2026, 11, 19), 12), date(2027, 11, 19))

    def test_config_is_checked(self):
        good = dict(legal_name="X", abn="51824753556", address="A", email="a@b.co", bank_account_name="X",
                    bank_bsb="123-456", bank_account_number="1234567")
        self.assertEqual(billing.BillingConfig(**good).bank_bsb, "123-456")
        for field, bad in [("abn", "123"), ("bank_bsb", "12345"), ("bank_account_number", "12"),
                           ("email", "nope"), ("legal_name", " "), ("invoice_prefix", "mm"),
                           ("payment_terms_days", 0), ("timezone", "Mars/Base")]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                billing.BillingConfig(**{**good, field: bad})
        self.assertIsNone(billing.BillingConfig.from_env({}))
        env = {"MM_ABN": "51824753556", "MM_BUSINESS_NAME": "X", "MM_BUSINESS_ADDRESS": "A",
               "MM_BILLING_EMAIL": "a@b.co", "MM_BANK_ACCOUNT_NAME": "X", "MM_BANK_BSB": "123456",
               "MM_BANK_ACCOUNT": "1234567", "MM_GST_REGISTERED": "0"}
        self.assertFalse(billing.BillingConfig.from_env(env).gst_registered)


class Fees(unittest.TestCase):
    def setUp(self):
        self.w = billing_world(record=False)
        self.conn = self.w.conn

    def tearDown(self):
        self.conn.close()

    def preview(self, start, salary, who="e1", candidate=None):
        return billing.preview_hire(self.conn, CFG, self.w.p[who], candidate or self.w.c1, start, salary, today=TODAY)

    def test_fee_gst_and_dates(self):
        q = self.preview("2026-12-01", 120000)
        self.assertEqual((q["fee_cents"], q["gst_cents"], q["total_cents"]), (1_440_000, 144_000, 1_584_000))
        self.assertEqual((q["issue_date"], q["due_date"]), ("2026-12-01", "2026-12-15"))
        self.assertTrue(q["in_window"])
        self.assertEqual(q["terms_version"], 1)

    def test_fee_window(self):
        self.assertTrue(self.preview("2027-11-19", 100000)["in_window"])
        late = self.preview("2027-11-20", 100000)
        self.assertEqual((late["in_window"], late["fee_cents"], late["issue_date"]), (False, 0, None))

    def test_minimum_fee_from_the_terms_that_applied(self):
        v2 = ac.publish_fee_terms(self.conn, self.w.p["admin"], fee_percent=12, fee_basis="Base salary",
                                  window_months=12, guarantee_days=90, terms_text="v2", min_fee_aud=9000)
        ac.accept_fee_terms(self.conn, self.w.p["e1"], v2)
        ac.set_contact_release(self.conn, self.w.p["c2"], self.w.e1, True)
        ac.unlock_candidate(self.conn, self.w.p["e1"], self.w.c2, v2)
        self.conn.execute("UPDATE introductions SET unlocked_at = ?", (UNLOCKED,))
        self.conn.commit()
        q = self.preview("2026-12-01", 50000, candidate=self.w.c2)
        self.assertEqual((q["percent_fee_cents"], q["fee_cents"], q["minimum_applied"]), (600_000, 900_000, True))
        self.assertEqual(self.preview("2026-12-01", 50000)["fee_cents"], 600_000)  # c1 is still under v1: no minimum

    def test_rejections(self):
        with self.assertRaises(ValueError):
            self.preview("2026-11-18", 100000)  # before the unlock
        with self.assertRaises(ValueError):
            self.preview("2029-01-01", 100000)  # too far ahead
        with self.assertRaises(ValueError):
            self.preview("not a date", 100000)
        with self.assertRaises(ValueError):
            self.preview("2026-12-01", 0)
        with self.assertRaises(dl.DataError):
            self.preview("2026-12-01", 100000, who="e2")  # e2 never unlocked c1

    def test_not_set_up(self):
        with self.assertRaises(billing.BillingNotSetUp):
            billing.record_hire(self.conn, None, self.w.outbox, URL, self.w.p["e1"], self.w.c1, "2026-12-01", 1, today=TODAY)
        self.assertEqual(billing.billing_settings(self.conn, None, self.w.p["e1"]), {"enabled": False})


class Invoices(unittest.TestCase):
    def setUp(self):
        self.w = billing_world(record=False)
        self.conn = self.w.conn
        self.out = self.w.outbox

    def tearDown(self):
        self.conn.close()

    def hire(self, start, salary=120000, who="e1", today=TODAY, **kw):
        return billing.record_hire(self.conn, CFG, self.out, URL, self.w.p[who], self.w.c1, start, salary, today=today, **kw)

    def test_future_start_is_scheduled_then_issued_on_the_day(self):
        r = self.hire("2026-12-01", job_id=self.w.j1)
        inv = r["invoice"]
        self.assertEqual((inv["status"], inv["number"], inv["display_status"]), ("scheduled", None, "scheduled"))
        self.assertEqual(self.out.messages, [])
        self.assertEqual(billing.issue_due_invoices(self.conn, CFG, self.out, URL, today=date(2026, 11, 30)), [])
        self.assertEqual(billing.issue_due_invoices(self.conn, CFG, self.out, URL, today=date(2026, 12, 1)), ["MM-00001"])
        (msg,) = self.out.messages
        self.assertEqual(msg["to"], "e1@example.com")
        self.assertIn("Tax invoice MM-00001 from Mining Match Pty Ltd: $15,840.00 due 15 Dec 2026", msg["subject"])
        for text in ("BSB: 123-456", "Account number: 12345678", "Reference: MM-00001", "never change by email",
                     f"{URL}/#/invoices/{inv['id']}", "Replacement guarantee: 90 days"):
            self.assertIn(text, msg["body"])
        (name, data, mime) = msg["attachments"][0]
        self.assertEqual((name, mime), ("MM-00001.pdf", "application/pdf"))
        self.assertTrue(data.startswith(b"%PDF"))
        issued = billing.get_invoice(self.conn, CFG, self.w.p["e1"], inv["id"], today=date(2026, 12, 1))
        self.assertEqual((issued["issue_date"], issued["due_date"], issued["display_status"]), ("2026-12-01", "2026-12-15", "due"))

    def test_late_report_is_issued_today_not_backdated(self):
        inv = self.hire("2026-11-19")["invoice"]
        self.assertEqual((inv["status"], inv["number"], inv["issue_date"], inv["due_date"]),
                         ("issued", "MM-00001", "2026-11-25", "2026-12-09"))

    def test_daily_job_running_late_never_backdates(self):
        inv = self.hire("2026-12-01")["invoice"]
        self.assertEqual(billing.issue_due_invoices(self.conn, CFG, self.out, URL, today=date(2026, 12, 3)), ["MM-00001"])
        issued = billing.get_invoice(self.conn, CFG, self.w.p["e1"], inv["id"], today=date(2026, 12, 3))
        self.assertEqual((issued["issue_date"], issued["due_date"]), ("2026-12-03", "2026-12-17"))  # full 14 days to pay

    def test_invoice_content_meets_tax_invoice_rules(self):
        inv = self.hire("2026-11-19", job_id=self.w.j1)["invoice"]
        self.assertEqual(inv["title"], "Tax invoice")
        self.assertEqual((inv["seller"]["legal_name"], inv["seller"]["abn"]), ("Mining Match Pty Ltd", "51 824 753 556"))
        self.assertEqual(inv["buyer"]["company_name"], "Alpha Mining")  # buyer's identity (needed at $1,000+)
        self.assertEqual((inv["subtotal_cents"], inv["gst_cents"], inv["total_cents"]), (1_440_000, 144_000, 1_584_000))
        (line,) = inv["lines"]
        self.assertEqual(line["description"],
                         "Placement fee: Jordan Private, Dragline Operator, starting 19 Nov 2026. 12% of base salary, "
                         "excluding allowances, $120,000.00 (fee terms version 1).")
        self.assertEqual(inv["payment"]["reference"], "MM-00001")
        if PdfReader:
            text = pdf_text(self.out.messages[-1]["attachments"][0][1])
            for part in ("Tax invoice", "MM-00001", "ABN 51 824 753 556", "Alpha Mining", "GST (10%)", "$1,440.00",
                         "$15,840.00", "Total price includes GST", "BSB: 123-456", "Reference: MM-00001"):
                self.assertIn(part, text)

    def test_not_registered_for_gst(self):
        cfg = billing.BillingConfig(**{**CFG.__dict__, "gst_registered": False})
        inv = billing.record_hire(self.conn, cfg, self.out, URL, self.w.p["e1"], self.w.c1, "2026-11-19", 120000,
                                  today=TODAY)["invoice"]
        self.assertEqual((inv["title"], inv["gst_cents"], inv["total_cents"]), ("Invoice", 0, 1_440_000))
        if PdfReader:
            text = pdf_text(self.out.messages[-1]["attachments"][0][1])
            self.assertIn("No GST has been charged", text)
            self.assertNotIn("Tax invoice", text)

    def test_numbers_are_sequential_across_kinds(self):
        self.hire("2026-11-19")
        services = billing.create_services_invoice(
            self.conn, CFG, self.out, URL, self.w.p["admin"], self.w.e2,
            [{"description": "Search subscription, December 2026", "amount": "199"},
             {"description": "Featured job listing", "amount": 49.95}], today=TODAY)
        self.assertEqual(services["number"], "MM-00002")
        self.assertEqual((services["subtotal_cents"], services["gst_cents"], services["total_cents"]), (24_895, 2_490, 27_385))
        with self.assertRaises(ValueError):
            billing.create_services_invoice(self.conn, CFG, self.out, URL, self.w.p["admin"], self.w.e2, [], today=TODAY)
        with self.assertRaises(ValueError):
            billing.create_services_invoice(self.conn, CFG, self.out, URL, self.w.p["admin"], self.w.e2,
                                            [{"description": "x", "amount": -1}], today=TODAY)

    def test_duplicate_hire_and_cancelling(self):
        r = self.hire("2026-12-01")
        with self.assertRaises(dl.DuplicateError):
            self.hire("2026-12-01")
        cancelled = billing.cancel_hire(self.conn, CFG, self.w.p["e1"], r["placement"]["id"], "Didn't start")
        self.assertEqual((cancelled["status"], cancelled["invoice_status"]), ("cancelled", "void"))
        again = self.hire("2026-12-08")  # can be recorded again once cancelled
        self.assertEqual(again["placement"]["start_date"], "2026-12-08")

    def test_employers_cannot_cancel_once_issued(self):
        r = self.hire("2026-11-19")
        with self.assertRaises(ac.AccessDenied):
            billing.cancel_hire(self.conn, CFG, self.w.p["e1"], r["placement"]["id"], "changed our mind")
        billing.cancel_hire(self.conn, CFG, self.w.p["admin"], r["placement"]["id"], "Didn't start")
        inv = billing.get_invoice(self.conn, CFG, self.w.p["admin"], r["invoice"]["id"], today=TODAY)
        self.assertEqual((inv["status"], inv["number"]), ("void", "MM-00001"))  # keeps its number

    def test_issued_invoices_are_locked(self):
        inv = self.hire("2026-11-19")["invoice"]
        for stmt in ("UPDATE invoices SET subtotal_cents = 1, gst_cents = 0, total_cents = 1",
                     "UPDATE invoices SET payment_json = '{}'",
                     "UPDATE invoice_lines SET amount_cents = 1", "DELETE FROM invoices"):
            with self.subTest(stmt=stmt), self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute(stmt)
        self.conn.rollback()
        billing.update_billing_details(self.conn, self.w.p["e1"], abn="51 824 753 556", billing_address="1 New St")
        again = billing.get_invoice(self.conn, CFG, self.w.p["e1"], inv["id"], today=TODAY)
        self.assertIsNone(again["buyer"]["abn"])  # details as they were when issued

    def test_paid_overdue_and_reminders(self):
        inv = self.hire("2026-11-19")["invoice"]
        late = date(2026, 12, 20)
        self.assertEqual(billing.list_invoices(self.conn, CFG, self.w.p["e1"], status="overdue", today=late)[0]["id"], inv["id"])
        reminded = billing.send_reminder(self.conn, CFG, self.out, URL, self.w.p["admin"], inv["id"], today=late)
        self.assertEqual(reminded["reminders_sent"], 1)
        self.assertEqual(self.out.messages[-1]["subject"], "Reminder: invoice MM-00001 is overdue")
        with self.assertRaises(dl.DataError):  # once a day
            billing.send_reminder(self.conn, CFG, self.out, URL, self.w.p["admin"], inv["id"], today=late)
        with self.assertRaises(ValueError):
            billing.mark_paid(self.conn, CFG, self.w.p["admin"], inv["id"], "2026-12-25", today=late)  # future
        paid = billing.mark_paid(self.conn, CFG, self.w.p["admin"], inv["id"], "2026-12-19", "CBA transfer", today=late)
        self.assertEqual((paid["status"], paid["display_status"], paid["paid_on_time"]), ("paid", "paid", False))
        for action in (lambda: billing.mark_paid(self.conn, CFG, self.w.p["admin"], inv["id"], "2026-12-19", today=late),
                       lambda: billing.void_invoice(self.conn, CFG, self.w.p["admin"], inv["id"], "oops"),
                       lambda: billing.send_reminder(self.conn, CFG, self.out, URL, self.w.p["admin"], inv["id"], today=date(2026, 12, 21))):
            with self.assertRaises(dl.DataError):
                action()

    def test_paid_on_time(self):
        inv = self.hire("2026-11-19")["invoice"]
        paid = billing.mark_paid(self.conn, CFG, self.w.p["admin"], inv["id"], "2026-12-09", today=date(2026, 12, 10))
        self.assertTrue(paid["paid_on_time"])

    def test_email_failure_keeps_the_invoice(self):
        class Broken:
            def send(self, *a, **kw):
                raise OSError("smtp down")
        inv = billing.record_hire(self.conn, CFG, Broken(), URL, self.w.p["e1"], self.w.c1, "2026-11-19", 120000,
                                  today=TODAY)["invoice"]
        self.assertEqual((inv["status"], inv["emailed"]), ("issued", False))
        detail = self.conn.execute("SELECT detail FROM audit_log WHERE action = 'billing.invoice.issue'").fetchone()[0]
        self.assertEqual(detail, "MM-00001: email failed")

    def test_markup_in_names_is_safe_in_the_pdf(self):
        self.conn.execute("UPDATE employers SET company_name = 'Alpha & <Sons> Pty Ltd' WHERE id = ?", (self.w.e1,))
        self.conn.commit()
        inv = self.hire("2026-11-19")["invoice"]
        filename, data = billing.invoice_pdf(self.conn, CFG, self.w.p["e1"], inv["id"])
        self.assertEqual(filename, "MM-00001.pdf")
        if PdfReader:
            self.assertIn("Alpha & <Sons> Pty Ltd", pdf_text(data))

    def test_billing_details(self):
        d = billing.update_billing_details(self.conn, self.w.p["e1"], abn="51824753556",
                                           billing_email="Accounts@Alpha.example", billing_address="1 Pit Rd, Moranbah QLD")
        self.assertEqual((d["abn"], d["billing_email"]), ("51 824 753 556", "accounts@alpha.example"))
        for kw in ({"abn": "12 345 678 901"}, {"billing_email": "nope"}, {"billing_address": "x" * 301}):
            with self.subTest(**{k: str(v)[:20] for k, v in kw.items()}), self.assertRaises(ValueError):
                billing.update_billing_details(self.conn, self.w.p["e1"], **kw)
        inv = self.hire("2026-11-19")["invoice"]
        self.assertEqual(self.out.messages[-1]["to"], "accounts@alpha.example")
        self.assertEqual(inv["buyer"]["abn"], "51 824 753 556")

    def test_audit_log_has_no_salary_or_names(self):
        self.hire("2026-11-19", job_id=self.w.j1)
        dump = json.dumps([dict(r) for r in self.conn.execute("SELECT * FROM audit_log WHERE action LIKE 'billing.%'")])
        for secret in ("120000", "120,000", "Jordan", "14400", "15,840"):
            self.assertNotIn(secret, dump)


# --------------------------------------------------------------------------
# Who may do what. Each cell runs in a fresh world: c1 unlocked by e1, with an issued invoice.
# --------------------------------------------------------------------------
def _for_admin(w, p):
    return w.e1 if p.role == "admin" else None


GRID = [
    ("billing_settings", lambda w, p: billing.billing_settings(w.conn, CFG, p), {"e1", "e2", "admin"}, {}),
    ("get_billing_details", lambda w, p: billing.get_billing_details(w.conn, p, employer_id=_for_admin(w, p)),
     {"e1", "e2", "admin"}, {}),
    ("update_billing_details", lambda w, p: billing.update_billing_details(
        w.conn, p, abn="51824753556", employer_id=_for_admin(w, p)), {"e1", "e2", "admin"}, {}),
    ("preview_hire", lambda w, p: billing.preview_hire(w.conn, CFG, p, w.c1, "2026-12-01", 100000,
                                                       employer_id=_for_admin(w, p), today=TODAY),
     {"e1", "admin"}, {"e2": dl.DataError}),
    ("record_hire", lambda w, p: (billing.cancel_hire(w.conn, CFG, w.p["admin"], w.placement_id, "redo"),
                                  billing.record_hire(w.conn, CFG, w.outbox, URL, p, w.c1, "2026-12-01", 100000,
                                                      employer_id=_for_admin(w, p), today=TODAY)),
     {"e1", "admin"}, {"e2": dl.DataError}),
    ("cancel_hire", lambda w, p: billing.cancel_hire(w.conn, CFG, p, w.placement_id, "Didn't start"), {"admin"}, {}),
    ("list_placements", lambda w, p: billing.list_placements(w.conn, p), {"e1", "e2", "admin"}, {}),
    ("list_invoices", lambda w, p: billing.list_invoices(w.conn, CFG, p, today=TODAY), {"e1", "e2", "admin"}, {}),
    ("get_invoice", lambda w, p: billing.get_invoice(w.conn, CFG, p, w.invoice_id, today=TODAY), {"e1", "admin"}, {}),
    ("invoice_pdf", lambda w, p: billing.invoice_pdf(w.conn, CFG, p, w.invoice_id), {"e1", "admin"}, {}),
    ("issue_due_invoices", lambda w, p: billing.issue_due_invoices(w.conn, CFG, w.outbox, URL, p, today=TODAY), {"admin"}, {}),
    ("create_services_invoice", lambda w, p: billing.create_services_invoice(
        w.conn, CFG, w.outbox, URL, p, w.e1, [{"description": "Listing", "amount": 50}], today=TODAY), {"admin"}, {}),
    ("mark_paid", lambda w, p: billing.mark_paid(w.conn, CFG, p, w.invoice_id, "2026-11-25", today=TODAY), {"admin"}, {}),
    ("void_invoice", lambda w, p: billing.void_invoice(w.conn, CFG, p, w.invoice_id, "Wrong salary"), {"admin"}, {}),
    ("send_reminder", lambda w, p: billing.send_reminder(w.conn, CFG, w.outbox, URL, p, w.invoice_id, today=TODAY), {"admin"}, {}),
]


class BillingPermissions(unittest.TestCase):
    def test_every_caller_against_every_function(self):
        for name, call, allowed, other in GRID:
            for who in ("c1", "c2", "e1", "e2", "admin"):
                with self.subTest(function=name, caller=who):
                    w = billing_world()
                    try:
                        if who in allowed:
                            call(w, w.p[who])
                        elif who in other:
                            with self.assertRaises(other[who]):
                                call(w, w.p[who])
                        else:
                            with self.assertRaises(ac.AccessDenied):
                                call(w, w.p[who])
                            self.assertTrue(w.audit(outcome="denied", user_id=w.users[who]))
                    finally:
                        w.conn.close()

    def test_grid_covers_every_guarded_function(self):
        guarded = {name for name, fn in inspect.getmembers(billing, inspect.isfunction)
                   if fn.__module__ == "billing" and "principal" in inspect.signature(fn).parameters}
        self.assertEqual(guarded, {row[0] for row in GRID})


if __name__ == "__main__":
    unittest.main()
