"""Browser tests: the whole hiring flow through the web app, in real Chrome.

Needs Playwright (pip install playwright) and Chrome. Skipped if either is missing.
Run: python -m unittest test_web -v
Set MM_SHOTS=/some/folder to save phone and desktop screenshots there.
"""
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

import access_control as ac
import api
import billing
import auth
import data_layer as dl
from storage import LocalStorage

PASSWORD = "Dragline-shift-2026"
PDF = b"%PDF-1.4\n% Standard 11 statement of attainment (test)\n"
PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1280, "height": 900}
SHOTS = os.environ.get("MM_SHOTS")
BILLING = billing.BillingConfig(
    legal_name="Mining Match Pty Ltd", abn="51 824 753 556", address="PO Box 1, Emerald QLD 4720",
    email="accounts@miningmatch.example", bank_account_name="Mining Match Pty Ltd", bank_bsb="123-456",
    bank_account_number="12345678")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class HiringFlowInBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import uvicorn
        cls._patch = mock.patch.object(auth, "SCRYPT_N", 2 ** 10)
        cls._patch.start()
        cls.dir = Path(tempfile.mkdtemp())
        cls.outbox = auth.Outbox()
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        app = api.create_app(str(cls.dir / "mm.db"), storage=LocalStorage(cls.dir / "files"), mailer=cls.outbox,
                             base_url=cls.base, secure_cookies=False, billing_config=BILLING)
        conn = dl.connect(str(cls.dir / "mm.db"))
        ac.create_admin(conn, "admin@miningmatch.example", auth.hash_password(PASSWORD))
        conn.close()
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cls.port, log_level="warning"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(channel="chrome")
        except Exception as exc:  # pragma: no cover
            cls.pw.stop()
            cls.server.should_exit = True
            raise unittest.SkipTest(f"Chrome not available: {exc}")
        cls.problems = []

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        cls._patch.stop()
        shutil.rmtree(cls.dir)

    # ---------------------------------------------------------------- helpers
    def page(self, viewport=PHONE):
        context = self.browser.new_context(viewport=viewport, device_scale_factor=2, accept_downloads=True)
        page = context.new_page()
        # Expected statuses (401 when logged out, 404 before terms exist) are checked by the API tests;
        # here anything else counts: script errors, blocked content, server errors, missing files.
        page.on("console", lambda m: m.type == "error" and not m.text.startswith("Failed to load resource")
                and self.problems.append(f"console: {m.text}"))
        page.on("response", lambda r: (r.status >= 500 or (r.status == 404 and "/fee-terms/" not in r.url))
                and self.problems.append(f"{r.status} {r.url}"))
        page.on("pageerror", lambda e: self.problems.append(f"page error: {e}"))
        page.on("dialog", lambda d: (self.problems.append(f"dialog: {d.message}"), d.dismiss()))
        return page

    def shot(self, page, name, at=None):
        """Screen-sized capture, from the top of the page or around `at` (set MM_SHOTS to save them)."""
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            page.evaluate("document.getElementById('toast').className = 'toast'")
            if at is None:
                page.evaluate("window.scrollTo(0, 0)")
            else:
                at.scroll_into_view_if_needed()
            page.wait_for_timeout(300)  # let the toast finish fading out
            page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))

    def link_from_email(self, to):
        for msg in reversed(self.outbox.messages):
            m = re.search(r"(http\S+/#/verify-email\?token=\S+)", msg["body"])
            if msg["to"] == to and m:
                return m.group(1)
        self.fail(f"no confirmation email for {to}")

    def sign_up(self, page, role_label, email, fields):
        page.goto(f"{self.base}/#/signup")
        page.get_by_label(role_label).check()
        for label, value in fields.items():
            control = page.get_by_label(label, exact=True)
            if control.evaluate("el => el.tagName") == "SELECT":
                control.select_option(value)
            else:
                control.fill(value)
        page.get_by_label("Email", exact=True).fill(email)
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        self.shot(page, f"signup-{role_label[:5].strip()}")
        page.get_by_role("button", name="Create account").click()
        expect(page.get_by_role("heading", name="Check your email")).to_be_visible()
        page.goto(self.link_from_email(email))
        expect(page.get_by_role("heading", name="Email confirmed")).to_be_visible()
        self.assertNotIn("token", page.url)  # removed from the address bar
        page.get_by_role("link", name="Log in").click()
        self.log_in(page, email)

    def log_in(self, page, email, password=PASSWORD):
        page.get_by_label("Email", exact=True).fill(email)
        page.get_by_label("Password", exact=True).fill(password)
        page.get_by_role("button", name="Log in").click()

    def toast(self, page, text):
        expect(page.locator("#toast")).to_contain_text(text)

    # ------------------------------------------------------------------ test
    def test_hiring_flow(self):
        soon = (date.today() + timedelta(days=30)).isoformat()

        # Admin publishes placement terms
        admin = self.page(DESKTOP)
        admin.goto(self.base)
        expect(admin.get_by_role("heading", name="Your tickets, matched to real jobs.")).to_be_visible()
        self.log_in(admin, "admin@miningmatch.example")
        expect(admin.get_by_role("heading", name="Publish version 1")).to_be_visible()
        admin.get_by_label("Full terms").fill("1. Placement fee\nA fee of 12% of first-year base salary applies ...")
        self.shot(admin, "desktop-0-admin-terms")
        publish = admin.get_by_role("button", name="Publish version 1")
        publish.click()
        admin.get_by_role("button", name="Tap again: this can’t be changed later").click()
        expect(admin.get_by_role("heading", name="Current: version 1")).to_be_visible()

        # Candidate signs up on a phone and builds a profile
        cand = self.page(PHONE)
        cand.goto(self.base)
        self.shot(cand, "phone-1-login")
        self.sign_up(cand, "I’m looking for work", "jordan@example.com",
                     {"Full name": "Jordan Private", "State you live in": "QLD", "Mobile number (optional)": "0400 111 222"})
        expect(cand.get_by_role("heading", name="Jordan Private")).to_be_visible()
        expect(cand.locator("#nav")).to_be_visible()  # bottom navigation on phones

        def add(button, fields, kind=None):
            cand.get_by_role("button", name=button).click()
            for label, value in fields.items():
                cand.get_by_label(label, exact=True).fill(value)
            cand.get_by_role("button", name="Save", exact=True).click()

        add("Add a licence or ticket", {"Licence or ticket": "Standard 11", "Expiry date": "2031-05-01"})
        expect(cand.locator(".ticket", has_text="Standard 11")).to_contain_text("Valid")
        add("Add a licence or ticket", {"Licence or ticket": "Coal Board Medical", "Expiry date": soon})
        expect(cand.locator(".ticket", has_text="Coal Board Medical")).to_contain_text("Expires in 30 days")
        expect(cand.locator(".notice.warn")).to_contain_text("expiring within 90 days")
        # A known licence explains its renewal cycle and works out the expiry date from the issue date
        cand.get_by_role("button", name="Add a licence or ticket").click()
        licence = cand.get_by_label("Licence or ticket", exact=True)
        issued = cand.get_by_label("Date you got it or last renewed it", exact=True)
        expiry = cand.get_by_label("Expiry date", exact=True)
        licence.fill("White Card")  # advice only: no fixed cycle, so nothing to work out
        expect(cand.locator(".rule-note")).to_contain_text("In WA it lapses")
        expect(issued).to_be_hidden()
        licence.fill("Dogging (DG)")
        expect(cand.locator(".rule-note")).to_contain_text("High risk work licences last 5 years")
        expect(issued).to_be_visible()
        issued.fill("2024-03-10")
        expect(expiry).to_have_value("2029-03-10")
        expect(cand.locator(".field", has=expiry)).to_contain_text("Check it against your card")
        self.shot(cand, "phone-2a-licence-suggested-expiry", at=cand.locator(".rule-note"))
        expiry.fill("2029-03-01")  # a date they type themselves wins, even if the issue date changes
        issued.fill("2024-03-11")
        expect(expiry).to_have_value("2029-03-01")
        expect(cand.locator(".field", has=expiry)).to_contain_text("Leave empty if it doesn’t expire")
        cand.get_by_role("button", name="Save", exact=True).click()
        expect(cand.locator(".ticket", has_text="Dogging (DG)")).to_contain_text("1 Mar 2029")

        add("Add a qualification", {"Qualification": "Cert III Surface Extraction Operations"})
        add("Add a skill", {"Skill": "Dragline operation", "Years of experience": "6"})
        expect(cand.locator(".chip", has_text="Dragline operation")).to_contain_text("6 yrs")
        add("Add a role", {"Role": "Dragline Operator", "Started": "2018-01-01", "Finished": "2024-01-01"})
        expect(cand.locator("#history ~ ul")).to_contain_text("Dragline Operator")
        # Text a person types is shown as text, never run as code
        add("Add a qualification", {"Qualification": "<img src=x onerror=window.__xss=1>"})
        expect(cand.get_by_role("heading", name="<img src=x onerror=window.__xss=1>")).to_be_visible()
        self.assertIsNone(cand.evaluate("window.__xss"))

        cand.get_by_label("What is it?").select_option("licence")
        cand.get_by_label("File", exact=True).set_input_files({"name": "standard-11.pdf", "mimeType": "application/pdf", "buffer": PDF})
        cand.get_by_role("button", name="Upload").click()
        self.toast(cand, "standard-11.pdf uploaded.")
        expect(cand.locator(".row", has_text="standard-11.pdf")).to_contain_text("Not checked yet")
        expect(cand.get_by_label("Email me before my licences expire")).to_be_checked()
        cand.get_by_label("Open to FIFO or DIDO rosters").check()
        cand.get_by_role("button", name="Save details").click()
        self.toast(cand, "Details saved.")
        cand.get_by_role("heading", name="Jordan Private").scroll_into_view_if_needed()
        self.shot(cand, "phone-2-candidate-profile")

        # Employer signs up and posts a job
        emp = self.page(DESKTOP)
        self.sign_up(emp, "I’m hiring", "hr@alpha.example", {"Company name": "Alpha Mining"})
        expect(emp.get_by_role("heading", name="Jobs")).to_be_visible()
        emp.get_by_role("link", name="Post a job").first.click()
        emp.get_by_label("Job title").fill("Dragline Operator")
        emp.get_by_label("State", exact=True).select_option("QLD")
        emp.get_by_label("Start date").fill("2030-12-01")
        lic = emp.get_by_label("Licences they must hold", exact=True)
        lic.fill("Standard 11")
        lic.press("Enter")
        qual = emp.get_by_label("Qualifications they must hold", exact=True)
        qual.fill("Cert III Surface Extraction Operations")
        qual.press("Enter")
        skill = emp.get_by_label("Skills needed", exact=True)
        skill.fill("Dragline operation")
        emp.get_by_label("Minimum years").first.fill("3")
        skill.press("Enter")
        emp.get_by_label("Status").select_option("open")
        self.shot(emp, "desktop-1a-job-form")
        emp.get_by_role("button", name="Save job").click()
        self.toast(emp, "Job saved.")
        row = emp.locator(".row", has_text="Meets the requirements")
        expect(row).to_have_count(1)
        expect(row.locator(".score-num")).to_have_text("100")
        self.assertNotIn("Jordan", emp.locator("main").inner_text())  # anonymised
        row.get_by_role("button", name="Add to shortlist").click()
        expect(row.get_by_role("link", name="On shortlist")).to_be_visible()
        self.shot(emp, "desktop-1-ranked-candidates")

        row.get_by_role("link", name=re.compile("Candidate ")).click()
        expect(emp.locator(".ticket", has_text="Standard 11")).to_be_visible()
        emp.get_by_role("button", name="Ask to see contact details").click()
        expect(emp.locator("aside")).to_contain_text("Request sent")

        # Candidate approves on their phone
        cand.get_by_role("link", name="Requests").click()
        req = cand.locator(".row", has_text="Alpha Mining")
        expect(req).to_contain_text("About: Dragline Operator")
        req.get_by_role("button", name="Approve").click()
        expect(req).to_contain_text("You approved")
        self.shot(cand, "phone-3-requests")

        # Employer: approval alone isn't enough; accept terms, then unlock
        emp.reload()
        aside = emp.locator("aside")
        expect(aside).to_contain_text("12% of base salary")
        self.assertNotIn("Jordan", emp.locator("main").inner_text())
        self.shot(emp, "desktop-1b-approved-unlock")
        aside.get_by_role("button", name="Unlock contact details").click()
        expect(aside.locator(".form-error")).to_contain_text("Tick the box")
        aside.get_by_label("I accept the placement terms (version 1)").check()
        aside.get_by_role("button", name="Unlock contact details").click()
        expect(emp.get_by_role("heading", name="Jordan Private", exact=True)).to_be_visible()
        expect(aside.get_by_role("link", name="jordan@example.com")).to_be_visible()
        with emp.expect_download() as info:
            emp.locator(".row", has_text="standard-11.pdf").get_by_role("button", name="Download").click()
        self.assertEqual(Path(info.value.path()).read_bytes(), PDF)
        self.assertEqual(info.value.suggested_filename, "standard-11.pdf")
        self.shot(emp, "desktop-2-candidate-unlocked")

        # Employer records the hire from Billing, checks the fee, confirms: the invoice is issued
        emp.get_by_role("link", name="Billing", exact=True).click()
        emp.locator(".row", has_text="Jordan Private").get_by_role("link", name="Record a hire").click()
        expect(emp.get_by_role("heading", name="Record a hire: Jordan Private")).to_be_visible()
        emp.get_by_label("Start date").fill(BILLING.today().isoformat())
        emp.get_by_label("First-year base salary (AUD)").fill("120,000")
        emp.get_by_role("button", name="Work out the fee").click()
        fee = emp.locator(".fee-preview")
        expect(fee).to_contain_text("$15,840.00")
        expect(fee).to_contain_text("12% of $120,000.00: $14,400.00")
        expect(fee).to_contain_text("GST: $1,440.00")
        self.shot(emp, "desktop-3-hire-fee")
        fee.get_by_role("button", name="Confirm hire").click()
        expect(emp.get_by_role("heading", name="Tax invoice MM-00001")).to_be_visible()
        expect(emp.locator(".pay-box")).to_contain_text("123-456")
        expect(emp.locator(".pay-box")).to_contain_text("MM-00001")
        self.shot(emp, "desktop-4-invoice")
        with emp.expect_download() as pdf:
            emp.get_by_role("button", name="Download PDF").click()
        self.assertTrue(Path(pdf.value.path()).read_bytes().startswith(b"%PDF"))
        emailed = [m for m in self.outbox.messages if m["to"] == "hr@alpha.example" and m["attachments"]]
        self.assertEqual(emailed[-1]["attachments"][0][0], "MM-00001.pdf")
        emp.get_by_role("link", name="Billing", exact=True).click()
        expect(emp.locator('[aria-labelledby="inv-title"] .row', has_text="MM-00001")).to_contain_text("Due")
        expect(emp.locator('[aria-labelledby="hires-title"] .row', has_text="Jordan Private")).to_contain_text("Hire recorded")
        self.shot(emp, "desktop-4b-billing")
        emp.get_by_role("link", name="Shortlists").click()
        emp.get_by_role("link", name="Dragline Operator shortlist").click()
        emp.get_by_label("Status for Jordan Private").select_option("interviewing")
        self.toast(emp, "Status saved.")

        # Candidate sees the job ranked for them
        cand.get_by_role("link", name="Jobs").click()
        match = cand.locator(".row", has_text="Dragline Operator")
        expect(match).to_contain_text("You meet the requirements")
        expect(match).to_contain_text("Alpha Mining")
        self.shot(cand, "phone-4-jobs-for-you")

        # Admin checks the document, then suspends the employer
        admin.get_by_role("link", name="Documents").click()
        admin.locator(".row", has_text="standard-11.pdf").get_by_role("button", name="Mark as checked").click()
        expect(admin.get_by_text("Nothing waiting to be checked.")).to_be_visible()
        admin.get_by_role("link", name="Invoices").click()
        expect(admin.locator(".total-figure").first).to_have_text("$15,840.00")
        admin.get_by_role("link", name="MM-00001").click()
        admin.get_by_role("button", name="Mark as paid").click()
        expect(admin.locator(".invoice-doc")).to_contain_text("Paid")
        self.shot(admin, "desktop-5-admin-invoice-paid")
        admin.get_by_role("link", name="All invoices").click()
        expect(admin.locator("table.data")).to_contain_text("Paid")

        # Admin sends today's licence reminders; the candidate turns them off from the email
        admin.get_by_role("link", name="Reminders").click()
        expect(admin.locator(".row", has_text="Dogging (DG)")).to_contain_text("Every 5 yrs")
        admin.get_by_role("button", name="Send today’s reminders now").click()
        self.toast(admin, "Sent 1 email about 1 licence.")
        sent = admin.locator("table.data tbody tr", has_text="Coal Board Medical")
        expect(sent).to_contain_text("Jordan Private")
        expect(sent).to_contain_text("30 days")
        self.shot(admin, "desktop-6-admin-reminders")
        reminder = [m for m in self.outbox.messages if m["to"] == "jordan@example.com"][-1]
        self.assertEqual(reminder["subject"], "Your Coal Board Medical expires in 30 days")
        self.assertIn("Employers can't count an expired licence", reminder["body"])
        admin.get_by_role("button", name="Send today’s reminders now").click()
        self.toast(admin, "Nothing due today.")  # never the same reminder twice

        cand.goto(re.search(r"(http\S+/#/unsubscribe\?token=\S+)", reminder["body"]).group(1))
        expect(cand.get_by_role("heading", name="Reminders turned off")).to_be_visible()
        self.assertNotIn("token", cand.url)
        self.shot(cand, "phone-5-unsubscribed")
        cand.get_by_role("link", name="Go to your profile").click()
        expect(cand.get_by_label("Email me before my licences expire")).not_to_be_checked()
        expect(cand.locator("#licences ~ .muted")).to_contain_text("Expiry reminders are off")

        admin.get_by_role("link", name="Accounts").click()
        admin.get_by_label("Find").fill("alpha")
        admin.get_by_role("button", name="Search").click()
        account = admin.locator(".row", has_text="hr@alpha.example")
        account.get_by_role("button", name="Suspend").click()
        account.get_by_role("button", name="Tap again to suspend").click()
        self.toast(admin, "Account suspended")
        emp.get_by_role("link", name="Jobs").click()
        expect(emp.get_by_role("heading", name="Log in")).to_be_visible()  # logged out straight away

        # The employer asked (by email) to close their account; the invoice is paid, so it can be
        account = admin.locator(".row", has_text="Alpha Mining")
        account.get_by_role("button", name="Close account").click()
        account.get_by_role("button", name="Tap again: jobs close, sign-in is deleted").click()
        self.toast(admin, "Account closed. Invoices and hires are kept.")
        expect(admin.locator(".row", has_text="Alpha Mining")).to_contain_text("Closed")
        expect(admin.locator(".row", has_text="hr@alpha.example")).to_have_count(0)
        self.shot(admin, "desktop-7-admin-employer-closed")

        # Candidate logs out from the account page
        cand.get_by_role("link", name="Account").click()
        cand.get_by_role("button", name="Log out").click()
        expect(cand.get_by_role("heading", name="Log in")).to_be_visible()

        self.assertEqual(self.problems, [], "\n".join(self.problems))

    def test_wrong_password_and_password_reset(self):
        page = self.page(PHONE)
        page.goto(self.base)
        self.log_in(page, "nobody@example.com", "wrong-password-1")
        expect(page.locator(".form-error")).to_contain_text("wrong email or password")
        page.get_by_role("link", name="Forgot your password?").click()
        expect(page.get_by_role("heading", name="Reset your password")).to_be_visible()  # new screen is in
        page.get_by_label("Email", exact=True).fill("admin@miningmatch.example")
        page.get_by_role("button", name="Send reset link").click()
        expect(page.get_by_role("heading", name="Check your email")).to_be_visible()
        link = re.search(r"(http\S+/#/reset-password\?token=\S+)", self.outbox.messages[-1]["body"]).group(1)
        page.goto(link)
        page.get_by_label("New password").fill("Brand-New-Pass-2027")
        page.get_by_label("Type it again").fill("Brand-New-Pass-2026")
        page.get_by_role("button", name="Save new password").click()
        expect(page.locator(".form-error")).to_contain_text("don’t match")
        page.get_by_label("Type it again").fill("Brand-New-Pass-2027")
        page.get_by_role("button", name="Save new password").click()
        expect(page.get_by_role("heading", name="Password saved")).to_be_visible()
        page.get_by_role("link", name="Log in").click()
        self.log_in(page, "admin@miningmatch.example", "Brand-New-Pass-2027")
        expect(page.get_by_role("heading", name="Admin")).to_be_visible()
        # put the password back for the other test
        conn = dl.connect(str(self.dir / "mm.db"))
        conn.execute("UPDATE users SET password_hash = ? WHERE email = ?", (auth.hash_password(PASSWORD), "admin@miningmatch.example"))
        conn.commit()
        conn.close()
        self.assertEqual([p for p in self.problems if "401" not in p], [])


if __name__ == "__main__":
    unittest.main()
