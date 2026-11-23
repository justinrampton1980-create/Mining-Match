"""Tests for access_control. Run: python -m unittest -v   (from the python/ folder)"""
import json
import unittest
from pathlib import Path
from unittest import mock

import access_control as ac
import data_layer as dl
from test_data_layer import load_fixture_candidate

FIXTURES = json.loads(
    (Path(__file__).resolve().parent.parent / "fixtures" / "match_cases.json").read_text()
)
AS_OF = FIXTURES["as_of"]
PRIVATE = ["Jordan", "jordan@example.com", "0400 111 222"]  # c1's details employers must not see


def user_of(conn, table, row_id):
    return conn.execute(f"SELECT user_id FROM {table} WHERE id = ?", (row_id,)).fetchone()[0]


class World:
    """An admin, two employers, two candidates, jobs, shortlists and a document."""

    def __init__(self):
        self.conn = conn = dl.connect(":memory:")
        dl.init_schema(conn)
        admin = ac.create_admin(conn, "admin@example.com")
        self.e1 = dl.register_employer(conn, "e1@example.com", "Alpha Mining", contact_name="Pat Boss", email_verified=True)
        self.e2 = dl.register_employer(conn, "e2@example.com", "Beta Resources", email_verified=True)
        self.c1 = dl.register_candidate(
            conn, "jordan@example.com", "Jordan Private", location="QLD",
            phone="0400 111 222", summary="Call Jordan on 0400 111 222", email_verified=True)
        self.c2 = dl.register_candidate(conn, "sam@example.com", "Sam Other", location="NSW", email_verified=True)
        dl.set_candidate_licence(conn, self.c1, "Standard 11", "2028-05-01")
        dl.set_candidate_qualification(conn, self.c1, "Cert III Surface Extraction")
        dl.set_candidate_skill(conn, self.c1, "Dragline operation", 6)
        self.exp1 = dl.add_experience(conn, self.c1, "Dragline Operator", "2018-01-01", "2024-01-01")
        self.doc1 = dl.add_document(conn, self.c1, "licence", "s11.pdf", "candidates/c1/doc1",
                                    mime_type="application/pdf", size_bytes=1000)
        self.j1 = dl.create_job(conn, self.e1, FIXTURES["job"], status="open")
        self.j_draft = dl.create_job(conn, self.e1, {"title": "Secret draft"})
        self.j2 = dl.create_job(conn, self.e2, FIXTURES["job2"], status="open")
        self.s1 = dl.create_shortlist(conn, self.e1, "Dragline shortlist", self.j1)
        self.s2 = dl.create_shortlist(conn, self.e2, "Fitters")
        self.users = {
            "admin": admin,
            "e1": user_of(conn, "employers", self.e1), "e2": user_of(conn, "employers", self.e2),
            "c1": user_of(conn, "candidates", self.c1), "c2": user_of(conn, "candidates", self.c2),
        }
        self.p = {k: ac.principal_for_user(conn, u) for k, u in self.users.items()}
        self.terms_v = ac.publish_fee_terms(
            conn, self.p["admin"], fee_percent=12, fee_basis="Base salary, excluding allowances",
            window_months=12, guarantee_days=90, terms_text="Placement fee terms (draft for testing)",
        )

    def unlock(self, cand="c1", emp="e1"):
        """Turn both keys: candidate approves the employer, employer accepts terms and unlocks."""
        cid = {"c1": self.c1, "c2": self.c2}[cand]
        eid = {"e1": self.e1, "e2": self.e2}[emp]
        ac.set_contact_release(self.conn, self.p[cand], eid, True)
        ac.accept_fee_terms(self.conn, self.p[emp], self.terms_v)
        return ac.unlock_candidate(self.conn, self.p[emp], cid, self.terms_v)

    def audit(self, **where):
        clause = " AND ".join(f"{k} = ?" for k in where) or "1"
        return self.conn.execute(f"SELECT * FROM audit_log WHERE {clause} ORDER BY id", tuple(where.values())).fetchall()


class WorldTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.conn = self.w.conn
        self.p = self.w.p

    def tearDown(self):
        self.conn.close()


def _for_admin(w, p):
    return w.e1 if p.role == "admin" else None


