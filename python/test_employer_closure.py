"""Employer account closure (launch blocker B4)."""
import json
import sqlite3
import unittest

import access_control as ac
import data_layer as dl
from test_api import BILLING, JOB, PASSWORD, ApiTest


class EmployerClosure(ApiTest):
    WEB = {"X-Requested-With": "mining-match"}  # the website: session in a cookie

    def busy_employer(self, paid=True):
        """An employer with a job, a shortlist note, a pending request, an unlocked hire and its invoice."""
        ch, cid = self.candidate()
        eh, me = self.signup("employer", "hr@alpha.example", company_name="Alpha Mining", contact_name="Pat Manager",
                             phone="07 4900 1234")
        eid = me["employer_id"]
        self.assertEqual(self.row("SELECT phone FROM employers WHERE id = ?", eid)["phone"], "07 4900 1234")
        self.client.put("/billing/details", headers=eh,
                        json={"abn": "51824753556", "billing_email": "ap@alpha.example", "billing_address": "1 Pit Rd, Moranbah"})
        job_id = self.client.post("/jobs", json={**JOB, "status": "open"}, headers=eh).json()["id"]
        sid = self.client.post("/shortlists", json={"name": "Dragline crew", "job_id": job_id}, headers=eh).json()["id"]
        self.client.post(f"/shortlists/{sid}/entries", json={"candidate_id": cid, "notes": "Strong on night shift"},
                         headers=eh)
        version = self.publish_terms()
        self.client.put(f"/contact-releases/{eid}", json={"grant": True}, headers=ch)
        self.client.post(f"/fee-terms/{version}/accept", headers=eh)
        self.assertEqual(self.client.post(f"/candidates/{cid}/unlock", json={"terms_version": version}, headers=eh).status_code, 200)
        r = self.client.post("/hires", headers=eh, json={"candidate_id": cid, "start_date": BILLING.today().isoformat(),
                                                         "base_salary": 120000})
        self.assertEqual(r.status_code, 201, r.text)
        invoice = r.json()["invoice"]
        if paid:
            self.client.post(f"/invoices/{invoice['id']}/paid", json={"paid_on": BILLING.today().isoformat()},
                             headers=self.admin)
        # A second candidate with a request still waiting.
        sh, sid2 = self.candidate("sam@example.com", "Sam Other")
        self.client.post(f"/candidates/{sid2}/contact-request", json={"job_id": job_id}, headers=eh)
        return {"ch": ch, "cid": cid, "eh": eh, "eid": eid, "job_id": job_id, "invoice": invoice, "sh": sh}

    def row(self, sql, *params):
        conn = dl.connect(self.db)
        try:
            r = conn.execute(sql, params).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()

    def count(self, sql, *params):
        conn = dl.connect(self.db)
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()

    def test_an_employer_closes_their_account(self):
        s = self.busy_employer()
        r = self.client.post(f"/employers/{s['eid']}/close", json={"password": PASSWORD}, headers=s["eh"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"closed": True, "jobs_closed": 1, "shortlists_removed": 1, "contact_requests_removed": 2})

        # Logged out, and the old sign-in no longer works.
        self.assertEqual(self.client.get("/me", headers=s["eh"]).status_code, 401)
        r = self.client.post("/auth/login", json={"email": "hr@alpha.example", "password": PASSWORD})
        self.assertEqual(r.status_code, 401)

        # Personal details gone; business records kept.
        user = self.row("SELECT u.email, u.password_hash, u.is_active FROM users u JOIN employers e ON e.user_id = u.id "
                        "WHERE e.id = ?", s["eid"])
        self.assertTrue(user["email"].endswith("@closed.invalid"))
        self.assertIsNone(user["password_hash"])
        self.assertEqual(user["is_active"], 0)
        emp = self.row("SELECT company_name, contact_name, phone, abn, billing_email, billing_address FROM employers "
                       "WHERE id = ?", s["eid"])
        self.assertEqual((emp["company_name"], emp["abn"], emp["billing_address"]),
                         ("Alpha Mining", "51824753556", "1 Pit Rd, Moranbah"))
        self.assertEqual((emp["contact_name"], emp["phone"], emp["billing_email"]), (None, None, None))
        self.assertEqual(self.count("SELECT COUNT(*) FROM sessions s JOIN employers e ON e.user_id = s.user_id "
                                    "WHERE e.id = ?", s["eid"]), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM shortlists WHERE employer_id = ?", s["eid"]), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM shortlist_entries"), 0)  # notes on candidates gone
        self.assertEqual(self.count("SELECT COUNT(*) FROM contact_releases WHERE employer_id = ?", s["eid"]), 0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM introductions WHERE employer_id = ?", s["eid"]), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM placements WHERE employer_id = ?", s["eid"]), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM invoices WHERE employer_id = ? AND status = 'paid'", s["eid"]), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM terms_acceptances WHERE employer_id = ?", s["eid"]), 1)

        self.assertEqual(self.count("SELECT COUNT(*) FROM jobs WHERE employer_id = ? AND status != 'closed'", s["eid"]), 0)
        everything = json.dumps(self.row("SELECT * FROM employers WHERE id = ?", s["eid"]))
        for private in ("Pat Manager", "07 4900 1234", "ap@alpha.example", "hr@alpha.example"):
            self.assertNotIn(private, everything)

        # Candidates no longer see the job or the waiting request.
        self.assertEqual(self.client.get("/contact-requests", headers=s["sh"]).json(), [])
        jobs = self.client.get(f"/candidates/{s['cid']}/matching-jobs", headers=s["ch"]).json()
        self.assertEqual(jobs, [])

        # The admin's invoice is unchanged, and the log says what happened without personal details.
        inv = self.client.get(f"/invoices/{s['invoice']['id']}", headers=self.admin).json()
        self.assertEqual((inv["number"], inv["status"]), ("MM-00001", "paid"))
        log = self.client.get(f"/admin/audit-log?resource_id={s['eid']}", headers=self.admin).json()
        (entry,) = [a for a in log if a["action"] == "employer.close"]
        self.assertEqual((entry["outcome"], entry["detail"]), ("allowed", "closed by the employer"))
        self.assertNotIn("alpha.example", json.dumps(log))

    def test_the_same_email_can_sign_up_again(self):
        s = self.busy_employer()
        self.client.post(f"/employers/{s['eid']}/close", json={"password": PASSWORD}, headers=s["eh"])
        eh, eid = self.employer("hr@alpha.example", "Alpha Mining (new)")
        self.assertNotEqual(eid, s["eid"])
        self.assertEqual(self.client.get("/jobs", headers=eh).json(), [])

    def test_closing_needs_the_right_password(self):
        s = self.busy_employer()
        for body in ({"password": "wrong-pass-123"}, {}):
            r = self.client.post(f"/employers/{s['eid']}/close", json=body, headers=s["eh"])
            self.assertEqual(r.status_code, 401)
        self.assertEqual(self.client.get("/me", headers=s["eh"]).status_code, 200)  # nothing changed
        self.assertEqual(self.count("SELECT COUNT(*) FROM jobs WHERE status = 'open'"), 1)

    def test_nobody_else_can_close_it(self):
        s = self.busy_employer()
        other, _ = self.employer("hr@beta.example", "Beta Mining")
        for h in (other, s["ch"]):
            r = self.client.post(f"/employers/{s['eid']}/close", json={"password": PASSWORD}, headers=h)
            self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get("/me", headers=s["eh"]).status_code, 200)

    def test_unpaid_invoices_block_closing(self):
        s = self.busy_employer(paid=False)
        r = self.client.post(f"/employers/{s['eid']}/close", json={"password": PASSWORD}, headers=s["eh"])
        self.assertEqual(r.status_code, 409)
        self.assertIn("1 invoice is unpaid", r.json()["detail"])
        self.assertEqual(self.client.get("/me", headers=s["eh"]).status_code, 200)
        r = self.client.post(f"/employers/{s['eid']}/close", json={"reason": "on request"}, headers=self.admin)
        self.assertEqual(r.status_code, 409)  # admins too: settle the invoice first
        self.client.post(f"/invoices/{s['invoice']['id']}/paid", json={"paid_on": BILLING.today().isoformat()},
                         headers=self.admin)
        r = self.client.post(f"/employers/{s['eid']}/close", json={"password": PASSWORD}, headers=s["eh"])
        self.assertEqual(r.status_code, 200, r.text)

    def test_an_admin_closes_one_on_request(self):
        s = self.busy_employer()
        r = self.client.post(f"/employers/{s['eid']}/close", json={"reason": "asked by email"}, headers=self.admin)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("set-cookie", r.headers)  # the admin stays logged in
        self.assertEqual(self.client.get("/me", headers=self.admin).status_code, 200)
        self.assertEqual(self.client.get("/me", headers=s["eh"]).status_code, 401)
        (u,) = [u for u in self.client.get("/admin/users?role=employer", headers=self.admin).json()
                if u["employer_id"] == s["eid"]]
        self.assertTrue(u["closed"])
        log = self.client.get(f"/admin/audit-log?resource_id={s['eid']}", headers=self.admin).json()
        self.assertIn("closed by an admin: asked by email", [a["detail"] for a in log])
        # It can't be reactivated or closed twice.
        r = self.client.put(f"/admin/users/{u['user_id']}/active", json={"active": True}, headers=self.admin)
        self.assertEqual(r.status_code, 409)
        r = self.client.post(f"/employers/{s['eid']}/close", json={}, headers=self.admin)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.client.post("/employers/no-such/close", json={}, headers=self.admin).status_code, 404)

    def test_the_website_cookie_is_cleared(self):
        s = self.busy_employer()
        self.client.post("/auth/login", json={"email": "hr@alpha.example", "password": PASSWORD, "use_cookie": True})
        r = self.client.post(f"/employers/{s['eid']}/close", json={"password": PASSWORD}, headers=self.WEB)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('mm_session=""', r.headers["set-cookie"])
        self.assertEqual(self.client.get("/me", headers=self.WEB).status_code, 401)

    def test_closing_one_employer_leaves_another_employers_records_alone(self):
        # The deletions are scoped to the employer being closed. With only one employer in the fixture, an
        # unscoped DELETE would pass every other test here.
        s = self.busy_employer()
        eh2, me2 = self.signup("employer", "hr@beta.example", company_name="Beta Mining", contact_name="Lee Other",
                               phone="07 4900 9999")
        eid2 = me2["employer_id"]
        job2 = self.client.post("/jobs", json={**JOB, "status": "open"}, headers=eh2).json()["id"]
        sid = self.client.post("/shortlists", json={"name": "Beta crew", "job_id": job2}, headers=eh2).json()["id"]
        self.client.post(f"/shortlists/{sid}/entries", json={"candidate_id": s["cid"], "notes": "Beta's note"}, headers=eh2)
        self.client.post(f"/candidates/{s['cid']}/contact-request", json={"job_id": job2}, headers=eh2)

        def beta():
            return (self.count("SELECT COUNT(*) FROM shortlists WHERE employer_id = ?", eid2),
                    self.count("SELECT COUNT(*) FROM shortlist_entries WHERE shortlist_id IN "
                               "(SELECT id FROM shortlists WHERE employer_id = ?)", eid2),
                    self.count("SELECT COUNT(*) FROM contact_releases WHERE employer_id = ?", eid2),
                    self.count("SELECT COUNT(*) FROM jobs WHERE employer_id = ? AND status = 'open'", eid2))

        before = beta()
        self.assertEqual(before, (1, 1, 1, 1))
        r = self.client.post(f"/employers/{s['eid']}/close", json={"password": PASSWORD}, headers=s["eh"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(beta(), before)
        self.assertEqual(self.client.get("/me", headers=eh2).status_code, 200)
        self.assertEqual(self.row("SELECT phone FROM employers WHERE id = ?", eid2)["phone"], "07 4900 9999")

    def employer_principal(self, conn, employer_id):
        user_id = conn.execute("SELECT user_id FROM employers WHERE id = ?", (employer_id,)).fetchone()[0]
        return ac.principal_for_user(conn, user_id)

    def test_closing_takes_the_write_lock_before_counting_what_is_owed(self):
        # Count and closure must be one unit. If the count ran outside a transaction, an invoice recorded just
        # after it would be missed and the closure would commit anyway.
        s = self.busy_employer()
        conn = dl.connect(self.db)
        try:
            principal = self.employer_principal(conn, s["eid"])
            trace = []
            conn.set_trace_callback(lambda sql: trace.append((sql, conn.in_transaction)))
            ac.close_employer(conn, principal, s["eid"], password=PASSWORD)
            conn.set_trace_callback(None)
        finally:
            conn.close()
        self.assertEqual([in_transaction for sql, in_transaction in trace if "COUNT(*) FROM invoices" in sql], [True])

    def test_a_refused_closure_lets_go_of_the_write_lock(self):
        s = self.busy_employer(paid=False)
        conn = dl.connect(self.db)
        try:
            principal = self.employer_principal(conn, s["eid"])
            with self.assertRaises(dl.DataError):
                ac.close_employer(conn, principal, s["eid"], password=PASSWORD)
            self.assertFalse(conn.in_transaction)
            other = sqlite3.connect(self.db, timeout=0.3)  # would fail with "database is locked" if still held
            try:
                other.execute("UPDATE employers SET phone = phone")
                other.commit()
            finally:
                other.close()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
