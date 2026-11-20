"""Tests for data_layer. Run: python -m unittest -v   (from the python/ folder)"""
import json
import sqlite3
import unittest
from datetime import date, timedelta
from pathlib import Path

import data_layer as dl
import matching_engine as me

FIXTURES = json.loads(
    (Path(__file__).resolve().parent.parent / "fixtures" / "match_cases.json").read_text()
)
AS_OF = FIXTURES["as_of"]


def span(years):
    """(start, end) ISO dates covering `years` (loader rounds back to the same figure)."""
    end = date(2024, 1, 1)
    return (end - timedelta(days=round(years * 365.25))).isoformat(), end.isoformat()


def load_fixture_candidate(conn, c):
    """Insert a fixtures-format candidate through the public API. Returns the new id."""
    cid = dl.register_candidate(
        conn,
        f"{c['id']}@example.com",
        c["name"],
        location=c.get("location"),
        willing_fifo=c.get("willing_fifo", False),
        willing_to_relocate=c.get("willing_to_relocate", False), email_verified=True)
    for lic in c.get("licences", []):
        if isinstance(lic, dict):
            dl.set_candidate_licence(conn, cid, lic["type"], lic.get("expiry"))
        else:
            dl.set_candidate_licence(conn, cid, lic)
    for q in c.get("qualifications", []):
        dl.set_candidate_qualification(conn, cid, q)
    for s in c.get("skills", []):
        if isinstance(s, dict):
            dl.set_candidate_skill(conn, cid, s["name"], s.get("years"))
        else:
            dl.set_candidate_skill(conn, cid, s)
    for e in c.get("experience", []):
        start, end = span(e["years"])
        dl.add_experience(conn, cid, e["role"], start, end)
    return cid


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = dl.connect(":memory:")
        dl.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def load_everything(self, job_status="open"):
        """Employer + fixture job + fixture candidates. Returns (employer_id, job_id, {fixture_id: db_id})."""
        employer = dl.register_employer(self.conn, "hr@bigmine.example", "Big Mine Pty Ltd", email_verified=True)
        job_id = dl.create_job(self.conn, employer, FIXTURES["job"], status=job_status)
        ids = {c["id"]: load_fixture_candidate(self.conn, c) for c in FIXTURES["candidates"]}
        return employer, job_id, ids


class Schema(Base):
    def test_init_is_idempotent(self):
        dl.init_schema(self.conn)  # second call must not fail

    def test_foreign_keys_enforce_catalog_kind(self):
        cid = dl.register_candidate(self.conn, "a@example.com", "A", email_verified=True)
        licence_item = dl.get_or_create_item(self.conn, "licence", "Standard 11")
        with self.assertRaises(sqlite3.IntegrityError):  # a licence cannot be filed as a skill
            self.conn.execute(
                "INSERT INTO candidate_skills (candidate_id, item_id) VALUES (?, ?)", (cid, licence_item)
            )


class Registration(Base):
    def test_duplicate_email_is_case_insensitive(self):
        dl.register_candidate(self.conn, "Sam@Example.com", "Sam", email_verified=True)
        with self.assertRaises(dl.DuplicateError):
            dl.register_employer(self.conn, "sam@example.COM", "Sam Co", email_verified=True)

    def test_failed_registration_leaves_nothing_behind(self):
        with self.assertRaises(ValueError):
            dl.register_candidate(self.conn, "no-name@example.com", "   ", email_verified=True)
        count = self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        self.assertEqual(count, 0)

    def test_update_candidate(self):
        cid = dl.register_candidate(self.conn, "a@example.com", "A", location="QLD", email_verified=True)
        dl.update_candidate(self.conn, cid, location="NSW", willing_fifo=True, available_from=date(2027, 1, 5))
        c = dl.load_candidates(self.conn, [cid])[0]
        self.assertEqual((c["location"], c["willing_fifo"], c["available_from"]), ("NSW", True, "2027-01-05"))
        with self.assertRaises(ValueError):
            dl.update_candidate(self.conn, cid, user_id="x")
        with self.assertRaises(dl.NotFoundError):
            dl.update_candidate(self.conn, "missing", name="B")


