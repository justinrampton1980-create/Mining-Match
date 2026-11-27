"""End-to-end tests through HTTP. Run: python -m unittest -v   (from the python/ folder)"""
import io
import json
import re
import shutil
import tempfile
import time
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import access_control as ac
import api
import auth
import two_factor
import billing
import data_layer as dl
import reminders
from storage import LocalStorage, sniff_type

PASSWORD = "Dragline-shift-2026"
PDF = b"%PDF-1.4\n% Mining Match test document\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JOB = {
    "title": "Dragline Operator", "location": "QLD", "start_date": "2030-12-01",
    "required_licences": ["Standard 11"], "required_qualifications": ["Cert III Surface Extraction"],
    "required_skills": [{"name": "Dragline operation", "min_years": 3}], "min_experience_years": 2,
}
BILLING = billing.BillingConfig(
    legal_name="Mining Match Pty Ltd", abn="51 824 753 556", address="PO Box 1, Emerald QLD 4720",
    email="accounts@miningmatch.example", bank_account_name="Mining Match Pty Ltd", bank_bsb="123-456",
    bank_account_number="12345678")
TERMS = {"fee_percent": 12, "fee_basis": "Base salary excluding allowances", "window_months": 12,
         "guarantee_days": 90, "terms_text": "Placement fee terms (draft for testing)"}


def setUpModule():
    # Cheaper password hashing so the tests run quickly (hashes record their own settings).
    global _patch
    _patch = mock.patch.object(auth, "SCRYPT_N", 2 ** 10)
    _patch.start()


def tearDownModule():
    _patch.stop()


def docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
    return buf.getvalue()


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = str(self.dir / "mm.db")
        self.outbox = auth.Outbox()
        self.app = api.create_app(
            self.db, storage=LocalStorage(self.dir / "files"), mailer=self.outbox,
            base_url="https://miningmatch.example", scan_upload=lambda data: b"EICAR" not in data,
            secure_cookies=False,  # the test client talks plain http
            billing_config=BILLING,
        )
        self.client = TestClient(self.app)
        self.totp_secrets, self.totp_now = {}, time.time()
        conn = dl.connect(self.db)  # first admin, as set up from a terminal, with two-factor already on
        admin_id = ac.create_admin(conn, "admin@example.com", auth.hash_password(PASSWORD))
        self.totp_secrets["admin@example.com"] = two_factor.new_secret()
        two_factor.enable(conn, admin_id, self.totp_secrets["admin@example.com"], 0)
        conn.close()
        self.admin = self.login("admin@example.com")

    def tearDown(self):
        self.client.close()
        shutil.rmtree(self.dir)

    # ---- helpers
    def token_from_email(self, to: str, purpose: str = "verify-email") -> str:
        for msg in reversed(self.outbox.messages):
            m = re.search(rf"/{purpose}\?token=(\S+)", msg["body"])
            if msg["to"] == to and m:
                return m.group(1)
        self.fail(f"no {purpose} email to {to}")

    def login(self, email, password=PASSWORD):
        r = self.client.post("/auth/login", json={"email": email, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        if "two_factor" in r.json():  # admins: the second step
            r = self.two_factor_step(email, r.json())
            self.assertEqual(r.status_code, 200, r.text)
        return {"Authorization": f"Bearer {r.json()['token']}"}

    def totp(self, email):
        """The next code from this admin's 'phone' (each call is the next 30-second code)."""
        self.totp_now += two_factor.STEP
        return two_factor.code_at(self.totp_secrets[email], int(self.totp_now // two_factor.STEP))

    def two_factor_step(self, email, started, code=None):
        """Answer a login challenge: set up the app if needed, then send a code."""
        if started["two_factor"] == "setup":
            info = self.client.post("/auth/two-factor/setup", json={"challenge": started["challenge"]}).json()
            self.totp_secrets[email] = info["secret"].replace(" ", "")
            path = "/auth/two-factor/setup/confirm"
        else:
            path = "/auth/login/two-factor"
        code = code or self.totp(email)
        with mock.patch.object(two_factor, "clock", lambda: self.totp_now):
            return self.client.post(path, json={"challenge": started["challenge"], "code": code})

    def signup(self, role, email, **profile):
        r = self.client.post(f"/auth/register/{role}", json={"email": email, "password": PASSWORD, **profile})
        self.assertEqual(r.status_code, 202, r.text)
        r = self.client.post("/auth/verify-email", json={"token": self.token_from_email(email)})
        self.assertEqual(r.status_code, 200, r.text)
        headers = self.login(email)
        return headers, self.client.get("/me", headers=headers).json()

    def candidate(self, email="jordan@example.com", name="Jordan Private"):
        h, me = self.signup("candidate", email, name=name, location="QLD", phone="0400 111 222")
        cid = me["candidate_id"]
        for path, body in [
            ("licences", {"name": "Standard 11", "expiry": "2031-05-01"}),
            ("qualifications", {"name": "Cert III Surface Extraction"}),
            ("skills", {"name": "Dragline operation", "years": 6}),
        ]:
            self.assertEqual(self.client.put(f"/candidates/{cid}/{path}", json=body, headers=h).status_code, 200)
        r = self.client.post(f"/candidates/{cid}/experience", headers=h,
                             json={"role": "Dragline Operator", "start_date": "2018-01-01", "end_date": "2024-01-01"})
        self.assertEqual(r.status_code, 201, r.text)
        return h, cid

    def employer(self, email="hr@alpha.example", company="Alpha Mining"):
        h, me = self.signup("employer", email, company_name=company)
        return h, me["employer_id"]

    def upload(self, h, cid, data=PDF, filename="licence.pdf", content_type="application/pdf", kind="licence"):
        return self.client.post(f"/candidates/{cid}/documents", headers=h, data={"kind": kind},
                                files={"file": (filename, data, content_type)})

    def publish_terms(self, **changes):
        r = self.client.post("/fee-terms", json={**TERMS, **changes}, headers=self.admin)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["version"]


class HiringFlow(ApiTest):
    def test_end_to_end_with_two_step_unlock(self):
        ch, cid = self.candidate()
        doc = self.upload(ch, cid).json()
        eh, eid = self.employer()
        job_id = self.client.post("/jobs", json={**JOB, "status": "open"}, headers=eh).json()["id"]
        version = self.publish_terms()

        ranked = self.client.get(f"/jobs/{job_id}/ranked-candidates", headers=eh).json()
        (row,) = [r for r in ranked if r["candidate_id"] == cid]
        self.assertTrue(row["eligible"])
        self.assertEqual(row["contact_status"], "none")
        self.assertNotIn("Jordan", json.dumps(ranked))

        self.assertEqual(self.client.post(f"/candidates/{cid}/contact-request", json={"job_id": job_id},
                                          headers=eh).json(), {"status": "requested"})
        (req,) = self.client.get("/contact-requests", headers=ch).json()
        self.assertEqual((req["company_name"], req["job_title"]), ("Alpha Mining", "Dragline Operator"))
        self.client.put(f"/contact-releases/{eid}", json={"grant": True}, headers=ch)

        view = self.client.get(f"/candidates/{cid}", headers=eh).json()
        self.assertEqual(view["contact_status"], "approved")
        self.assertNotIn("Jordan", json.dumps(view))  # one key isn't enough

        r = self.client.post(f"/candidates/{cid}/unlock", json={"terms_version": version}, headers=eh)
        self.assertEqual(r.status_code, 403)  # terms not accepted yet
        terms = self.client.get("/fee-terms/current", headers=eh).json()
        self.assertEqual((terms["fee_percent"], terms["accepted"]), (12, False))
        self.client.post(f"/fee-terms/{terms['version']}/accept", headers=eh)
        r = self.client.post(f"/candidates/{cid}/unlock", json={"terms_version": version, "job_id": job_id}, headers=eh)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["new_introduction"])

        view = self.client.get(f"/candidates/{cid}", headers=eh).json()
        self.assertEqual((view["name"], view["email"], view["phone"]),
                         ("Jordan Private", "jordan@example.com", "0400 111 222"))
        f = self.client.get(f"/documents/{doc['id']}/file", headers=eh)
        self.assertEqual(f.content, PDF)
        self.assertEqual(f.headers["content-disposition"], 'attachment; filename="licence.pdf"')
        (intro,) = self.client.get("/introductions", headers=eh).json()
        self.assertEqual((intro["candidate_id"], intro["terms_version"], intro["job_id"]), (cid, version, job_id))

        matches = self.client.get(f"/candidates/{cid}/matching-jobs", headers=ch).json()
        self.assertEqual([(m["job_title"], m["company_name"]) for m in matches], [("Dragline Operator", "Alpha Mining")])

    def test_changed_terms_give_409_until_accepted(self):
        ch, cid = self.candidate()
        eh, eid = self.employer()
        v1 = self.publish_terms()
        self.client.post(f"/fee-terms/{v1}/accept", headers=eh)
        self.client.put(f"/contact-releases/{eid}", json={"grant": True}, headers=ch)
        v2 = self.publish_terms(fee_percent=10)
        r = self.client.post(f"/candidates/{cid}/unlock", json={"terms_version": v1}, headers=eh)
        self.assertEqual(r.status_code, 409)
        self.assertIn("version 2", r.json()["detail"])
        self.assertEqual(self.client.post(f"/fee-terms/{v1}/accept", headers=eh).status_code, 409)
        self.client.post(f"/fee-terms/{v2}/accept", headers=eh)
        self.assertEqual(self.client.post(f"/candidates/{cid}/unlock", json={"terms_version": v2},
                                          headers=eh).status_code, 200)


class SignUpAndLogin(ApiTest):
    def test_signup_and_reset_never_reveal_who_has_an_account(self):
        body = {"email": "jordan@example.com", "password": PASSWORD, "name": "Jordan"}
        first = self.client.post("/auth/register/candidate", json=body)
        again = self.client.post("/auth/register/candidate", json={**body, "name": "Someone else"})
        self.assertEqual((first.status_code, first.json()), (again.status_code, again.json()))
        self.assertIn("already have", self.outbox.messages[-1]["subject"])
        known = self.client.post("/auth/password-reset/request", json={"email": "jordan@example.com"})
        sent = len(self.outbox.messages)
        unknown = self.client.post("/auth/password-reset/request", json={"email": "nobody@example.com"})
        self.assertEqual((known.status_code, known.json()), (unknown.status_code, unknown.json()))
        self.assertEqual(len(self.outbox.messages), sent)  # nothing sent to an unknown address
        wrong = self.client.post("/auth/login", json={"email": "jordan@example.com", "password": "x" * 12})
        missing = self.client.post("/auth/login", json={"email": "nobody@example.com", "password": "x" * 12})
        self.assertEqual((wrong.status_code, wrong.json()), (missing.status_code, missing.json()))

    def test_unconfirmed_accounts_cannot_log_in_and_stay_hidden(self):
        eh, _ = self.employer()
        self.client.post("/auth/register/candidate",
                         json={"email": "new@example.com", "password": PASSWORD, "name": "New"})
        r = self.client.post("/auth/login", json={"email": "new@example.com", "password": PASSWORD})
        self.assertEqual(r.status_code, 403)
        found = self.client.post("/search/candidates", json={"criteria": {}}, headers=eh).json()
        self.assertEqual(found, [])
        self.client.post("/auth/verify-email", json={"token": self.token_from_email("new@example.com")})
        self.login("new@example.com")
        self.assertEqual(len(self.client.post("/search/candidates", json={"criteria": {}}, headers=eh).json()), 1)

    def test_weak_passwords_rejected(self):
        for pw in ("short", "password123", "aaaaaaaaaaaa", "jordan-is-great"):
            r = self.client.post("/auth/register/candidate",
                                 json={"email": "jordan@example.com", "password": pw, "name": "J"})
            self.assertEqual(r.status_code, 400, pw)

    def test_failed_logins_from_one_address_make_that_address_wait(self):
        self.candidate()
        now = [2_000_000_000]
        with mock.patch.object(auth, "clock", lambda: now[0]):
            for _ in range(5):
                r = self.client.post("/auth/login", json={"email": "jordan@example.com", "password": "wrong-pass-123"})
                self.assertEqual(r.status_code, 401)
            r = self.client.post("/auth/login", json={"email": "jordan@example.com", "password": PASSWORD})
            self.assertEqual(r.status_code, 429)  # even the right password waits, from this address
            self.assertGreater(int(r.headers["retry-after"]), 0)
            now[0] += 16 * 60
            self.login("jordan@example.com")

    def test_someone_elses_wrong_guesses_dont_lock_the_owner_out(self):
        self.candidate()
        attacker = TestClient(self.app, client=("203.0.113.9", 40000))
        owner = TestClient(self.app, client=("198.51.100.7", 40000))
        for _ in range(10):
            attacker.post("/auth/login", json={"email": "jordan@example.com", "password": "wrong-pass-123"})
        r = attacker.post("/auth/login", json={"email": "jordan@example.com", "password": "wrong-pass-123"})
        self.assertEqual(r.status_code, 429)  # the guesser is slowed down
        r = owner.post("/auth/login", json={"email": "jordan@example.com", "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)  # the owner, elsewhere, still gets in
        attacker.close()
        owner.close()

    def test_guessing_one_account_from_many_addresses_is_still_limited(self):
        self.candidate()
        limit, _ = auth.LOGIN_FAILS_PER_EMAIL
        now = [2_000_000_000]
        with mock.patch.object(auth, "clock", lambda: now[0]):
            conn = dl.connect(self.db)
            try:
                for i in range(limit):
                    with self.assertRaises(auth.InvalidCredentials):
                        auth.login(conn, "jordan@example.com", "wrong-pass-123", f"10.0.{i // 250}.{i % 250}")
                with self.assertRaises(auth.TooManyAttempts):
                    auth.login(conn, "jordan@example.com", PASSWORD, "192.0.2.50")
                now[0] += 61 * 60
                self.assertEqual(auth.login(conn, "jordan@example.com", PASSWORD, "192.0.2.50")["role"], "candidate")
            finally:
                conn.close()

    def test_signup_takes_the_same_work_whether_or_not_the_email_is_taken(self):
        self.candidate()
        body = {"password": PASSWORD, "name": "Someone"}
        with mock.patch.object(auth, "hash_password", wraps=auth.hash_password) as hashed:
            self.client.post("/auth/register/candidate", json={"email": "jordan@example.com", **body})
            taken = hashed.call_count
            hashed.reset_mock()
            self.client.post("/auth/register/candidate", json={"email": "fresh@example.com", **body})
            new = hashed.call_count
        self.assertEqual((taken, new), (1, 1))  # the slow password hash runs either way

    def test_sessions_end_on_logout_idle_and_password_reset(self):
        self.candidate()
        now = [2_000_000_000]
        with mock.patch.object(auth, "clock", lambda: now[0]):
            a = self.login("jordan@example.com")
            self.assertEqual(self.client.post("/auth/logout", headers=a).status_code, 204)
            self.assertEqual(self.client.get("/me", headers=a).status_code, 401)

            b = self.login("jordan@example.com")
            now[0] += auth.SESSION_IDLE_SECONDS + 1
            self.assertEqual(self.client.get("/me", headers=b).status_code, 401)

            c = self.login("jordan@example.com")
            self.client.post("/auth/password-reset/request", json={"email": "jordan@example.com"})
            token = self.token_from_email("jordan@example.com", "reset-password")
            self.assertEqual(self.client.post("/auth/password-reset/confirm",
                                              json={"token": token, "new_password": "short"}).status_code, 400)
            r = self.client.post("/auth/password-reset/confirm", json={"token": token, "new_password": "New-Pass-2027!"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self.client.get("/me", headers=c).status_code, 401)  # all sessions ended
            again = self.client.post("/auth/password-reset/confirm", json={"token": token, "new_password": "Other-Pass-2028!"})
            self.assertEqual(again.status_code, 400)  # links work once
            self.login("jordan@example.com", "New-Pass-2027!")

            self.client.post("/auth/password-reset/request", json={"email": "jordan@example.com"})
            late = self.token_from_email("jordan@example.com", "reset-password")
            now[0] += auth.RESET_TOKEN_SECONDS + 1
            self.assertEqual(self.client.post("/auth/password-reset/confirm",
                                              json={"token": late, "new_password": "Late-Pass-2029!"}).status_code, 400)

    def test_change_password_logs_out_other_devices(self):
        h, _ = self.candidate()
        other = self.login("jordan@example.com")
        r = self.client.post("/auth/change-password", headers=h,
                             json={"current_password": "wrong-pass-123", "new_password": "Brand-New-2027"})
        self.assertEqual(r.status_code, 401)
        r = self.client.post("/auth/change-password", headers=h,
                             json={"current_password": PASSWORD, "new_password": "Brand-New-2027"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/me", headers=h).status_code, 200)
        self.assertEqual(self.client.get("/me", headers=other).status_code, 401)

    def test_suspended_users_are_logged_out_and_told_on_login(self):
        h, _ = self.candidate()
        user_id = self.client.get("/me", headers=h).json()["user_id"]
        self.client.put(f"/admin/users/{user_id}/active", json={"active": False, "reason": "spam"}, headers=self.admin)
        self.assertEqual(self.client.get("/me", headers=h).status_code, 401)
        r = self.client.post("/auth/login", json={"email": "jordan@example.com", "password": PASSWORD})
        self.assertEqual((r.status_code, r.json()["detail"]), (403, "this account is suspended, contact support"))

    def test_admin_invites_admin(self):
        ch, _ = self.candidate()
        self.assertEqual(self.client.post("/admin/admins", json={"email": "x@example.com"}, headers=ch).status_code, 403)
        r = self.client.post("/admin/admins", json={"email": "Second@Example.com"}, headers=self.admin)
        self.assertEqual(r.status_code, 201)
        token = self.token_from_email("second@example.com", "reset-password")
        self.client.post("/auth/password-reset/confirm", json={"token": token, "new_password": "Admin-Two-2027"})
        h = self.login("second@example.com", "Admin-Two-2027")
        self.assertEqual(self.client.get("/me", headers=h).json()["role"], "admin")


class Uploads(ApiTest):
    def test_file_type_comes_from_content(self):
        h, cid = self.candidate()
        cases = [
            (b"<html><script>alert(1)</script></html>", "cv.pdf", "application/pdf", 400),
            (b"MZ\x90\x00 an executable", "cv.pdf", "application/pdf", 400),
            (PDF + b"EICAR", "cv.pdf", "application/pdf", 400),  # virus scan says no
            (PNG, "photo.pdf", "application/pdf", 201),           # really a PNG: stored as PNG
            (docx_bytes(), "cv.docx", "application/octet-stream", 201),
        ]
        for data, name, ctype, status in cases:
            with self.subTest(name=name, start=data[:8]):
                r = self.upload(h, cid, data, name, ctype, kind="resume")
                self.assertEqual(r.status_code, status, r.text)
        types = [d["mime_type"] for d in self.client.get(f"/candidates/{cid}/documents", headers=h).json()]
        self.assertEqual(sorted(types), sorted(["image/png", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]))

    def test_size_limit_and_path_tricks(self):
        h, cid = self.candidate()
        with mock.patch.object(ac, "MAX_UPLOAD_BYTES", 100):
            self.assertEqual(self.upload(h, cid, PDF + b"x" * 200).status_code, 413)
        doc = self.upload(h, cid, PDF, "../../etc/passwd.pdf").json()
        (listed,) = self.client.get(f"/candidates/{cid}/documents", headers=h).json()
        self.assertEqual(listed["filename"], "passwd.pdf")
        stored = list((self.dir / "files").rglob("*"))
        self.assertTrue(all(str(p).startswith(str(self.dir / "files")) for p in stored))
        self.assertNotIn("storage_key", json.dumps(listed) + json.dumps(doc))

    def test_only_the_owner_uploads_and_deleting_removes_files(self):
        h, cid = self.candidate()
        eh, _ = self.employer()
        self.assertEqual(self.upload(eh, cid).status_code, 403)
        doc = self.upload(h, cid).json()
        files = lambda: [p for p in (self.dir / "files").rglob("*") if p.is_file()]
        self.assertEqual(len(files()), 1)
        self.assertEqual(self.client.delete(f"/documents/{doc['id']}", headers=h).status_code, 204)
        self.assertEqual(files(), [])
        self.upload(h, cid)
        self.assertEqual(self.client.delete(f"/candidates/{cid}", headers=h).status_code, 204)
        self.assertEqual(files(), [])
        self.assertEqual(self.client.post("/auth/login", json={"email": "jordan@example.com",
                                                               "password": PASSWORD}).status_code, 401)


class Errors(ApiTest):
    def test_status_codes(self):
        h, cid = self.candidate()
        h2, cid2 = self.candidate("sam@example.com", "Sam Other")
        self.assertEqual(self.client.get(f"/candidates/{cid}").status_code, 401)
        self.assertEqual(self.client.get(f"/candidates/{cid}", headers={"Authorization": "Bearer nonsense"}).status_code, 401)
        self.assertEqual(self.client.get(f"/candidates/{cid2}", headers=h).status_code, 403)
        self.assertEqual(self.client.patch(f"/candidates/{cid2}", json={"location": "WA"}, headers=h).status_code, 403)
        self.assertEqual(self.client.get("/jobs/no-such-job", headers=h).status_code, 404)
        self.assertEqual(self.client.put(f"/candidates/{cid}/licences", json={"name": "S11", "expiry": "31/12/2030"},
                                         headers=h).status_code, 400)
        self.assertEqual(self.client.patch(f"/candidates/{cid}", json={"is_active": None}, headers=h).status_code, 400)
        self.assertEqual(self.client.patch(f"/candidates/{cid}", json={"user_id": "x"}, headers=h).status_code, 422)

    def test_validation_errors_never_echo_passwords(self):
        secret = "Sup3r-Secret-" + "z" * 300
        r = self.client.post("/auth/register/candidate", json={"email": "a@example.com", "password": secret, "name": "A"})
        self.assertEqual(r.status_code, 422)
        self.assertNotIn("Sup3r-Secret", r.text)

    def test_security_headers(self):
        r = self.client.get("/health")
        for header, value in [("x-content-type-options", "nosniff"), ("cache-control", "no-store"),
                              ("x-frame-options", "DENY"), ("referrer-policy", "no-referrer")]:
            self.assertEqual(r.headers[header], value)
        self.assertIn("default-src 'none'", r.headers["content-security-policy"])
        self.assertEqual(self.client.get("/docs").status_code, 200)


class Billing(ApiTest):
    def unlocked(self):
        ch, cid = self.candidate()
        eh, eid = self.employer()
        version = self.publish_terms()
        self.client.put(f"/contact-releases/{eid}", json={"grant": True}, headers=ch)
        self.client.post(f"/fee-terms/{version}/accept", headers=eh)
        self.client.post(f"/candidates/{cid}/unlock", json={"terms_version": version}, headers=eh)
        return ch, cid, eh, eid

    def test_hire_invoice_and_payment(self):
        ch, cid, eh, eid = self.unlocked()
        start = BILLING.today().isoformat()
        body = {"candidate_id": cid, "start_date": start, "base_salary": 120000}
        preview = self.client.post("/hires/preview", json=body, headers=eh).json()
        self.assertEqual((preview["fee_cents"], preview["total_cents"]), (1_440_000, 1_584_000))
        self.assertEqual(self.client.post("/hires/preview", json=body, headers=ch).status_code, 403)
        r = self.client.post("/hires", json={**body, "base_salary": "120,000"}, headers=eh)
        self.assertEqual(r.status_code, 201, r.text)
        invoice = r.json()["invoice"]
        self.assertEqual((invoice["number"], invoice["status"]), ("MM-00001", "issued"))
        msg = self.outbox.messages[-1]
        self.assertEqual((msg["to"], msg["attachments"][0][0]), ("hr@alpha.example", "MM-00001.pdf"))
        self.assertEqual(self.client.post("/hires", json=body, headers=eh).status_code, 409)  # already recorded

        (listed,) = self.client.get("/invoices", headers=eh).json()
        self.assertEqual((listed["number"], listed["display_status"]), ("MM-00001", "due"))
        pdf = self.client.get(f"/invoices/{invoice['id']}/pdf", headers=eh)
        self.assertEqual(pdf.headers["content-type"], "application/pdf")
        self.assertEqual(pdf.headers["content-disposition"], 'attachment; filename="MM-00001.pdf"')
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        other, _ = self.employer("hr@beta.example", "Beta Resources")
        self.assertEqual(self.client.get(f"/invoices/{invoice['id']}", headers=other).status_code, 403)
        self.assertEqual(self.client.get("/invoices", headers=other).json(), [])

        paid = {"paid_on": start, "note": "Bank transfer"}
        self.assertEqual(self.client.post(f"/invoices/{invoice['id']}/paid", json=paid, headers=eh).status_code, 403)
        r = self.client.post(f"/invoices/{invoice['id']}/paid", json=paid, headers=self.admin)
        self.assertEqual((r.status_code, r.json()["status"]), (200, "paid"))
        (hire,) = self.client.get("/hires", headers=eh).json()
        self.assertEqual((hire["invoice_number"], hire["invoice_status"]), ("MM-00001", "paid"))

    def test_billing_details_and_settings(self):
        eh, eid = self.employer()
        settings = self.client.get("/billing/settings", headers=eh).json()
        self.assertEqual((settings["enabled"], settings["payment"]["bsb"]), (True, "123-456"))
        bad = self.client.put("/billing/details", json={"abn": "12 345 678 901"}, headers=eh)
        self.assertEqual(bad.status_code, 400)
        good = self.client.put("/billing/details", json={"abn": "51824753556", "billing_email": "ap@alpha.example"},
                               headers=eh).json()
        self.assertEqual((good["abn"], good["billing_email"]), ("51 824 753 556", "ap@alpha.example"))

    def test_services_invoice_by_admin(self):
        eh, eid = self.employer()
        body = {"employer_id": eid, "lines": [{"description": "Search subscription, December 2026", "amount": 199}]}
        self.assertEqual(self.client.post("/invoices", json=body, headers=eh).status_code, 403)
        r = self.client.post("/invoices", json=body, headers=self.admin)
        self.assertEqual((r.status_code, r.json()["total_cents"]), (201, 21_890))
        self.assertEqual(self.client.post("/invoices", json={**body, "lines": []}, headers=self.admin).status_code, 422)

    def test_invoicing_not_set_up(self):
        d = Path(tempfile.mkdtemp())
        try:
            app = api.create_app(str(d / "n.db"), storage=LocalStorage(d / "f"), mailer=auth.Outbox(),
                                 base_url="https://x.example", secure_cookies=False)
            conn = dl.connect(str(d / "n.db"))
            secret = two_factor.new_secret()
            two_factor.enable(conn, ac.create_admin(conn, "a@example.com", auth.hash_password(PASSWORD)), secret, 0)
            conn.close()
            with TestClient(app) as c:
                challenge = c.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD}).json()["challenge"]
                code = two_factor.code_at(secret, two_factor.current_step())
                token = c.post("/auth/login/two-factor", json={"challenge": challenge, "code": code}).json()["token"]
                h = {"Authorization": f"Bearer {token}"}
                self.assertEqual(c.get("/billing/settings", headers=h).json(), {"enabled": False})
                r = c.post("/invoices/issue-due", headers=h)
                self.assertEqual(r.status_code, 409)
                self.assertIn("isn't set up", r.json()["detail"])
        finally:
            shutil.rmtree(d)


class Website(ApiTest):
    WEB = {"X-Requested-With": "mining-match"}

    def test_cookie_login_keeps_the_token_out_of_reach_of_page_scripts(self):
        self.candidate()
        r = self.client.post("/auth/login", json={"email": "jordan@example.com", "password": PASSWORD,
                                                  "use_cookie": True})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("token", r.json())
        cookie = r.headers["set-cookie"].lower()
        for part in ("mm_session=", "httponly", "samesite=strict", "path=/"):
            self.assertIn(part, cookie)
        self.assertEqual(self.client.get("/me", headers=self.WEB).json()["role"], "candidate")
        # Without the header (what a form or image on another site would send) the cookie doesn't count.
        self.assertEqual(self.client.get("/me").status_code, 401)
        self.assertEqual(self.client.get("/me", headers={"X-Requested-With": "XMLHttpRequest"}).status_code, 401)
        self.assertEqual(self.client.post("/auth/logout", headers=self.WEB).status_code, 204)
        self.assertEqual(self.client.get("/me", headers=self.WEB).status_code, 401)

    def test_secure_cookie_flag_in_production(self):
        d = Path(tempfile.mkdtemp())
        try:
            app = api.create_app(str(d / "p.db"), storage=LocalStorage(d / "f"), mailer=auth.Outbox(),
                                 base_url="https://x.example")
            conn = dl.connect(str(d / "p.db"))
            secret = two_factor.new_secret()
            two_factor.enable(conn, ac.create_admin(conn, "a@example.com", auth.hash_password(PASSWORD)), secret, 0)
            conn.close()
            with TestClient(app, base_url="https://testserver") as c:
                challenge = c.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD}).json()["challenge"]
                code = two_factor.code_at(secret, two_factor.current_step())
                r = c.post("/auth/login/two-factor", json={"challenge": challenge, "code": code, "use_cookie": True})
                self.assertIn("secure", r.headers["set-cookie"].lower())
        finally:
            shutil.rmtree(d)

    def test_website_served_with_strict_content_policy(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        csp = r.headers["content-security-policy"]
        for rule in ("default-src 'self'", "script-src 'self'", "frame-ancestors 'none'", "object-src 'none'"):
            self.assertIn(rule, csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertEqual(self.client.get("/assets/app.css").status_code, 200)
        self.assertEqual(self.client.get("/assets/../python/api.py").status_code, 404)
        self.assertEqual(self.client.get("/me").headers["content-security-policy"],
                         "default-src 'none'; frame-ancestors 'none'")

    def test_email_links_open_app_screens(self):
        self.client.post("/auth/register/candidate", json={"email": "n@example.com", "password": PASSWORD, "name": "N"})
        self.assertIn("https://miningmatch.example/#/verify-email?token=", self.outbox.messages[-1]["body"])

    def test_catalog_suggestions(self):
        h, _ = self.candidate()
        names = self.client.get("/catalog/licence", headers=h).json()
        self.assertIn("Standard 11", names)
        self.assertIn("Coal Board Medical", names)
        self.assertEqual(self.client.get("/catalog/nonsense", headers=h).status_code, 400)
        self.assertEqual(self.client.get("/catalog/licence").status_code, 401)


class ExpiryReminders(ApiTest):
    def soon(self, days):
        return (reminders.local_today() + timedelta(days=days)).isoformat()

    def test_reminders_run_and_the_unsubscribe_link_works(self):
        ch, cid = self.candidate()
        expiry = self.soon(30)
        self.client.put(f"/candidates/{cid}/licences", json={"name": "Coal Board Medical", "expiry": expiry}, headers=ch)

        self.assertEqual(self.client.post("/admin/reminders/run", headers=ch).status_code, 403)
        result = self.client.post("/admin/reminders/run", headers=self.admin).json()
        self.assertEqual(result, {"emails": 1, "licences": 1, "failed": 0})
        msg = self.outbox.messages[-1]
        self.assertEqual(msg["to"], "jordan@example.com")
        self.assertEqual(msg["subject"], "Your Coal Board Medical expires in 30 days")

        (row,) = self.client.get("/admin/reminders", headers=self.admin).json()
        self.assertEqual((row["candidate_name"], row["licence"], row["stage"]), ("Jordan Private", "Coal Board Medical", 30))
        self.assertEqual(self.client.get("/admin/reminders", headers=ch).status_code, 403)
        self.assertEqual(self.client.get("/admin/reminders?limit=0", headers=self.admin).status_code, 422)

        token = re.search(r"/unsubscribe\?token=(\S+)", msg["body"]).group(1)
        self.assertIn("https://miningmatch.example/#/unsubscribe?token=", msg["body"])
        r = self.client.post("/reminders/unsubscribe", json={"token": token})  # no login needed
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("reminders", r.json()["detail"])
        self.assertFalse(self.client.get(f"/candidates/{cid}", headers=ch).json()["expiry_reminders"])
        self.assertEqual(self.client.post("/reminders/unsubscribe", json={"token": "x" * 30}).status_code, 400)

    def test_candidates_turn_reminders_off_and_on_in_their_profile(self):
        ch, cid = self.candidate()
        self.assertTrue(self.client.get(f"/candidates/{cid}", headers=ch).json()["expiry_reminders"])
        r = self.client.patch(f"/candidates/{cid}", json={"expiry_reminders": False}, headers=ch)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.client.get(f"/candidates/{cid}", headers=ch).json()["expiry_reminders"])
        self.client.put(f"/candidates/{cid}/licences", json={"name": "Coal Board Medical", "expiry": self.soon(30)}, headers=ch)
        self.assertEqual(self.client.post("/admin/reminders/run", headers=self.admin).json()["emails"], 0)
        self.client.patch(f"/candidates/{cid}", json={"expiry_reminders": True}, headers=ch)
        self.assertEqual(self.client.post("/admin/reminders/run", headers=self.admin).json()["emails"], 1)
        self.assertEqual(self.client.patch(f"/candidates/{cid}", json={"expiry_reminders": None}, headers=ch).status_code, 400)

    def test_licence_rules_for_suggesting_expiry_dates(self):
        ch, _ = self.candidate()
        rules = {r["name"]: r for r in self.client.get("/catalog/licence-rules", headers=ch).json()}
        self.assertEqual(rules["Dogging (DG)"]["renew_years"], 5)
        self.assertIsNone(rules["White Card"]["renew_years"])
        self.assertIn("Doesn’t expire".replace("’", "'"), rules["White Card"]["note"])
        self.assertEqual(self.client.get("/catalog/licence-rules").status_code, 401)


class SearchLimits(ApiTest):
    def test_employer_searches_are_limited_per_hour_and_per_day(self):
        self.candidate()
        self.employer()
        hourly, _ = auth.EMPLOYER_SEARCHES_PER_HOUR
        daily, _ = auth.EMPLOYER_SEARCHES_PER_DAY
        now = [int(time.time())]
        with mock.patch.object(auth, "clock", lambda: now[0]):
            eh = self.login("hr@alpha.example")
            search = lambda: self.client.post("/search/candidates", json={"criteria": {}}, headers=eh)
            for _ in range(hourly):
                self.assertEqual(search().status_code, 200)
            r = search()
            self.assertEqual(r.status_code, 429)
            self.assertGreater(int(r.headers["retry-after"]), 0)
            done = hourly
            while done < daily:  # spread over the day, the daily cap still applies
                now[0] += 3601
                for _ in range(min(hourly, daily - done)):
                    self.assertEqual(search().status_code, 200)
                done += min(hourly, daily - done)
            now[0] += 3601
            self.assertEqual(search().status_code, 429)
            now[0] += 24 * 3600
            self.assertEqual(search().status_code, 200)

    def test_job_rankings_count_too_and_other_employers_are_unaffected(self):
        self.candidate()
        eh, _ = self.employer()
        other, _ = self.employer("hr@beta.example", "Beta Mining")
        r = self.client.post("/jobs", headers=eh, json={**JOB, "status": "open"})
        self.assertEqual(r.status_code, 201, r.text)
        job_id = r.json()["id"]
        hourly, _ = auth.EMPLOYER_SEARCHES_PER_HOUR
        with mock.patch.object(auth, "EMPLOYER_SEARCHES_PER_HOUR", (3, 3600)):
            for _ in range(3):
                self.assertEqual(self.client.get(f"/jobs/{job_id}/ranked-candidates", headers=eh).status_code, 200)
            self.assertEqual(self.client.post("/search/candidates", json={"criteria": {}}, headers=eh).status_code, 429)
            self.assertEqual(self.client.post("/search/candidates", json={"criteria": {}}, headers=other).status_code, 200)
            for _ in range(5):  # admins aren't limited
                self.assertEqual(self.client.post("/search/candidates", json={"criteria": {}},
                                                  headers=self.admin).status_code, 200)


class HttpsOnly(ApiTest):
    def test_https_responses_tell_browsers_to_stay_on_https(self):
        secure = TestClient(self.app, base_url="https://testserver")
        try:
            for path in ("/", "/health"):
                r = secure.get(path)
                self.assertEqual(r.headers.get("strict-transport-security"), "max-age=31536000", path)
        finally:
            secure.close()

    def test_plain_http_responses_dont_send_it(self):
        # Browsers ignore it over http anyway; local development stays on http.
        self.assertNotIn("strict-transport-security", self.client.get("/health").headers)


class PasswordHashing(unittest.TestCase):
    def test_hash_round_trip_and_tampering(self):
        stored = auth.hash_password("Correct-Horse-9")
        self.assertTrue(auth.verify_password("Correct-Horse-9", stored))
        self.assertFalse(auth.verify_password("correct-horse-9", stored))
        self.assertNotEqual(stored, auth.hash_password("Correct-Horse-9"))  # random salt
        for bad in (None, "", "plain", stored.replace("scrypt", "md5"), "scrypt$1048577$8$1$AA==$AA==",
                    stored[:-4] + "!!!!"):
            self.assertFalse(auth.verify_password("Correct-Horse-9", bad), bad)

    def test_sniffing(self):
        self.assertEqual(sniff_type(PDF), "application/pdf")
        self.assertIsNone(sniff_type(b"PK\x03\x04 not really a zip"))
        self.assertIsNone(sniff_type(b"GIF89a"))


if __name__ == "__main__":
    unittest.main()