# function, call, who may call it. Everyone else must get AccessDenied.
GRID = [
    ("view_candidate", lambda w, p: ac.view_candidate(w.conn, p, w.c1, as_of=AS_OF), {"c1", "e1", "e2", "admin"}),
    ("update_candidate", lambda w, p: ac.update_candidate(w.conn, p, w.c1, location="WA"), {"c1", "admin"}),
    ("set_candidate_licence", lambda w, p: ac.set_candidate_licence(w.conn, p, w.c1, "Coal Board Medical", "2027-06-30"), {"c1", "admin"}),
    ("set_candidate_qualification", lambda w, p: ac.set_candidate_qualification(w.conn, p, w.c1, "Cert IV"), {"c1", "admin"}),
    ("set_candidate_skill", lambda w, p: ac.set_candidate_skill(w.conn, p, w.c1, "Dozer operation", 2), {"c1", "admin"}),
    ("remove_candidate_licence", lambda w, p: ac.remove_candidate_licence(w.conn, p, w.c1, "Standard 11"), {"c1", "admin"}),
    ("remove_candidate_qualification", lambda w, p: ac.remove_candidate_qualification(w.conn, p, w.c1, "Cert III Surface Extraction"), {"c1", "admin"}),
    ("remove_candidate_skill", lambda w, p: ac.remove_candidate_skill(w.conn, p, w.c1, "Dragline operation"), {"c1", "admin"}),
    ("add_experience", lambda w, p: ac.add_experience(w.conn, p, w.c1, "Fitter", "2015-01-01", "2016-01-01"), {"c1", "admin"}),
    ("remove_experience", lambda w, p: ac.remove_experience(w.conn, p, w.exp1), {"c1", "admin"}),
    ("delete_candidate", lambda w, p: ac.delete_candidate(w.conn, p, w.c1), {"c1", "admin"}),
    ("add_document", lambda w, p: ac.add_document(w.conn, p, w.c1, "resume", "cv.pdf", mime_type="application/pdf", size_bytes=500), {"c1", "admin"}),
    ("list_documents", lambda w, p: ac.list_documents(w.conn, p, w.c1), {"c1", "admin"}),
    ("get_document_file", lambda w, p: ac.get_document_file(w.conn, p, w.doc1), {"c1", "admin"}),
    ("verify_document", lambda w, p: ac.verify_document(w.conn, p, w.doc1), {"admin"}),
    ("remove_document", lambda w, p: ac.remove_document(w.conn, p, w.doc1), {"c1", "admin"}),
    ("publish_fee_terms", lambda w, p: ac.publish_fee_terms(
        w.conn, p, fee_percent=10, fee_basis="Base salary", window_months=12, guarantee_days=90,
        terms_text="Version 2"), {"admin"}),
    ("get_fee_terms", lambda w, p: ac.get_fee_terms(w.conn, p), {"e1", "e2", "admin"}),
    ("accept_fee_terms", lambda w, p: ac.accept_fee_terms(w.conn, p, w.terms_v), {"e1", "e2"}),
    ("unlock_candidate", lambda w, p: (ac.set_contact_release(w.conn, w.p["c1"], w.e1, True),
                                       ac.accept_fee_terms(w.conn, w.p["e1"], w.terms_v),
                                       ac.unlock_candidate(w.conn, p, w.c1, w.terms_v)), {"e1"}),
    ("list_introductions", lambda w, p: ac.list_introductions(w.conn, p), {"e1", "e2", "admin"}),
    ("request_contact", lambda w, p: ac.request_contact(w.conn, p, w.c1), {"e1", "e2"}),
    ("set_contact_release", lambda w, p: ac.set_contact_release(w.conn, p, w.e1, True), {"c1", "c2"}),
    ("list_contact_requests", lambda w, p: ac.list_contact_requests(w.conn, p), {"c1", "c2", "e1", "e2"}),
    ("create_job", lambda w, p: ac.create_job(w.conn, p, {"title": "New"}, employer_id=_for_admin(w, p)), {"e1", "e2", "admin"}),
    ("update_job", lambda w, p: ac.update_job(w.conn, p, w.j1, {"title": "Renamed"}), {"e1", "admin"}),
    ("set_job_status", lambda w, p: ac.set_job_status(w.conn, p, w.j1, "closed"), {"e1", "admin"}),
    ("get_job (open)", lambda w, p: ac.get_job(w.conn, p, w.j1), {"c1", "c2", "e1", "admin"}),
    ("get_job (draft)", lambda w, p: ac.get_job(w.conn, p, w.j_draft), {"e1", "admin"}),
    ("list_my_jobs", lambda w, p: ac.list_my_jobs(w.conn, p), {"e1", "e2", "admin"}),
    ("list_open_jobs", lambda w, p: ac.list_open_jobs(w.conn, p), {"c1", "c2", "admin"}),
    ("rank_candidates_for_job", lambda w, p: ac.rank_candidates_for_job(w.conn, p, w.j1, as_of=AS_OF), {"e1", "admin"}),
    ("search_candidates", lambda w, p: ac.search_candidates(w.conn, p, {"required_licences": ["Standard 11"]}, as_of=AS_OF), {"e1", "e2", "admin"}),
    ("rank_jobs_for_candidate", lambda w, p: ac.rank_jobs_for_candidate(w.conn, p, w.c1, as_of=AS_OF), {"c1", "admin"}),
    ("create_shortlist", lambda w, p: ac.create_shortlist(w.conn, p, "New list", employer_id=_for_admin(w, p)), {"e1", "e2", "admin"}),
    ("list_shortlists", lambda w, p: ac.list_shortlists(w.conn, p, employer_id=_for_admin(w, p)), {"e1", "e2", "admin"}),
    ("get_shortlist", lambda w, p: ac.get_shortlist(w.conn, p, w.s1), {"e1", "admin"}),
    ("add_to_shortlist", lambda w, p: ac.add_to_shortlist(w.conn, p, w.s1, w.c1, as_of=AS_OF), {"e1", "admin"}),
    ("set_shortlist_status", lambda w, p: (dl.add_to_shortlist(w.conn, w.s1, w.c1),
                                           ac.set_shortlist_status(w.conn, p, w.s1, w.c1, "contacted")), {"e1", "admin"}),
    ("remove_from_shortlist", lambda w, p: ac.remove_from_shortlist(w.conn, p, w.s1, w.c1), {"e1", "admin"}),
    ("set_user_active", lambda w, p: ac.set_user_active(w.conn, p, w.users["c2"], False), {"admin"}),
    ("reset_two_factor", lambda w, p: ac.reset_two_factor(w.conn, p, w.users["c2"], "lost phone"), {"admin"}),
    ("read_audit_log", lambda w, p: ac.read_audit_log(w.conn, p), {"admin"}),
    ("list_users", lambda w, p: ac.list_users(w.conn, p, search="jordan"), {"admin"}),
    ("list_documents_to_verify", lambda w, p: ac.list_documents_to_verify(w.conn, p), {"admin"}),
    ("list_catalog", lambda w, p: ac.list_catalog(w.conn, p, "licence"), {"c1", "c2", "e1", "e2", "admin"}),
    ("create_admin", lambda w, p: ac.create_admin(w.conn, "second-admin@example.com", by=p), {"admin"}),
]
CALLERS = ("c1", "c2", "e1", "e2", "admin")