class Catalog(Base):
    def test_aliases_share_one_item(self):
        dl.add_alias(self.conn, "SS11", "Standard 11")
        a = dl.get_or_create_item(self.conn, "licence", "SS11")
        b = dl.get_or_create_item(self.conn, "licence", "standard  11")
        self.assertEqual(a, b)
        self.assertEqual(len(dl.list_items(self.conn, "licence")), 1)

    def test_load_aliases_feeds_the_engine(self):
        dl.add_alias(self.conn, "tkt", "dogging ticket")
        me.ALIASES.pop("tkt", None)
        self.assertEqual(dl.load_aliases(self.conn), 1)
        self.assertEqual(me.normalize("TKT"), "dogging ticket")

    def test_same_name_in_different_kinds_is_separate(self):
        a = dl.get_or_create_item(self.conn, "skill", "Blasting")
        b = dl.get_or_create_item(self.conn, "licence", "Blasting")
        self.assertNotEqual(a, b)


class CandidateProfile(Base):
    def test_engine_shape_round_trip(self):
        cid = dl.register_candidate(self.conn, "a@example.com", "Ann", location="QLD", willing_fifo=True, email_verified=True)
        dl.set_candidate_licence(self.conn, cid, "Standard 11", "2028-05-01")
        dl.set_candidate_licence(self.conn, cid, "Working at Heights")
        dl.set_candidate_qualification(self.conn, cid, "Cert III Surface Extraction", institution="TAFE")
        dl.set_candidate_skill(self.conn, cid, "Dragline operation", 6)
        dl.set_candidate_skill(self.conn, cid, "Dozer operation")
        dl.add_experience(self.conn, cid, "Dragline Operator", "2018-01-01", "2022-01-01")
        c = dl.load_candidates(self.conn, [cid], as_of=AS_OF)[0]
        self.assertEqual(
            c["licences"],
            [{"type": "Standard 11", "expiry": "2028-05-01"}, {"type": "Working at Heights", "expiry": None}],
        )
        self.assertEqual(c["qualifications"], ["Cert III Surface Extraction"])
        self.assertEqual(
            c["skills"], [{"name": "Dozer operation", "years": None}, {"name": "Dragline operation", "years": 6.0}]
        )
        self.assertEqual(c["experience"], [{"role": "Dragline Operator", "years": 4.0}])
        self.assertTrue(c["willing_fifo"])

    def test_licence_renewal_updates_in_place(self):
        cid = dl.register_candidate(self.conn, "a@example.com", "Ann", email_verified=True)
        dl.set_candidate_licence(self.conn, cid, "Coal Board Medical", "2026-10-31")
        dl.set_candidate_licence(self.conn, cid, "coal board medical", "2028-10-31")
        lic = dl.load_candidates(self.conn, [cid])[0]["licences"]
        self.assertEqual(lic, [{"type": "Coal Board Medical", "expiry": "2028-10-31"}])

    def test_open_ended_experience_runs_to_as_of(self):
        cid = dl.register_candidate(self.conn, "a@example.com", "Ann", email_verified=True)
        dl.add_experience(self.conn, cid, "Fitter", "2020-01-01")
        c = dl.load_candidates(self.conn, [cid], as_of="2024-01-01")[0]
        self.assertEqual(c["experience"][0]["years"], 4.0)

    def test_remove_items(self):
        cid = dl.register_candidate(self.conn, "a@example.com", "Ann", email_verified=True)
        dl.set_candidate_licence(self.conn, cid, "Standard 11")
        dl.set_candidate_skill(self.conn, cid, "Welding", 2)
        exp = dl.add_experience(self.conn, cid, "Welder", "2020-01-01", "2021-01-01")
        self.assertTrue(dl.remove_candidate_licence(self.conn, cid, "standard 11"))
        self.assertFalse(dl.remove_candidate_licence(self.conn, cid, "standard 11"))
        self.assertTrue(dl.remove_candidate_skill(self.conn, cid, "Welding"))
        self.assertTrue(dl.remove_experience(self.conn, exp))
        c = dl.load_candidates(self.conn, [cid])[0]
        self.assertEqual((c["licences"], c["skills"], c["experience"]), ([], [], []))

    def test_validation(self):
        cid = dl.register_candidate(self.conn, "a@example.com", "Ann", email_verified=True)
        with self.assertRaises(ValueError):
            dl.set_candidate_licence(self.conn, cid, "Standard 11", "not-a-date")
        with self.assertRaises(ValueError):
            dl.add_experience(self.conn, cid, "Fitter", "2021-01-01", "2020-01-01")
        with self.assertRaises(ValueError):
            dl.set_candidate_skill(self.conn, cid, "Welding", -1)
        with self.assertRaises(dl.NotFoundError):
            dl.set_candidate_skill(self.conn, "missing", "Welding", 1)

    def test_inactive_candidates_hidden_by_default(self):
        cid = dl.register_candidate(self.conn, "a@example.com", "Ann", email_verified=True)
        dl.update_candidate(self.conn, cid, is_active=False)
        self.assertEqual(dl.load_candidates(self.conn), [])
        self.assertEqual(len(dl.load_candidates(self.conn, active_only=False)), 1)


