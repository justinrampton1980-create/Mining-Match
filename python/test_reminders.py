"""Tests for licence expiry reminders. Run: python -m unittest -v"""
import inspect
import re
import unittest
from datetime import date, timedelta

import access_control as ac
import auth
import data_layer as dl
import reminders
from test_access_control import World

URL = "https://miningmatch.example"
TODAY = date(2026, 11, 20)


def day(n):
    return TODAY + timedelta(days=n)


class Stages(unittest.TestCase):
    def test_stage_for_days_left(self):
        cases = {91: None, 90: 90, 61: 90, 60: 60, 31: 60, 30: 30, 1: 30, 0: 30, -1: 0, -14: 0, -15: None}
        for days_left, stage in cases.items():
            with self.subTest(days_left=days_left):
                self.assertEqual(reminders.stage_for(days_left), stage)


class Reminders(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.conn = self.w.conn
        self.out = auth.Outbox()

    def tearDown(self):
        self.conn.close()

    def licence(self, name, expiry, candidate=None):
        dl.set_candidate_licence(self.conn, candidate or self.w.c1, name, expiry.isoformat())

    def run_on(self, today, mailer=None):
        return reminders.send_expiry_reminders(self.conn, mailer or self.out, URL, today=today)

    def subjects(self):
        return [m["subject"] for m in self.out.messages]

    def test_each_stage_once_over_time(self):
        self.licence("Coal Board Medical", day(95))
        for n in range(0, 112):  # the daily job, every day for 16 weeks
            self.run_on(day(n))
        self.assertEqual(self.subjects(), [
            "Your Coal Board Medical expires in 90 days",
            "Your Coal Board Medical expires in 60 days",
            "Your Coal Board Medical expires in 30 days",
            "Your Coal Board Medical has expired",
        ])
        self.assertEqual({m["to"] for m in self.out.messages}, {"jordan@example.com"})

    def test_missed_days_catch_up_with_one_email(self):
        self.licence("Coal Board Medical", day(45))  # added late, or the job didn't run
        self.assertEqual(self.run_on(TODAY), {"emails": 1, "licences": 1, "failed": 0})
        self.assertEqual(self.run_on(TODAY)["emails"], 0)  # running twice in a day is harmless
        self.assertEqual(self.run_on(day(14))["emails"], 0)  # 31 days left: nothing new
        self.run_on(day(15))  # 30 days left
        self.assertEqual(self.subjects(), ["Your Coal Board Medical expires in 45 days",
                                           "Your Coal Board Medical expires in 30 days"])

    def test_one_email_lists_every_licence_due(self):
        self.licence("Coal Board Medical", day(30))
        self.licence("Dogging (DG)", day(12))
        self.run_on(TODAY)
        (msg,) = self.out.messages
        self.assertEqual(msg["subject"], "2 of your licences expire soon")
        self.assertIn("Coal Board Medical: expires 20 Dec 2026 (in 30 days)", msg["body"])
        self.assertIn("Dogging (DG): expires 2 Dec 2026 (in 12 days)", msg["body"])
        self.assertTrue(msg["body"].startswith("Hello Jordan,"))
        self.assertIn(f"{URL}/#/profile", msg["body"])
        self.assertIn("Employers can't count an expired licence", msg["body"])

    def test_expires_today_and_expired_wording(self):
        self.licence("Coal Board Medical", TODAY)
        self.run_on(TODAY)
        self.assertEqual(self.subjects()[-1], "Your Coal Board Medical expires today")
        self.licence("Coal Board Medical", day(-3))
        self.licence("First Aid (HLTAID011)", day(10))
        self.run_on(TODAY)
        self.assertEqual(self.subjects()[-1], "2 of your licences need attention")
        self.assertIn("Coal Board Medical: expired 17 Nov 2026", self.out.messages[-1]["body"])

    def test_renewing_starts_afresh(self):
        self.licence("Coal Board Medical", day(20))
        self.run_on(TODAY)
        renewed = day(20 + 5 * 365)
        self.licence("Coal Board Medical", renewed)  # renewal: new expiry date
        self.assertEqual(self.run_on(day(1))["emails"], 0)
        self.run_on(renewed - timedelta(days=30))
        self.assertEqual(self.subjects()[-1], "Your Coal Board Medical expires in 30 days")

    def test_correcting_an_expiry_date_sends_a_fresh_reminder(self):
        self.licence("Coal Board Medical", day(25))
        self.run_on(TODAY)
        self.licence("Coal Board Medical", day(10))  # they had the date wrong: it's sooner than that
        self.run_on(TODAY)
        self.assertEqual(self.subjects(), ["Your Coal Board Medical expires in 25 days",
                                           "Your Coal Board Medical expires in 10 days"])

    def test_old_expiries_are_not_announced(self):
        self.licence("Coal Board Medical", day(-15))
        self.assertEqual(self.run_on(TODAY)["emails"], 0)

    def test_unsubscribe_link_and_turning_back_on(self):
        self.licence("Coal Board Medical", day(60))
        self.run_on(TODAY)
        token = re.search(r"#/unsubscribe\?token=(\S+)", self.out.messages[0]["body"]).group(1)
        reminders.unsubscribe(self.conn, token)
        self.assertFalse(ac.view_candidate(self.conn, self.w.p["c1"], self.w.c1)["expiry_reminders"])
        self.assertEqual(self.run_on(day(30))["emails"], 0)
        for bad in ("", "short", "x" * 40, token + "x"):
            with self.subTest(token=bad[:10]), self.assertRaises(ValueError):
                reminders.unsubscribe(self.conn, bad)
        ac.update_candidate(self.conn, self.w.p["c1"], self.w.c1, expiry_reminders=True)
        self.run_on(day(31))  # 29 days left: the 30-day reminder not yet sent
        self.assertEqual(self.subjects()[-1], "Your Coal Board Medical expires in 29 days")
        self.assertIn(token, self.out.messages[-1]["body"])  # same link keeps working

    def test_each_candidate_gets_their_own_unsubscribe_link(self):
        self.licence("Coal Board Medical", day(30))
        self.licence("Coal Board Medical", day(30), candidate=self.w.c2)
        self.run_on(TODAY)
        self.assertEqual(len(self.out.messages), 2)
        tokens = [re.search(r"#/unsubscribe\?token=(\S+)", m["body"]).group(1) for m in self.out.messages]
        self.assertEqual(len(set(tokens)), 2, "one token for everyone would let anyone silence anyone")
        self.assertTrue(all(len(t) >= 20 for t in tokens), tokens)
        reminders.unsubscribe(self.conn, tokens[0])  # one person opting out doesn't touch the other
        off = self.out.messages[0]["to"]
        still_on = {c: bool(ac.view_candidate(self.conn, self.w.p[c], getattr(self.w, c))["expiry_reminders"])
                    for c in ("c1", "c2")}
        self.assertEqual(sum(still_on.values()), 1, f"{off} opted out; the other candidate should still be on")

    def test_who_gets_reminders(self):
        self.licence("Coal Board Medical", day(30))
        self.licence("Coal Board Medical", day(30), candidate=self.w.c2)
        ac.update_candidate(self.conn, self.w.p["c1"], self.w.c1, is_active=False)  # hidden from employers: still reminded
        ac.set_user_active(self.conn, self.w.p["admin"], self.w.users["c2"], False)  # suspended: not reminded
        new = dl.register_candidate(self.conn, "new@example.com", "New Person")  # email not confirmed
        dl.set_candidate_licence(self.conn, new, "Coal Board Medical", day(30).isoformat())
        self.run_on(TODAY)
        self.assertEqual([m["to"] for m in self.out.messages], ["jordan@example.com"])

    def test_failed_email_is_tried_again(self):
        class Broken:
            def send(self, *a, **kw):
                raise OSError("smtp down")
        self.licence("Coal Board Medical", day(30))
        self.assertEqual(self.run_on(TODAY, Broken()), {"emails": 0, "licences": 0, "failed": 1})
        self.assertEqual(self.run_on(day(1))["emails"], 1)

    def test_deleting_the_candidate_removes_their_reminder_log(self):
        self.licence("Coal Board Medical", day(30))
        self.run_on(TODAY)
        ac.delete_candidate(self.conn, self.w.p["c1"], self.w.c1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM licence_reminders").fetchone()[0], 0)

    def test_admin_log_and_audit(self):
        self.licence("Coal Board Medical", day(30))
        result = reminders.send_expiry_reminders(self.conn, self.out, URL, self.w.p["admin"], today=TODAY)
        self.assertEqual(result["emails"], 1)
        (row,) = reminders.list_recent_reminders(self.conn, self.w.p["admin"])
        self.assertEqual((row["candidate_name"], row["licence"], row["stage"]), ("Jordan Private", "Coal Board Medical", 30))
        detail = self.conn.execute("SELECT detail FROM audit_log WHERE action = 'reminders.run'").fetchone()[0]
        self.assertEqual(detail, "1 emails, 1 licences")

    def test_licence_rules_from_the_seed(self):
        dl.seed_catalog(self.conn)
        rules = {r["name"]: r for r in reminders.licence_rules(self.conn, self.w.p["c1"])}
        self.assertEqual(rules["Dogging (DG)"]["renew_years"], 5)
        self.assertEqual(rules["Standard 11"]["renew_years"], 5)
        self.assertIsNone(rules["White Card"]["renew_years"])
        self.assertIn("Doesn't expire", rules["White Card"]["note"])


GRID = [
    ("send_expiry_reminders", lambda w, p: reminders.send_expiry_reminders(w.conn, auth.Outbox(), URL, p, today=TODAY), {"admin"}),
    ("list_recent_reminders", lambda w, p: reminders.list_recent_reminders(w.conn, p), {"admin"}),
    ("licence_rules", lambda w, p: reminders.licence_rules(w.conn, p), {"c1", "c2", "e1", "e2", "admin"}),
]


class ReminderPermissions(unittest.TestCase):
    def test_every_caller_against_every_function(self):
        for name, call, allowed in GRID:
            for who in ("c1", "c2", "e1", "e2", "admin"):
                with self.subTest(function=name, caller=who):
                    w = World()
                    try:
                        if who in allowed:
                            call(w, w.p[who])
                        else:
                            with self.assertRaises(ac.AccessDenied):
                                call(w, w.p[who])
                    finally:
                        w.conn.close()

    def test_grid_covers_every_guarded_function(self):
        guarded = {name for name, fn in inspect.getmembers(reminders, inspect.isfunction)
                   if fn.__module__ == "reminders" and "principal" in inspect.signature(fn).parameters}
        self.assertEqual(guarded, {row[0] for row in GRID})


if __name__ == "__main__":
    unittest.main()