class PermissionGrid(unittest.TestCase):
    """Every guarded function x every kind of caller, each in a fresh database."""

    def test_every_caller_against_every_function(self):
        for name, call, allowed in GRID:
            for who in CALLERS:
                with self.subTest(function=name, caller=who):
                    w = World()
                    try:
                        if who in allowed:
                            call(w, w.p[who])  # must succeed outright
                        else:
                            with self.assertRaises(ac.AccessDenied):
                                call(w, w.p[who])
                            self.assertTrue(w.audit(outcome="denied", user_id=w.users[who]))
                    finally:
                        w.conn.close()

    def test_grid_covers_every_guarded_function(self):
        covered = {name.split()[0] for name, _, _ in GRID}
        public = {n for n in ac.__all__ if callable(getattr(ac, n))}
        self.assertEqual(public - covered, {"AccessDenied", "Principal", "principal_for_user"})


class Identity(WorldTest):
    def test_principals_come_from_the_database(self):
        self.assertEqual((self.p["c1"].role, self.p["c1"].candidate_id), ("candidate", self.w.c1))
        self.assertEqual((self.p["e1"].role, self.p["e1"].employer_id), ("employer", self.w.e1))
        self.assertEqual((self.p["admin"].candidate_id, self.p["admin"].employer_id), (None, None))

    def test_unknown_user_denied_and_logged(self):
        with self.assertRaises(ac.AccessDenied):
            ac.principal_for_user(self.conn, "no-such-user")
        self.assertEqual(self.w.audit(action="auth.principal")[0]["resource_id"], "no-such-user")

    def test_suspension_applies_to_an_existing_principal_immediately(self):
        held = self.p["c1"]
        ac.set_user_active(self.conn, self.p["admin"], self.w.users["c1"], False, "spam")
        with self.assertRaises(ac.AccessDenied):
            ac.view_candidate(self.conn, held, self.w.c1)
        with self.assertRaises(ac.AccessDenied):
            ac.principal_for_user(self.conn, self.w.users["c1"])
        ac.set_user_active(self.conn, self.p["admin"], self.w.users["c1"], True)
        ac.view_candidate(self.conn, held, self.w.c1)

    def test_forged_principals_rejected(self):
        u = self.w.users["c1"]
        cases = [
            (ac.Principal(u, "admin"), lambda p: ac.read_audit_log(self.conn, p)),
            (ac.Principal(u, "candidate", candidate_id=self.w.c2), lambda p: ac.update_candidate(self.conn, p, self.w.c2, location="X")),
            (ac.Principal(u, "employer", employer_id=self.w.e1), lambda p: ac.list_my_jobs(self.conn, p)),
            (ac.Principal("no-such-user", "admin"), lambda p: ac.read_audit_log(self.conn, p)),
            (None, lambda p: ac.read_audit_log(self.conn, p)),
            ({"user_id": self.w.users["admin"], "role": "admin"}, lambda p: ac.read_audit_log(self.conn, p)),
        ]
        for forged, call in cases:
            with self.subTest(forged=forged), self.assertRaises(ac.AccessDenied):
                call(forged)
        self.assertEqual(len(self.w.audit(user_id=u, detail="principal does not match account")), 3)