class Documents(Base):
    def test_document_linked_to_licence_and_verified(self):
        cid = dl.register_candidate(self.conn, "a@example.com", "Ann", email_verified=True)
        doc = dl.add_document(self.conn, cid, "licence", "s11.pdf", "uploads/a/s11.pdf", mime_type="application/pdf", size_bytes=1200)
        dl.set_candidate_licence(self.conn, cid, "Standard 11", "2028-01-01", document_id=doc)
        dl.set_candidate_licence(self.conn, cid, "Standard 11", "2029-01-01")  # renewal keeps the link
        link = self.conn.execute("SELECT document_id FROM candidate_licences").fetchone()[0]
        self.assertEqual(link, doc)
        dl.verify_document(self.conn, doc)
        self.assertIsNotNone(dl.list_documents(self.conn, cid)[0]["verified_at"])

    def test_cannot_link_another_candidates_document(self):
        a = dl.register_candidate(self.conn, "a@example.com", "Ann", email_verified=True)
        b = dl.register_candidate(self.conn, "b@example.com", "Bob", email_verified=True)
        doc = dl.add_document(self.conn, a, "licence", "s11.pdf", "uploads/a/s11.pdf")
        with self.assertRaises(dl.DataError):
            dl.set_candidate_licence(self.conn, b, "Standard 11", document_id=doc)

    def test_duplicate_storage_key_rejected(self):
        a = dl.register_candidate(self.conn, "a@example.com", "Ann", email_verified=True)
        dl.add_document(self.conn, a, "resume", "cv.pdf", "uploads/cv.pdf")
        with self.assertRaises(dl.DuplicateError):
            dl.add_document(self.conn, a, "resume", "cv2.pdf", "uploads/cv.pdf")

    def test_delete_candidate_cascades_and_returns_file_keys(self):
        employer, job_id, ids = self.load_everything()
        cid = ids["cand-a"]
        dl.add_document(self.conn, cid, "resume", "cv.pdf", "uploads/a/cv.pdf")
        sl = dl.create_shortlist(self.conn, employer, "Shortlist", job_id)
        dl.add_to_shortlist(self.conn, sl, cid, as_of=AS_OF)
        self.assertEqual(dl.delete_candidate(self.conn, cid), ["uploads/a/cv.pdf"])
        for table, col in [("candidates", "id"), ("documents", "candidate_id"),
                           ("candidate_licences", "candidate_id"), ("candidate_experience", "candidate_id"),
                           ("shortlist_entries", "candidate_id")]:
            n = self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (cid,)).fetchone()[0]
            self.assertEqual(n, 0, table)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM users WHERE email = 'cand-a@example.com'").fetchone())
        with self.assertRaises(dl.NotFoundError):
            dl.delete_candidate(self.conn, cid)


