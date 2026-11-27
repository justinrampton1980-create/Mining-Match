"""Data export for privacy access requests (launch blocker B2)."""
import io
import json
import time
import unittest
import zipfile
from unittest import mock

import auth
import data_layer as dl
from test_api import BILLING, JOB, PDF, ApiTest

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class DataExport(ApiTest):
    def full_candidate_story(self):
        """A candidate whose data touches every table: files, a request, an unlock, a shortlist, a hire."""
        ch, cid = self.candidate()
        self.assertEqual(self.upload(ch, cid).status_code, 201)
        self.assertEqual(self.upload(ch, cid, PNG, "ticket photo.png", "image/png", "other").status_code, 201)
        eh, eid = self.employer()
        job_id = self.client.post("/jobs", json={**JOB, "status": "open"}, headers=eh).json()["id"]
        self.client.post(f"/candidates/{cid}/contact-request", json={"job_id": job_id}, headers=eh)
        self.client.put(f"/contact-releases/{eid}", json={"grant": True}, headers=ch)
        version = self.publish_terms()
        self.client.post(f"/fee-terms/{version}/accept", headers=eh)
        self.assertEqual(self.client.post(f"/candidates/{cid}/unlock", json={"terms_version": version, "job_id": job_id},
                                          headers=eh).status_code, 200)
        sid = self.client.post("/shortlists", json={"name": "Dragline crew", "job_id": job_id}, headers=eh).json()["id"]
        self.client.post(f"/shortlists/{sid}/entries", json={"candidate_id": cid, "notes": "Strong on night shift"},
                         headers=eh)
        r = self.client.post("/hires", headers=eh, json={"candidate_id": cid, "start_date": BILLING.today().isoformat(),
                                                         "base_salary": 120000, "job_id": job_id})
        self.assertEqual(r.status_code, 201, r.text)
        return ch, cid, eh, eid

    def unzip(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertRegex(response.headers["content-disposition"], r'^attachment; filename="mining-match-[a-z-]+-\d{4}-\d{2}-\d{2}\.zip"$')
        self.assertEqual(response.headers["cache-control"], "no-store")
        z = zipfile.ZipFile(io.BytesIO(response.content))
        return z, json.loads(z.read("data.json"))

    def test_a_candidate_gets_everything_held_about_them(self):
        ch, cid, eh, eid = self.full_candidate_story()
        z, data = self.unzip(self.client.get("/me/export", headers=ch))
        self.assertIn("README.txt", z.namelist())
        self.assertEqual(data["account"]["email"], "jordan@example.com")
        c = data["candidate"]
        self.assertEqual((c["name"], c["phone"], c["location"]), ("Jordan Private", "0400 111 222", "QLD"))
        self.assertEqual([(l["name"], l["expiry_date"]) for l in c["licences"]], [("Standard 11", "2031-05-01")])
        self.assertEqual([q["name"] for q in c["qualifications"]], ["Cert III Surface Extraction"])
        self.assertEqual([(s["name"], s["years"]) for s in c["skills"]], [("Dragline operation", 6)])
        self.assertEqual([w["role"] for w in c["work_history"]], ["Dragline Operator"])
        (req,) = data["contact_requests"]
        self.assertEqual((req["employer"], req["job"], req["status"]), ("Alpha Mining", "Dragline Operator", "granted"))
        (intro,) = data["introductions"]
        self.assertEqual((intro["employer"], intro["job"]), ("Alpha Mining", "Dragline Operator"))
        (entry,) = data["shortlists"]
        self.assertEqual((entry["employer"], entry["shortlist"], entry["notes"]),
                         ("Alpha Mining", "Dragline crew", "Strong on night shift"))
        (hire,) = data["hires"]
        self.assertEqual((hire["employer"], hire["base_salary_aud"]), ("Alpha Mining", 120000))
        self.assertTrue(data["sessions"])
        self.assertIn("user.export_data", [a["action"] for a in data["activity"]])

        # The uploaded files, byte for byte, linked from their document records.
        files = {d["filename"]: d["file_in_this_export"] for d in c["documents"]}
        self.assertEqual(z.read(files["licence.pdf"]), PDF)
        self.assertEqual(z.read(files["ticket photo.png"]), PNG)
        self.assertTrue(all(n.startswith("documents/") and " " not in n for n in files.values()))

    def test_no_secrets_or_the_employers_fee_in_the_export(self):
        ch, cid, eh, eid = self.full_candidate_story()
        conn = dl.connect(self.db)
        try:
            secrets = [r[0] for r in conn.execute("SELECT password_hash FROM users WHERE email = 'jordan@example.com'")]
            secrets += [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]
            secrets += [r[0] for r in conn.execute("SELECT token_hash FROM auth_tokens")]
            secrets += [r[0] for r in conn.execute("SELECT reminder_token FROM candidates WHERE reminder_token IS NOT NULL")]
            secrets += [r[0] for r in conn.execute("SELECT storage_key FROM documents")]
            fee = conn.execute("SELECT fee_cents FROM placements").fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(len(secrets), 5)
        r = self.client.get("/me/export", headers=ch)
        z, data = self.unzip(r)
        everything = b"".join(z.read(n) for n in z.namelist())
        for s in secrets:
            self.assertNotIn(s.encode(), everything)
        self.assertNotIn("fee", json.dumps(data["hires"]))
        self.assertNotIn(str(fee).encode(), z.read("data.json"))
        self.assertNotIn(b"password", z.read("data.json"))

    def test_an_employer_export_carries_no_candidates_details(self):
        ch, cid, eh, eid = self.full_candidate_story()
        z, data = self.unzip(self.client.get("/me/export", headers=eh))
        self.assertEqual(data["employer"]["company_name"], "Alpha Mining")
        (job,) = data["employer"]["jobs"]
        self.assertEqual(job["title"], "Dragline Operator")
        self.assertTrue(job["requirements"])
        self.assertTrue(data["employer"]["accepted_placement_terms"])
        self.assertNotIn("candidate", data)
        everything = b"".join(z.read(n) for n in z.namelist())
        for private in (b"Jordan", b"jordan@example.com", b"0400 111 222", b"night shift", PDF):
            self.assertNotIn(private, everything)
        self.assertEqual([n for n in z.namelist() if n.startswith("documents/")], [])

    def test_admins_can_export_anyone_and_it_is_logged(self):
        ch, cid, eh, eid = self.full_candidate_story()
        me = self.client.get("/me", headers=ch).json()
        z, data = self.unzip(self.client.get(f"/admin/users/{me['user_id']}/export", headers=self.admin))
        self.assertEqual(data["candidate"]["name"], "Jordan Private")
        admin_id = self.client.get("/me", headers=self.admin).json()["user_id"]
        log = self.client.get(f"/admin/audit-log?user_id={admin_id}&resource_id={me['user_id']}", headers=self.admin)
        self.assertEqual(log.status_code, 200, log.text)
        self.assertIn(("user.export_data", "allowed", "by an admin"),
                      [(a["action"], a["outcome"], a["detail"]) for a in log.json()])
        self.assertEqual(self.client.get("/admin/users/no-such-user/export", headers=self.admin).status_code, 404)

    def test_nobody_else_can_export_your_data(self):
        ch, cid, eh, eid = self.full_candidate_story()
        jordan = self.client.get("/me", headers=ch).json()["user_id"]
        other, _ = self.candidate("sam@example.com", "Sam Other")
        for h in (eh, other):
            r = self.client.get(f"/admin/users/{jordan}/export", headers=h)
            self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get("/me/export").status_code, 401)
        # Sam's own export holds only Sam.
        z, data = self.unzip(self.client.get("/me/export", headers=other))
        self.assertNotIn(b"Jordan", b"".join(z.read(n) for n in z.namelist()))

    def test_self_service_exports_are_limited(self):
        ch, cid = self.candidate()
        now = [int(time.time())]
        with mock.patch.object(auth, "clock", lambda: now[0]):
            h = self.login("jordan@example.com")
            for _ in range(auth.DATA_EXPORTS_PER_HOUR[0]):
                self.assertEqual(self.client.get("/me/export", headers=h).status_code, 200)
            r = self.client.get("/me/export", headers=h)
            self.assertEqual(r.status_code, 429)
            now[0] += 3601
            self.assertEqual(self.client.get("/me/export", headers=h).status_code, 200)

    def test_a_missing_file_doesnt_stop_the_export(self):
        ch, cid = self.candidate()
        doc = self.upload(ch, cid).json()
        conn = dl.connect(self.db)
        try:
            key = conn.execute("SELECT storage_key FROM documents WHERE id = ?", (doc["id"],)).fetchone()[0]
        finally:
            conn.close()
        # delete the stored file underneath the app
        for p in (self.dir / "files").rglob("*"):
            if p.is_file():
                p.unlink()
        z, data = self.unzip(self.client.get("/me/export", headers=ch))
        (d,) = data["candidate"]["documents"]
        self.assertIsNone(d["file_in_this_export"])
        self.assertEqual(d["filename"], "licence.pdf")
        self.assertTrue(key)

    def test_an_admin_export_of_an_admin_is_account_and_activity_only(self):
        me = self.client.get("/me", headers=self.admin).json()
        z, data = self.unzip(self.client.get("/me/export", headers=self.admin))
        self.assertEqual(data["account"]["role"], "admin")
        self.assertNotIn("candidate", data)
        self.assertNotIn("employer", data)
        self.assertEqual(data["account"]["id"], me["user_id"])


if __name__ == "__main__":
    unittest.main()