class EmployerPrivacy(WorldTest):
    def test_anonymised_view_is_an_allow_list(self):
        view = ac.view_candidate(self.conn, self.p["e1"], self.w.c1, as_of=AS_OF)
        self.assertEqual(set(view), set(ac.ANONYMOUS_FIELDS) | {"display_name", "contact_status", "contact_released"})
        self.assertTrue(view["display_name"].startswith("Candidate "))
        for secret in PRIVATE:
            self.assertNotIn(secret, json.dumps(view))

    def test_details_need_both_keys_and_hide_again_after_withdrawal(self):
        e1, c1 = self.p["e1"], self.w.c1
        self.assertEqual(ac.request_contact(self.conn, e1, c1, self.w.j1), "requested")
        self.assertEqual(ac.view_candidate(self.conn, e1, c1)["contact_status"], "requested")
        ac.set_contact_release(self.conn, self.p["c1"], self.w.e1, True)
        approved_only = ac.view_candidate(self.conn, e1, c1)  # first key only: still anonymised
        self.assertEqual((approved_only["contact_status"], approved_only["contact_released"]), ("approved", False))
        for secret in PRIVATE:
            self.assertNotIn(secret, json.dumps(approved_only))
        self.w.unlock()
        view = ac.view_candidate(self.conn, e1, c1)
        self.assertEqual(view["contact_status"], "unlocked")
        self.assertEqual(view["work_history"][0]["role"], "Dragline Operator")
        self.assertEqual((view["name"], view["email"], view["phone"]), ("Jordan Private", "jordan@example.com", "0400 111 222"))
        self.assertNotIn("storage_key", view["documents"][0])
        self.assertNotIn("name", ac.view_candidate(self.conn, self.p["e2"], c1))  # other employer still anonymised
        ac.set_contact_release(self.conn, self.p["c1"], self.w.e1, False)
        self.assertNotIn("name", ac.view_candidate(self.conn, e1, c1))
        self.assertEqual(ac.request_contact(self.conn, e1, c1), "declined")  # no repeat requests

    def test_rankings_hide_names_but_keep_scores(self):
        ids = {c["id"]: load_fixture_candidate(self.conn, c) for c in FIXTURES["candidates"]}
        by_db = {v: k for k, v in ids.items()}
        ranked = ac.rank_candidates_for_job(self.conn, self.p["e1"], self.w.j1, as_of=AS_OF)
        fixture_rows = [r for r in ranked if r["candidate_id"] in by_db]
        self.assertEqual([by_db[r["candidate_id"]] for r in fixture_rows], FIXTURES["expected_order"])
        for r in fixture_rows:
            self.assertEqual(r["score"], FIXTURES["expected"][by_db[r["candidate_id"]]]["score"])
            self.assertNotIn("candidate_name", r)
            self.assertTrue(r["display_name"].startswith("Candidate "))
        admin_names = {r["display_name"] for r in ac.rank_candidates_for_job(self.conn, self.p["admin"], self.w.j1, as_of=AS_OF)}
        self.assertIn("Alex Strong", admin_names)

    def _clones(self, n=8):
        """n candidates with identical profiles and alphabetical names."""
        made = []
        for i in range(n):
            name = chr(ord("A") + i) * 3
            cid = dl.register_candidate(self.conn, f"clone{i}@example.com", name, location="QLD", email_verified=True)
            dl.set_candidate_licence(self.conn, cid, "Shotfirer ticket", "2030-01-01")
            made.append(cid)
        return made

    def test_ranking_order_cannot_reveal_hidden_names(self):
        # Attack: copy a candidate's (visible) profile into test accounts with chosen names. If ties
        # were broken by name, the order would show whether the hidden name sorts before each test name.
        clones = self._clones()
        found = ac.search_candidates(self.conn, self.p["e1"], {"required_licences": ["Shotfirer ticket"]},
                                     as_of=AS_OF, eligible_only=True)
        self.assertEqual({r["score"] for r in found}, {100.0})
        self.assertEqual([r["candidate_id"] for r in found], sorted(clones))

    def test_shortlist_order_cannot_reveal_hidden_names(self):
        clones = self._clones()
        sl = ac.create_shortlist(self.conn, self.p["e1"], "Clones")
        for cid in clones:
            ac.add_to_shortlist(self.conn, self.p["e1"], sl, cid)
        self.conn.execute("UPDATE shortlist_entries SET added_at = '2026-01-01 00:00:00'")
        self.conn.commit()
        entries = ac.get_shortlist(self.conn, self.p["e1"], sl)["entries"]
        self.assertEqual([e["candidate_id"] for e in entries], sorted(clones))
        self.assertTrue(all("name" not in e for e in entries))

    def test_results_capped_for_employers_only(self):
        self._clones(5)
        crit = {"required_licences": ["Shotfirer ticket"]}
        with mock.patch.object(ac, "EMPLOYER_MAX_RESULTS", 2):
            self.assertEqual(len(ac.search_candidates(self.conn, self.p["e1"], crit)), 2)
            self.assertEqual(len(ac.search_candidates(self.conn, self.p["e1"], crit, limit=50)), 2)
            self.assertEqual(len(ac.search_candidates(self.conn, self.p["e1"], crit, limit=1)), 1)
            self.assertEqual(len(ac.search_candidates(self.conn, self.p["admin"], crit)), 7)
        for bad in (0, -1, True, "5", 2.5):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                ac.search_candidates(self.conn, self.p["e1"], crit, limit=bad)
        with self.assertRaises(ValueError):
            ac.search_candidates(self.conn, self.p["e1"], crit, as_admin=True)

    def test_hidden_or_suspended_candidates_are_unavailable(self):
        e1, c1 = self.p["e1"], self.w.c1
        crit = {"required_licences": ["Standard 11"]}
        ac.update_candidate(self.conn, self.p["c1"], c1, is_active=False)
        self.assertNotIn(c1, [r["candidate_id"] for r in ac.search_candidates(self.conn, e1, crit)])
        for call in (lambda: ac.view_candidate(self.conn, e1, c1),
                     lambda: ac.request_contact(self.conn, e1, c1),
                     lambda: ac.add_to_shortlist(self.conn, e1, self.w.s1, c1)):
            with self.assertRaises(ac.AccessDenied):
                call()
        ac.update_candidate(self.conn, self.p["c1"], c1, is_active=True)
        self.assertIn(c1, [r["candidate_id"] for r in ac.search_candidates(self.conn, e1, crit)])
        ac.set_user_active(self.conn, self.p["admin"], self.w.users["c1"], False)
        self.assertNotIn(c1, [r["candidate_id"] for r in ac.search_candidates(self.conn, e1, crit)])