class Jobs(Base):
    @staticmethod
    def _canon(job):
        """Comparable form: sorted lists, skills as (name, min_years) pairs."""
        out = {}
        for key in ("required_licences", "preferred_licences", "required_qualifications",
                    "preferred_qualifications", "relevant_roles"):
            out[key] = sorted(job.get(key) or [])
        for key in ("required_skills", "preferred_skills"):
            out[key] = sorted(
                (s["name"], s.get("min_years")) if isinstance(s, dict) else (s, None)
                for s in job.get(key) or []
            )
        for key in ("title", "location", "start_date", "min_experience_years"):
            out[key] = job.get(key)
        return out

    def test_job_round_trip(self):
        employer = dl.register_employer(self.conn, "hr@mine.example", "Mine Co", email_verified=True)
        job_id = dl.create_job(self.conn, employer, FIXTURES["job"])
        self.assertEqual(self._canon(dl.get_job(self.conn, job_id)), self._canon(FIXTURES["job"]))
        self.assertEqual(dl.get_job(self.conn, job_id)["status"], "draft")

    def test_required_wins_when_listed_twice(self):
        employer = dl.register_employer(self.conn, "hr@mine.example", "Mine Co", email_verified=True)
        job_id = dl.create_job(
            self.conn, employer,
            {"title": "X", "required_licences": ["Standard 11"], "preferred_licences": ["SS11", "Standard 11"]},
        )
        job = dl.get_job(self.conn, job_id)
        self.assertEqual(job["required_licences"], ["Standard 11"])
        self.assertEqual(job["preferred_licences"], [])

    def test_update_replaces_content(self):
        employer = dl.register_employer(self.conn, "hr@mine.example", "Mine Co", email_verified=True)
        job_id = dl.create_job(self.conn, employer, FIXTURES["job"])
        dl.update_job(self.conn, job_id, {"title": "Dozer Operator", "required_skills": [{"name": "Dozer operation", "min_years": 2}]})
        job = dl.get_job(self.conn, job_id)
        self.assertEqual(job["title"], "Dozer Operator")
        self.assertEqual(job["required_licences"], [])
        self.assertEqual(job["required_skills"], [{"name": "Dozer operation", "min_years": 2.0}])

    def test_status_and_filters(self):
        employer = dl.register_employer(self.conn, "hr@mine.example", "Mine Co", email_verified=True)
        other = dl.register_employer(self.conn, "hr@other.example", "Other Co", email_verified=True)
        a = dl.create_job(self.conn, employer, {"title": "A"}, status="open")
        dl.create_job(self.conn, employer, {"title": "B"})
        dl.create_job(self.conn, other, {"title": "C"}, status="open")
        self.assertEqual({j["title"] for j in dl.load_jobs(self.conn, status="open")}, {"A", "C"})
        self.assertEqual({j["title"] for j in dl.load_jobs(self.conn, employer_id=employer)}, {"A", "B"})
        dl.set_job_status(self.conn, a, "filled")
        self.assertEqual({j["title"] for j in dl.load_jobs(self.conn, status="open")}, {"C"})
        with self.assertRaises(ValueError):
            dl.set_job_status(self.conn, a, "nonsense")
        with self.assertRaises(dl.NotFoundError):
            dl.get_job(self.conn, "missing")

    def test_validation(self):
        employer = dl.register_employer(self.conn, "hr@mine.example", "Mine Co", email_verified=True)
        with self.assertRaises(ValueError):
            dl.create_job(self.conn, employer, {"title": ""})
        with self.assertRaises(ValueError):  # min_years only makes sense for skills
            dl.create_job(self.conn, employer, {"title": "X", "required_licences": [{"name": "S11", "min_years": 2}]})
        with self.assertRaises(dl.NotFoundError):
            dl.create_job(self.conn, "missing", {"title": "X"})


