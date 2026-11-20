"""Tests for matching_engine. Run: python -m unittest -v   (from the python/ folder)"""
import copy
import json
import unittest
from pathlib import Path

import matching_engine as me

FIXTURES = json.loads(
    (Path(__file__).resolve().parent.parent / "fixtures" / "match_cases.json").read_text()
)
JOB = FIXTURES["job"]
JOB2 = FIXTURES["job2"]
AS_OF = FIXTURES["as_of"]
CANDS = {c["id"]: c for c in FIXTURES["candidates"]}


def score(cid, job=JOB, **kw):
    return me.score_candidate(CANDS[cid], job, as_of=AS_OF, **kw)


class SharedFixtureParity(unittest.TestCase):
    """Same expectations as the JS suite - keeps both engines identical."""

    def test_each_candidate_matches_expected(self):
        for cid, exp in FIXTURES["expected"].items():
            with self.subTest(candidate=cid):
                r = score(cid)
                self.assertEqual(r.score, exp["score"])
                self.assertEqual(r.eligible, exp["eligible"])
                self.assertEqual(r.breakdown, exp["breakdown"])
                self.assertEqual(r.blockers, exp["blockers"])
                self.assertEqual(r.gaps, exp["gaps"])
                self.assertEqual(r.warnings, exp["warnings"])

    def test_ranking_order(self):
        ranked = me.rank_candidates(FIXTURES["candidates"], JOB, as_of=AS_OF)
        self.assertEqual([r.candidate_id for r in ranked], FIXTURES["expected_order"])


class Licences(unittest.TestCase):
    def test_alias_and_spelling_normalised(self):
        self.assertEqual(me.normalize("SS11"), "standard 11")
        self.assertEqual(me.normalize("  Heavy   Rigid License "), "heavy rigid licence")

    def test_expiry_on_start_date_is_still_valid(self):
        cand = {"licences": [{"type": "Standard 11", "expiry": "2026-12-01"}]}
        job = {"start_date": "2026-12-01", "required_licences": ["Standard 11"]}
        r = me.score_candidate(cand, job)
        self.assertTrue(r.eligible)
        self.assertEqual(r.warnings, ["Standard 11 expires 2026-12-01"])

    def test_expiry_day_before_start_is_blocker(self):
        cand = {"licences": [{"type": "Standard 11", "expiry": "2026-11-30"}]}
        job = {"start_date": "2026-12-01", "required_licences": ["Standard 11"]}
        r = me.score_candidate(cand, job)
        self.assertFalse(r.eligible)
        self.assertEqual(r.blockers, ["Standard 11: expired 2026-11-30"])

    def test_latest_expiry_wins_when_held_twice(self):
        cand = {
            "licences": [
                {"type": "Standard 11", "expiry": "2020-01-01"},
                {"type": "Standard 11", "expiry": "2030-01-01"},
            ]
        }
        job = {"start_date": "2026-12-01", "required_licences": ["Standard 11"]}
        self.assertTrue(me.score_candidate(cand, job).eligible)

    def test_custom_alias(self):
        me.register_aliases({"cert iii se": "cert iii surface extraction"})
        cand = {"qualifications": ["Cert III SE"]}
        job = {"required_qualifications": ["Cert III Surface Extraction"]}
        self.assertTrue(me.score_candidate(cand, job, as_of=AS_OF).eligible)


class Weights(unittest.TestCase):
    def test_custom_weights_licences_only(self):
        r = score("cand-b", weights={"qualifications": 0, "skills": 0, "experience": 0, "location": 0})
        self.assertEqual(r.score, 40.0)

    def test_component_skipped_when_job_has_no_requirement(self):
        job = copy.deepcopy(JOB)
        del job["location"]
        r = score("cand-d", job=job)
        self.assertIsNone(r.breakdown["location"])
        self.assertEqual(r.score, 78.9)  # 0.75 / 0.95 renormalised

    def test_invalid_weights_rejected(self):
        with self.assertRaises(ValueError):
            score("cand-a", weights={"licences": -1})
        with self.assertRaises(ValueError):
            score("cand-a", weights={"salary": 1})

    def test_job_with_no_requirements(self):
        r = me.score_candidate(CANDS["cand-a"], {"id": "empty"}, as_of=AS_OF)
        self.assertEqual(r.score, 0.0)
        self.assertTrue(r.eligible)
        self.assertEqual(r.warnings, ["job has no scorable requirements"])


class Ranking(unittest.TestCase):
    def test_eligible_only(self):
        ranked = me.rank_candidates(FIXTURES["candidates"], JOB, as_of=AS_OF, eligible_only=True)
        self.assertEqual([r.candidate_id for r in ranked], ["cand-a", "cand-c"])

    def test_min_score_and_limit(self):
        ranked = me.rank_candidates(FIXTURES["candidates"], JOB, as_of=AS_OF, min_score=80)
        self.assertEqual([r.candidate_id for r in ranked], ["cand-a", "cand-c"])
        ranked = me.rank_candidates(FIXTURES["candidates"], JOB, as_of=AS_OF, limit=1)
        self.assertEqual([r.candidate_id for r in ranked], ["cand-a"])

    def test_ineligible_ranks_below_eligible_even_with_higher_score(self):
        # Skills-only weighting: d scores 100 (but lacks a required cert), c scores 66.7.
        only_skills = {"licences": 0, "qualifications": 0, "experience": 0, "location": 0}
        ranked = me.rank_candidates(
            [CANDS["cand-d"], CANDS["cand-c"]], JOB, weights=only_skills, as_of=AS_OF
        )
        self.assertEqual([r.score for r in ranked], [66.7, 100.0])
        self.assertEqual([r.candidate_id for r in ranked], ["cand-c", "cand-d"])

    def test_jobs_for_candidate(self):
        ranked = me.match_jobs_for_candidate(CANDS["cand-a"], [JOB2, JOB], as_of=AS_OF)
        self.assertEqual([r.job_id for r in ranked], ["job-1", "job-2"])
        self.assertEqual(ranked[1].score, 40.0)
        self.assertFalse(ranked[1].eligible)

    def test_to_dict_is_json_serialisable(self):
        json.dumps(score("cand-b").to_dict())


if __name__ == "__main__":
    unittest.main()