class Documents(WorldTest):
    def test_storage_key_is_generated_and_filename_cleaned(self):
        c1 = self.p["c1"]
        cases = {"../../etc/passwd": "passwd", "C:\\Users\\me\\My CV.pdf": "My CV.pdf", "cv<script>.pdf": "cv_script_.pdf", "<b>cv</b>.pdf": "b_.pdf"}
        for given, stored in cases.items():
            doc = ac.add_document(self.conn, c1, self.w.c1, "resume", given, mime_type="application/pdf", size_bytes=10)
            self.assertTrue(doc["storage_key"].startswith(f"candidates/{self.w.c1}/"))
            self.assertNotIn("..", doc["storage_key"])
            row = self.conn.execute("SELECT filename FROM documents WHERE id = ?", (doc["id"],)).fetchone()
            self.assertEqual(row["filename"], stored)

    def test_upload_checks(self):
        before = self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        bad = [
            dict(filename="x.html", mime_type="text/html", size_bytes=10),
            dict(filename="x.pdf", mime_type="application/pdf", size_bytes=0),
            dict(filename="x.pdf", mime_type="application/pdf", size_bytes=ac.MAX_UPLOAD_BYTES + 1),
            dict(filename="x.pdf", mime_type="application/pdf", size_bytes=True),
            dict(filename=" .. ", mime_type="application/pdf", size_bytes=10),
        ]
        for kw in bad:
            with self.subTest(**kw), self.assertRaises(ValueError):
                ac.add_document(self.conn, self.p["c1"], self.w.c1, "resume", kw.pop("filename"), **kw)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], before)

    def test_employer_file_access_follows_approval(self):
        e1, e2 = self.p["e1"], self.p["e2"]
        for p in (e1, e2):
            with self.assertRaises(ac.AccessDenied):
                ac.get_document_file(self.conn, p, self.w.doc1)
        ac.set_contact_release(self.conn, self.p["c1"], self.w.e1, True)
        with self.assertRaises(ac.AccessDenied):  # approval alone is not enough
            ac.get_document_file(self.conn, e1, self.w.doc1)
        self.w.unlock()
        self.assertEqual(ac.get_document_file(self.conn, e1, self.w.doc1)["storage_key"], "candidates/c1/doc1")
        self.assertEqual(len(ac.list_documents(self.conn, e1, self.w.c1)), 1)
        with self.assertRaises(ac.AccessDenied):
            ac.get_document_file(self.conn, e2, self.w.doc1)
        ac.set_contact_release(self.conn, self.p["c1"], self.w.e1, False)
        with self.assertRaises(ac.AccessDenied):
            ac.list_documents(self.conn, e1, self.w.c1)

    def test_lists_never_include_storage_keys(self):
        for doc in ac.list_documents(self.conn, self.p["c1"], self.w.c1):
            self.assertNotIn("storage_key", doc)

    def test_cannot_attach_another_candidates_document(self):
        with self.assertRaises(dl.DataError):
            ac.set_candidate_licence(self.conn, self.p["c2"], self.w.c2, "Standard 11", document_id=self.w.doc1)