class MatchingIntegration(Base):
    """DB -> engine: the stored fixtures must score exactly as the engine suites expect."""

    def test_rank_candidates_for_job_matches_shared_fixtures(self):
        _, job_id, ids = self.load_everything()
        ranked = dl.rank_candidates_for_job(self.conn, job_id, as_of=AS_OF)
        by_fixture = {v: k for k, v in ids.items()}
        self.assertEqual([by_fixture[r["candidate_id"]] for r in ranked], FIXTURES["expected_order"])
        for r in ranked:
            exp = FIXTURES["expected"][by_fixture[r["candidate_id"]]]
            self.assertEqual(r["score"], exp["score"])
            self.assertEqual(r["eligible"], exp["eligible"])
            self.assertEqual(r["breakdown"], exp["breakdown"])
            self.assertEqual(r["blockers"], exp["blockers"])
            self.assertEqual(r["warnings"], exp["warnings"])
        self.assertEqual(ranked[0]["candidate_name"], "Alex Strong")

    def test_options_pass_through(self):
        _, job_id, _ = self.load_everything()
        top = dl.rank_candidates_for_job(self.conn, job_id, as_of=AS_OF, eligible_only=True, limit=1)
        self.assertEqual([r["candidate_name"] for r in top], ["Alex Strong"])

    def test_inactive_candidates_are_not_ranked(self):
        _, job_id, ids = self.load_everything()
        dl.update_candidate(self.conn, ids["cand-a"], is_active=False)
        ranked = dl.rank_candidates_for_job(self.conn, job_id, as_of=AS_OF)
        self.assertNotIn("Alex Strong", [r["candidate_name"] for r in ranked])

    def test_search_candidates_with_ad_hoc_criteria(self):
        self.load_everything()
        found = dl.search_candidates(
            self.conn, {"required_licences": ["Working at Heights"]}, as_of=AS_OF, eligible_only=True
        )
        self.assertEqual({r["candidate_name"] for r in found}, {"Alex Strong", "Casey Expiring", "Drew NoCert"})

    def test_rank_jobs_for_candidate_only_considers_open_jobs(self):
        employer, _, ids = self.load_everything()
        dl.create_job(self.conn, employer, FIXTURES["job2"], status="open")
        dl.create_job(self.conn, employer, {"title": "Draft job", "required_licences": ["Standard 11"]})
        ranked = dl.rank_jobs_for_candidate(self.conn, ids["cand-a"], as_of=AS_OF)
        self.assertEqual([r["job_title"] for r in ranked], ["Dragline Operator", "Maintenance Fitter"])
        self.assertEqual(ranked[1]["score"], 40.0)
        with self.assertRaises(dl.NotFoundError):
            dl.rank_jobs_for_candidate(self.conn, "missing")


class Shortlists(Base):
    def test_score_snapshot_status_and_ordering(self):
        employer, job_id, ids = self.load_everything()
        sl = dl.create_shortlist(self.conn, employer, "Dragline shortlist", job_id)
        dl.add_to_shortlist(self.conn, sl, ids["cand-b"], as_of=AS_OF)
        dl.add_to_shortlist(self.conn, sl, ids["cand-a"], notes="Strong", as_of=AS_OF)
        dl.set_shortlist_status(self.conn, sl, ids["cand-a"], "interviewing")
        dl.add_to_shortlist(self.conn, sl, ids["cand-a"], notes="Strong, call Monday", as_of=AS_OF)  # re-add
        result = dl.get_shortlist(self.conn, sl)
        self.assertEqual([e["name"] for e in result["entries"]], ["Alex Strong", "Blake Lapsed"])
        a, b = result["entries"]
        self.assertEqual((a["score_at_add"], b["score_at_add"]), (100.0, 51.8))
        self.assertEqual((a["status"], a["notes"]), ("interviewing", "Strong, call Monday"))
        self.assertEqual(dl.list_shortlists(self.conn, employer)[0]["entries"], 2)
        self.assertTrue(dl.remove_from_shortlist(self.conn, sl, ids["cand-b"]))
        self.assertFalse(dl.remove_from_shortlist(self.conn, sl, ids["cand-b"]))

    def test_shortlist_without_job_stores_given_score(self):
        employer, _, ids = self.load_everything()
        sl = dl.create_shortlist(self.conn, employer, "General")
        dl.add_to_shortlist(self.conn, sl, ids["cand-c"])
        dl.add_to_shortlist(self.conn, sl, ids["cand-d"], score=80.5)
        scores = [e["score_at_add"] for e in dl.get_shortlist(self.conn, sl)["entries"]]
        self.assertEqual(scores, [80.5, None])

    def test_cannot_use_another_employers_job(self):
        _, job_id, _ = self.load_everything()
        other = dl.register_employer(self.conn, "hr@other.example", "Other Co", email_verified=True)
        with self.assertRaises(dl.DataError):
            dl.create_shortlist(self.conn, other, "Nope", job_id)

    def test_validation(self):
        employer, _, ids = self.load_everything()
        sl = dl.create_shortlist(self.conn, employer, "S")
        with self.assertRaises(ValueError):
            dl.set_shortlist_status(self.conn, sl, ids["cand-a"], "hired")
        with self.assertRaises(dl.NotFoundError):
            dl.set_shortlist_status(self.conn, sl, ids["cand-a"], "contacted")  # not on the list yet
        with self.assertRaises(dl.NotFoundError):
            dl.add_to_shortlist(self.conn, "missing", ids["cand-a"])


if __name__ == "__main__":
    unittest.main()