class ContactRequests(WorldTest):
    def test_candidate_sees_who_asked_employer_sees_status(self):
        ac.request_contact(self.conn, self.p["e1"], self.w.c1, self.w.j1)
        (req,) = ac.list_contact_requests(self.conn, self.p["c1"])
        self.assertEqual((req["company_name"], req["job_title"], req["status"]),
                         ("Alpha Mining", "Dragline Operator", "requested"))
        self.assertNotIn("Pat Boss", json.dumps(req))
        (mine,) = ac.list_contact_requests(self.conn, self.p["e1"])
        self.assertEqual(mine["status"], "requested")
        self.assertNotIn("Jordan", json.dumps(mine))

    def test_cannot_request_using_another_employers_job(self):
        with self.assertRaises(ac.AccessDenied):
            ac.request_contact(self.conn, self.p["e2"], self.w.c1, self.w.j1)

    def test_candidate_can_refuse_before_being_asked(self):
        ac.set_contact_release(self.conn, self.p["c1"], self.w.e2, False)
        self.assertEqual(ac.request_contact(self.conn, self.p["e2"], self.w.c1), "declined")

    def test_unknown_employer(self):
        with self.assertRaises(dl.NotFoundError):
            ac.set_contact_release(self.conn, self.p["c1"], "no-such-employer", True)


class TwoStepUnlock(WorldTest):
    def test_unlock_needs_candidate_approval_and_accepted_terms(self):
        e1, c1 = self.p["e1"], self.w.c1
        with self.assertRaises(ac.AccessDenied):  # neither key
            ac.unlock_candidate(self.conn, e1, c1, self.w.terms_v)
        ac.accept_fee_terms(self.conn, e1, self.w.terms_v)
        with self.assertRaises(ac.AccessDenied):  # terms accepted, candidate hasn't approved
            ac.unlock_candidate(self.conn, e1, c1, self.w.terms_v)
        ac.set_contact_release(self.conn, self.p["c2"], self.w.e2, True)
        with self.assertRaises(ac.AccessDenied):  # candidate approved, terms not accepted
            ac.unlock_candidate(self.conn, self.p["e2"], self.w.c2, self.w.terms_v)
        self.assertEqual(self.w.audit(action="candidate.unlock", outcome="allowed"), [])

    def test_new_terms_must_be_accepted_before_more_unlocks(self):
        first = self.w.unlock()
        self.assertEqual((first["new_introduction"], first["terms_version"]), (True, 1))
        v2 = ac.publish_fee_terms(self.conn, self.p["admin"], fee_percent=10, fee_basis="Base salary",
                                  window_months=12, guarantee_days=60, terms_text="Version 2")
        self.assertEqual(v2, 2)
        ac.set_contact_release(self.conn, self.p["c2"], self.w.e1, True)
        with self.assertRaises(dl.DataError):  # old version no longer accepted for unlocking
            ac.unlock_candidate(self.conn, self.p["e1"], self.w.c2, 1)
        with self.assertRaises(ac.AccessDenied):  # v2 not accepted yet
            ac.unlock_candidate(self.conn, self.p["e1"], self.w.c2, 2)
        with self.assertRaises(dl.DataError):  # can't accept an outdated version
            ac.accept_fee_terms(self.conn, self.p["e1"], 1)
        terms = ac.get_fee_terms(self.conn, self.p["e1"])
        self.assertEqual((terms["version"], terms["accepted"], terms["is_current"]), (2, False, True))
        ac.accept_fee_terms(self.conn, self.p["e1"], 2)
        self.assertEqual(ac.unlock_candidate(self.conn, self.p["e1"], self.w.c2, 2)["terms_version"], 2)
        # Earlier unlock still shows details and keeps its original terms
        self.assertTrue(ac.view_candidate(self.conn, self.p["e1"], self.w.c1)["contact_released"])
        old = ac.get_fee_terms(self.conn, self.p["e1"], 1)
        self.assertEqual((old["accepted"], old["is_current"]), (True, False))

    def test_introduction_recorded_once_and_survives_withdrawal_and_deletion(self):
        self.w.unlock()
        again = ac.unlock_candidate(self.conn, self.p["e1"], self.w.c1, self.w.terms_v)
        self.assertFalse(again["new_introduction"])
        ac.set_contact_release(self.conn, self.p["c1"], self.w.e1, False)  # candidate withdraws
        self.assertFalse(ac.view_candidate(self.conn, self.p["e1"], self.w.c1)["contact_released"])
        (intro,) = ac.list_introductions(self.conn, self.p["e1"])
        self.assertTrue(intro["display_name"].startswith("Candidate "))
        with self.assertRaises(ac.AccessDenied):  # can't re-unlock without approval
            ac.unlock_candidate(self.conn, self.p["e1"], self.w.c1, self.w.terms_v)
        ac.set_contact_release(self.conn, self.p["c1"], self.w.e1, True)  # approves again: no new intro needed
        self.assertTrue(ac.view_candidate(self.conn, self.p["e1"], self.w.c1)["contact_released"])
        ac.delete_candidate(self.conn, self.p["c1"], self.w.c1)
        (record,) = ac.list_introductions(self.conn, self.p["admin"])
        self.assertEqual((record["candidate_id"], record["candidate_name"], record["company_name"]),
                         (self.w.c1, None, "Alpha Mining"))
        self.assertEqual(ac.list_introductions(self.conn, self.p["e2"]), [])

    def test_published_terms_cannot_be_changed(self):
        import sqlite3
        for stmt in ("UPDATE fee_terms SET fee_percent = 1", "DELETE FROM fee_terms"):
            with self.subTest(stmt=stmt), self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute(stmt)

    def test_terms_validation(self):
        good = dict(fee_percent=12, fee_basis="Base", window_months=12, guarantee_days=90, terms_text="T")
        for field, bad in [("fee_percent", 0), ("fee_percent", 101), ("fee_percent", True), ("fee_percent", "12"),
                           ("window_months", 0), ("window_months", 1.5), ("guarantee_days", -1),
                           ("terms_text", "  "), ("fee_basis", ""), ("min_fee_aud", -5)]:
            with self.subTest(field=field, value=bad), self.assertRaises(ValueError):
                ac.publish_fee_terms(self.conn, self.p["admin"], **{**good, field: bad})

    def test_cannot_unlock_hidden_candidate_or_with_another_employers_job(self):
        ac.set_contact_release(self.conn, self.p["c1"], self.w.e2, True)
        ac.accept_fee_terms(self.conn, self.p["e2"], self.w.terms_v)
        with self.assertRaises(ac.AccessDenied):
            ac.unlock_candidate(self.conn, self.p["e2"], self.w.c1, self.w.terms_v, job_id=self.w.j1)
        ac.update_candidate(self.conn, self.p["c1"], self.w.c1, is_active=False)
        with self.assertRaises(ac.AccessDenied):
            ac.unlock_candidate(self.conn, self.p["e2"], self.w.c1, self.w.terms_v)

    def test_unconfirmed_accounts_are_invisible(self):
        cid = dl.register_candidate(self.conn, "new@example.com", "New Person", location="QLD")
        dl.set_candidate_licence(self.conn, cid, "Standard 11", "2030-01-01")
        found = ac.search_candidates(self.conn, self.p["e1"], {"required_licences": ["Standard 11"]})
        self.assertNotIn(cid, [r["candidate_id"] for r in found])
        with self.assertRaises(ac.AccessDenied):
            ac.view_candidate(self.conn, self.p["e1"], cid)

    def test_suspension_ends_sessions(self):
        now = 1_800_000_000
        self.conn.execute("INSERT INTO sessions VALUES ('h', ?, ?, ?, ?)", (self.w.users["c2"], now, now + 999, now))
        self.conn.commit()
        ac.set_user_active(self.conn, self.p["admin"], self.w.users["c2"], False)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sessions WHERE token_hash = 'h'").fetchone())


class Jobs(WorldTest):
    def test_candidates_see_open_jobs_from_active_employers_only(self):
        titles = lambda: {j["title"] for j in ac.list_open_jobs(self.conn, self.p["c1"])}
        self.assertEqual(titles(), {"Dragline Operator", "Maintenance Fitter"})
        self.assertEqual(ac.get_job(self.conn, self.p["c1"], self.w.j2)["company_name"], "Beta Resources")
        ac.set_user_active(self.conn, self.p["admin"], self.w.users["e2"], False)
        self.assertEqual(titles(), {"Dragline Operator"})
        with self.assertRaises(ac.AccessDenied):
            ac.get_job(self.conn, self.p["c1"], self.w.j2)
        ranked = ac.rank_jobs_for_candidate(self.conn, self.p["c1"], self.w.c1, as_of=AS_OF)
        self.assertEqual([(r["job_title"], r["company_name"]) for r in ranked], [("Dragline Operator", "Alpha Mining")])

    def test_employers_act_only_for_themselves(self):
        with self.assertRaises(ac.AccessDenied):
            ac.create_job(self.conn, self.p["e1"], {"title": "X"}, employer_id=self.w.e2)
        with self.assertRaises(ac.AccessDenied):
            ac.list_my_jobs(self.conn, self.p["e1"], employer_id=self.w.e2)
        with self.assertRaises(ValueError):
            ac.create_job(self.conn, self.p["admin"], {"title": "X"})  # admin must say which employer
        self.assertEqual({j["title"] for j in ac.list_my_jobs(self.conn, self.p["e1"])},
                         {"Dragline Operator", "Secret draft"})


class Shortlists(WorldTest):
    def test_names_hidden_until_approved(self):
        ac.add_to_shortlist(self.conn, self.p["e1"], self.w.s1, self.w.c1, notes="good", as_of=AS_OF)
        (entry,) = ac.get_shortlist(self.conn, self.p["e1"], self.w.s1)["entries"]
        self.assertNotIn("name", entry)
        self.assertIsNotNone(entry["score_at_add"])
        ac.set_contact_release(self.conn, self.p["c1"], self.w.e1, True)
        (entry,) = ac.get_shortlist(self.conn, self.p["e1"], self.w.s1)["entries"]
        self.assertEqual((entry["contact_status"], entry["contact_released"]), ("approved", False))
        self.assertNotIn("Jordan", json.dumps(entry))
        self.w.unlock()
        (entry,) = ac.get_shortlist(self.conn, self.p["e1"], self.w.s1)["entries"]
        self.assertEqual((entry["display_name"], entry["contact_released"]), ("Jordan Private", True))
        (admin_entry,) = ac.get_shortlist(self.conn, self.p["admin"], self.w.s1)["entries"]
        self.assertEqual(admin_entry["name"], "Jordan Private")

    def test_cannot_link_another_employers_job(self):
        with self.assertRaises(ac.AccessDenied):
            ac.create_shortlist(self.conn, self.p["e2"], "Poach", self.w.j1)


class AdminAndAudit(WorldTest):
    def test_admin_cannot_suspend_self(self):
        with self.assertRaises(ac.AccessDenied):
            ac.set_user_active(self.conn, self.p["admin"], self.w.users["admin"], False)

    def test_first_admin_setup_only_works_once(self):
        with self.assertRaises(ac.AccessDenied):
            ac.create_admin(self.conn, "sneaky@example.com")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM users WHERE email = 'sneaky@example.com'").fetchone())

    def test_allowed_and_denied_actions_are_logged(self):
        with self.assertRaises(ac.AccessDenied):
            ac.update_job(self.conn, self.p["e2"], self.w.j1, {"title": "Hijack"})
        ac.update_job(self.conn, self.p["e1"], self.w.j1, {"title": "Renamed"})
        rows = [(r["user_id"], r["outcome"]) for r in self.w.audit(action="job.update", resource_id=self.w.j1)]
        self.assertEqual(rows, [(self.w.users["e2"], "denied"), (self.w.users["e1"], "allowed")])
        self.assertEqual(ac.get_job(self.conn, self.p["admin"], self.w.j1)["title"], "Renamed")

    def test_audit_log_holds_no_personal_details(self):
        c1, e1 = self.p["c1"], self.p["e1"]
        ac.update_candidate(self.conn, c1, self.w.c1, phone="0400 111 222", summary="Jordan here")
        ac.request_contact(self.conn, e1, self.w.c1, self.w.j1)
        self.w.unlock()
        ac.list_introductions(self.conn, self.p["admin"])
        ac.view_candidate(self.conn, e1, self.w.c1)
        ac.get_document_file(self.conn, e1, self.w.doc1)
        ac.rank_candidates_for_job(self.conn, e1, self.w.j1, as_of=AS_OF)
        with self.assertRaises(ac.AccessDenied):
            ac.view_candidate(self.conn, self.p["c2"], self.w.c1)
        dump = json.dumps([dict(r) for r in self.w.audit()])
        for secret in PRIVATE + ["Alpha Mining", "Pat Boss", "s11.pdf"]:
            self.assertNotIn(secret, dump)

    def test_admin_lists(self):
        found = ac.list_users(self.conn, self.p["admin"], search="ALPHA")
        self.assertEqual([(u["company_name"], u["role"]) for u in found], [("Alpha Mining", "employer")])
        self.assertEqual(ac.list_users(self.conn, self.p["admin"], search="%"), [])  # wildcards are ignored
        self.assertEqual(len(ac.list_users(self.conn, self.p["admin"], role="candidate")), 2)
        (doc,) = ac.list_documents_to_verify(self.conn, self.p["admin"])
        self.assertEqual((doc["id"], doc["candidate_name"]), (self.w.doc1, "Jordan Private"))
        ac.verify_document(self.conn, self.p["admin"], self.w.doc1)
        self.assertEqual(ac.list_documents_to_verify(self.conn, self.p["admin"]), [])

    def test_audit_survives_account_deletion(self):
        ac.delete_candidate(self.conn, self.p["c1"], self.w.c1)
        self.assertTrue(self.w.audit(user_id=self.w.users["c1"], action="candidate.delete"))

    def test_read_audit_log_filters_and_is_itself_logged(self):
        with self.assertRaises(ac.AccessDenied):
            ac.read_audit_log(self.conn, self.p["e1"])
        denied = ac.read_audit_log(self.conn, self.p["admin"], outcome="denied")
        self.assertTrue(denied and all(r["outcome"] == "denied" for r in denied))
        self.assertTrue(self.w.audit(action="audit.read", user_id=self.w.users["admin"]))
        for bad in (0, ac.AUDIT_MAX_ROWS + 1, "10"):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                ac.read_audit_log(self.conn, self.p["admin"], limit=bad)


if __name__ == "__main__":
    unittest.main()
