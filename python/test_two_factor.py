"""Two-factor login for admins (launch blocker B3)."""
import base64
import io
import os
import sqlite3
import time
import unittest
from unittest import mock

import access_control as ac
import auth
import data_layer as dl
import two_factor
from test_api import PASSWORD, ApiTest


class Codes(unittest.TestCase):
    SECRET = base64.b32encode(b"12345678901234567890").decode()  # RFC 6238 test key

    def test_rfc_6238_test_vectors(self):
        # RFC 6238 appendix B (SHA-1), last 6 of the 8 digits.
        for t, expected in ((59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
                            (1234567890, "005924"), (2000000000, "279037"), (20000000000, "353130")):
            self.assertEqual(two_factor.code_at(self.SECRET, t // 30), expected, t)

    def test_codes_a_step_either_side_are_accepted_once(self):
        now = 1_700_000_000
        with mock.patch.object(two_factor, "clock", lambda: now):
            step = now // 30
            for offset in (-1, 0, 1):
                self.assertEqual(two_factor.matching_step(self.SECRET, two_factor.code_at(self.SECRET, step + offset), None),
                                 step + offset)
            for offset in (-2, 2):
                self.assertIsNone(two_factor.matching_step(self.SECRET, two_factor.code_at(self.SECRET, step + offset), None))
            code = two_factor.code_at(self.SECRET, step)
            self.assertIsNone(two_factor.matching_step(self.SECRET, code, step))  # already used
            self.assertIsNone(two_factor.matching_step(self.SECRET, two_factor.code_at(self.SECRET, step - 1), step))
            self.assertEqual(two_factor.matching_step(self.SECRET, f"{code[:3]} {code[3:]}", None), step)  # spaces ok
            for junk in ("", "12345", "abcdef", None, "1234567"):
                self.assertIsNone(two_factor.matching_step(self.SECRET, junk, None))

    def test_digits_of_other_scripts_are_a_wrong_code_not_a_crash(self):
        # str.isdigit() accepts fullwidth and other scripts' digits, and hmac.compare_digest raises on
        # non-ASCII text: that used to be a 500 error and an attempt that wasn't counted.
        for junk in ("１２３４５６", "١٢٣٤٥٦", "123４56"):
            self.assertIsNone(two_factor.matching_step(self.SECRET, junk, None), junk)

    def test_secrets_and_setup_links(self):
        a, b = two_factor.new_secret(), two_factor.new_secret()
        self.assertNotEqual(a, b)
        self.assertEqual(len(base64.b32decode(a + "=" * (-len(a) % 8))), 20)
        uri = two_factor.otpauth_uri("boss@example.com", a)
        self.assertTrue(uri.startswith("otpauth://totp/Mining%20Match%3Aboss%40example.com?secret="))
        self.assertIn("issuer=Mining%20Match", uri)
        svg = two_factor.qr_svg(uri)
        self.assertTrue(svg is None or svg.lstrip().startswith("<svg"))


class AdminLogin(ApiTest):
    def raw_login(self, email, password=PASSWORD, **extra):
        r = self.client.post("/auth/login", json={"email": email, "password": password, **extra})
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def invite_admin(self, email="second@example.com"):
        r = self.client.post("/admin/admins", json={"email": email}, headers=self.admin)
        self.assertIn(r.status_code, (200, 201), r.text)
        token = self.token_from_email(email, "reset-password")
        r = self.client.post("/auth/password-reset/confirm", json={"token": token, "new_password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)

    def user_id(self, email):
        conn = dl.connect(self.db)
        try:
            return conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        finally:
            conn.close()

    # ------------------------------------------------------------ first login: setup
    def test_a_new_admin_must_set_up_two_factor_before_getting_in(self):
        self.invite_admin()
        r = self.raw_login("second@example.com", use_cookie=True)
        started = r.json()
        self.assertEqual(started["two_factor"], "setup")
        self.assertNotIn("token", started)
        self.assertNotIn("set-cookie", r.headers)  # no session yet

        info = self.client.post("/auth/two-factor/setup", json={"challenge": started["challenge"]}).json()
        self.assertRegex(info["secret"], r"^[A-Z2-7]{4}( [A-Z2-7]{4})+$")
        self.assertIn("second%40example.com", info["otpauth_uri"])
        self.assertTrue(info["qr_svg"].lstrip().startswith("<svg"))

        wrong = self.client.post("/auth/two-factor/setup/confirm", json={"challenge": started["challenge"], "code": "000000"})
        self.assertEqual(wrong.status_code, 400)
        self.assertIn("isn't right", wrong.json()["detail"])

        self.totp_secrets["second@example.com"] = info["secret"].replace(" ", "")
        code = self.totp("second@example.com")
        with mock.patch.object(two_factor, "clock", lambda: self.totp_now):
            r = self.client.post("/auth/two-factor/setup/confirm",
                                 json={"challenge": started["challenge"], "code": code, "use_cookie": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["recovery_codes"]), 10)
        self.assertRegex(r.json()["recovery_codes"][0], r"^[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}$")
        self.assertIn("mm_session", r.headers["set-cookie"])
        me = self.client.get("/me", headers={"X-Requested-With": "mining-match"})
        self.assertEqual(me.json()["role"], "admin")
        # The setup challenge is used up.
        again = self.client.post("/auth/two-factor/setup", json={"challenge": started["challenge"]})
        self.assertEqual(again.status_code, 400)

    def test_setup_needs_the_secret_first(self):
        self.invite_admin()
        started = self.raw_login("second@example.com").json()
        r = self.client.post("/auth/two-factor/setup/confirm", json={"challenge": started["challenge"], "code": "123456"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("set up your authenticator app first", r.json()["detail"])

    # ------------------------------------------------------------ normal login
    def test_admin_login_needs_a_fresh_code_every_time(self):
        started = self.raw_login("admin@example.com").json()
        self.assertEqual(started["two_factor"], "required")
        self.assertNotIn("token", started)
        r = self.client.post("/auth/login/two-factor", json={"challenge": started["challenge"], "code": "000000"})
        self.assertEqual(r.status_code, 400)
        code = self.totp("admin@example.com")
        with mock.patch.object(two_factor, "clock", lambda: self.totp_now):
            r = self.client.post("/auth/login/two-factor", json={"challenge": started["challenge"], "code": code})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["role"], "admin")
            # Someone who saw that code can't use it again, even with the password.
            again = self.raw_login("admin@example.com").json()
            replay = self.client.post("/auth/login/two-factor", json={"challenge": again["challenge"], "code": code})
        self.assertEqual(replay.status_code, 400)
        self.assertIn("used already", replay.json()["detail"])

    def test_candidates_and_employers_log_in_as_before(self):
        self.candidate()
        self.employer()
        for email in ("jordan@example.com", "hr@alpha.example"):
            self.assertIn("token", self.raw_login(email).json())

    def test_a_challenge_is_only_for_its_own_step(self):
        started = self.raw_login("admin@example.com").json()
        r = self.client.post("/auth/two-factor/setup", json={"challenge": started["challenge"]})
        self.assertEqual(r.status_code, 400)  # 'required' challenge can't start a new setup
        r = self.client.post("/auth/login/two-factor", json={"challenge": "x" * 43, "code": "123456"})
        self.assertEqual(r.status_code, 400)

    def test_challenges_expire_and_allow_five_wrong_codes(self):
        started = self.raw_login("admin@example.com").json()
        real = two_factor._now()
        with mock.patch.object(two_factor, "_now", lambda: real + two_factor.CHALLENGE_SECONDS + 1):
            r = self.two_factor_step("admin@example.com", started)
        self.assertEqual(r.status_code, 400)
        self.assertIn("expired", r.json()["detail"])

        started = self.raw_login("admin@example.com").json()
        for _ in range(two_factor.CHALLENGE_TRIES):
            self.client.post("/auth/login/two-factor", json={"challenge": started["challenge"], "code": "000000"})
        r = self.two_factor_step("admin@example.com", started)  # the right code, too late
        self.assertEqual(r.status_code, 400)

    def test_wrong_codes_are_limited_per_account(self):
        limit, _ = two_factor.WRONG_CODES_PER_USER
        for _ in range(limit // two_factor.CHALLENGE_TRIES):
            started = self.raw_login("admin@example.com").json()
            for _ in range(two_factor.CHALLENGE_TRIES):
                self.client.post("/auth/login/two-factor", json={"challenge": started["challenge"], "code": "000000"})
        started = self.raw_login("admin@example.com").json()
        r = self.two_factor_step("admin@example.com", started)
        self.assertEqual(r.status_code, 429)

    # ------------------------------------------------------------ recovery codes
    def test_recovery_codes_work_once_each_and_can_be_replaced(self):
        h = self.admin
        status = self.client.get("/auth/two-factor", headers=h).json()
        self.assertEqual(status, {"enabled": True, "recovery_codes_left": 10})
        code = self.totp("admin@example.com")
        with mock.patch.object(two_factor, "clock", lambda: self.totp_now):
            codes = self.client.post("/auth/two-factor/recovery-codes", json={"code": code}, headers=h).json()["recovery_codes"]
        self.assertEqual(len(set(codes)), 10)

        started = self.raw_login("admin@example.com").json()
        r = self.two_factor_step("admin@example.com", started, code=codes[0].upper())  # any case
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["used_recovery_code"])
        self.assertEqual(r.json()["recovery_codes_left"], 9)
        started = self.raw_login("admin@example.com").json()
        self.assertEqual(self.two_factor_step("admin@example.com", started, code=codes[0]).status_code, 400)

        # Making new codes needs an app code, not a recovery code, and retires the old ones.
        r = self.client.post("/auth/two-factor/recovery-codes", json={"code": codes[1]}, headers=self.login("admin@example.com"))
        self.assertEqual(r.status_code, 400)
        h = self.login("admin@example.com")
        code = self.totp("admin@example.com")
        with mock.patch.object(two_factor, "clock", lambda: self.totp_now):
            fresh = self.client.post("/auth/two-factor/recovery-codes", json={"code": code}, headers=h).json()["recovery_codes"]
        self.assertTrue(set(fresh).isdisjoint(codes))
        started = self.raw_login("admin@example.com").json()
        self.assertEqual(self.two_factor_step("admin@example.com", started, code=codes[2]).status_code, 400)

    # ------------------------------------------------------------ sessions
    def test_an_admin_session_without_two_factor_is_refused(self):
        conn = dl.connect(self.db)
        try:  # an admin from before two-factor existed, with a session made the old way
            uid = ac.create_admin(conn, "old@example.com", auth.hash_password(PASSWORD), by=ac.principal_for_user(
                conn, self.user_id("admin@example.com")))
            conn.execute("UPDATE users SET email_verified_at = CURRENT_TIMESTAMP WHERE id = ?", (uid,))
            conn.commit()
            token = auth._start_session(conn, uid, "admin")["token"]
        finally:
            conn.close()
        self.assertEqual(self.client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code, 401)
        self.assertEqual(self.raw_login("old@example.com").json()["two_factor"], "setup")

    def test_turning_it_on_ends_older_sessions(self):
        conn = dl.connect(self.db)
        try:
            uid = self.user_id("admin@example.com")
            old = auth._start_session(conn, uid, "admin")["token"]
            self.assertEqual(self.client.get("/me", headers={"Authorization": f"Bearer {old}"}).status_code, 200)
            two_factor.enable(conn, uid, two_factor.new_secret(), 0)
        finally:
            conn.close()
        self.assertEqual(self.client.get("/me", headers={"Authorization": f"Bearer {old}"}).status_code, 401)

    def test_a_password_reset_by_email_still_needs_the_code(self):
        self.client.post("/auth/password-reset/request", json={"email": "admin@example.com"})
        token = self.token_from_email("admin@example.com", "reset-password")
        r = self.client.post("/auth/password-reset/confirm", json={"token": token, "new_password": "Another-Pass-2027"})
        self.assertEqual(r.status_code, 200, r.text)
        started = self.raw_login("admin@example.com", "Another-Pass-2027").json()
        self.assertEqual(started["two_factor"], "required")
        self.assertNotIn("token", started)

    def test_a_suspended_admin_cant_finish_logging_in(self):
        self.invite_admin()
        h2 = self.login("second@example.com")
        started = self.raw_login("admin@example.com").json()
        r = self.client.put(f"/admin/users/{self.user_id('admin@example.com')}/active", json={"active": False}, headers=h2)
        self.assertEqual(r.status_code, 200, r.text)
        r = self.two_factor_step("admin@example.com", started)
        self.assertEqual(r.status_code, 403)

    # ------------------------------------------------------------ lost phone
    def test_another_admin_can_reset_it_and_it_is_logged(self):
        self.invite_admin()
        h2 = self.login("second@example.com")
        second = self.user_id("second@example.com")
        self.assertEqual(self.client.post(f"/admin/users/{second}/reset-two-factor", headers=h2).status_code, 403)
        ch, _ = self.candidate()
        self.assertEqual(self.client.post(f"/admin/users/{second}/reset-two-factor", headers=ch).status_code, 403)
        self.assertEqual(self.client.post(f"/admin/users/{second}/reset-two-factor", headers=self.admin).status_code, 204)
        self.assertEqual(self.client.get("/me", headers=h2).status_code, 401)  # logged out everywhere
        self.assertEqual(self.raw_login("second@example.com").json()["two_factor"], "setup")
        log = self.client.get(f"/admin/audit-log?resource_id={second}", headers=self.admin).json()
        self.assertIn(("user.reset_two_factor", "allowed"), [(a["action"], a["outcome"]) for a in log])

    def test_the_only_admin_can_be_reset_from_the_server(self):
        with mock.patch.dict(os.environ, {"MM_DB": self.db}), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(two_factor._main(["two_factor.py", "reset", "admin@example.com"]), 0)
        self.assertIn("reset for admin@example.com", out.getvalue())
        self.assertEqual(self.client.get("/me", headers=self.admin).status_code, 401)
        self.assertEqual(self.raw_login("admin@example.com").json()["two_factor"], "setup")
        with mock.patch.dict(os.environ, {"MM_DB": self.db}), mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(two_factor._main(["two_factor.py", "reset", "jordan@nowhere.example"]), 1)

    # ------------------------------------------------------------ found in review
    def test_a_code_in_other_scripts_digits_is_refused_and_counted(self):
        started = self.raw_login("admin@example.com").json()
        r = self.client.post("/auth/login/two-factor", json={"challenge": started["challenge"], "code": "１２３４５６"})
        self.assertEqual(r.status_code, 400, r.text)  # not a 500
        conn = dl.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT tries FROM login_challenges").fetchone()[0], 1)
        finally:
            conn.close()

    def test_the_recovery_code_route_limits_wrong_codes_too(self):
        # Making new recovery codes needs a current app code. Without a limit, someone holding a stolen admin
        # session could guess it, and the new recovery codes would outlive that session.
        limit, _ = two_factor.WRONG_CODES_PER_USER
        for _ in range(limit):
            r = self.client.post("/auth/two-factor/recovery-codes", json={"code": "000000"}, headers=self.admin)
            self.assertEqual(r.status_code, 400)
        code = self.totp("admin@example.com")  # now even the right code gets no further
        with mock.patch.object(two_factor, "clock", lambda: self.totp_now):
            r = self.client.post("/auth/two-factor/recovery-codes", json={"code": code}, headers=self.admin)
        self.assertEqual(r.status_code, 429, r.text)

    def test_setup_confirmation_fails_if_two_factor_was_reset_meanwhile(self):
        # The setup challenge is read, the code is checked, then two-factor is turned on. If another admin's
        # reset lands in between, it must win: nothing turned on and no session started.
        self.invite_admin()
        started = self.raw_login("second@example.com").json()
        info = self.client.post("/auth/two-factor/setup", json={"challenge": started["challenge"]}).json()
        self.totp_secrets["second@example.com"] = info["secret"].replace(" ", "")
        code = self.totp("second@example.com")
        uid = self.user_id("second@example.com")
        real = two_factor.matching_step

        def reset_then_check(*args, **kwargs):
            conn = dl.connect(self.db)
            try:
                two_factor.reset(conn, uid)  # the other admin's reset, landing mid-request
            finally:
                conn.close()
            return real(*args, **kwargs)

        with mock.patch.object(two_factor, "clock", lambda: self.totp_now), \
                mock.patch.object(two_factor, "matching_step", reset_then_check):
            r = self.client.post("/auth/two-factor/setup/confirm", json={"challenge": started["challenge"], "code": code})
        self.assertEqual(r.status_code, 400, r.text)
        conn = dl.connect(self.db)
        try:
            self.assertFalse(two_factor.is_enabled(conn, uid))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (uid,)).fetchone()[0], 0)
        finally:
            conn.close()

    def test_turning_two_factor_on_and_making_the_session_are_one_transaction(self):
        # A reset or password change landing between "two-factor turned on" and "session made" would leave a live
        # session behind it. They are one transaction: while it runs, nobody else can write.
        self.invite_admin()
        started = self.raw_login("second@example.com").json()
        info = self.client.post("/auth/two-factor/setup", json={"challenge": started["challenge"]}).json()
        self.totp_secrets["second@example.com"] = info["secret"].replace(" ", "")
        code = self.totp("second@example.com")
        uid = self.user_id("second@example.com")
        real, seen = auth._insert_session, []

        def watch(conn, user_id, role):
            other = sqlite3.connect(self.db, timeout=0.2)
            try:
                other.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))  # what a reset would do
                other.commit()
                seen.append("let in")
            except sqlite3.OperationalError:
                seen.append("locked out")
            finally:
                other.close()
            return real(conn, user_id, role)

        with mock.patch.object(two_factor, "clock", lambda: self.totp_now), mock.patch.object(auth, "_insert_session", watch):
            r = self.client.post("/auth/two-factor/setup/confirm", json={"challenge": started["challenge"], "code": code})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, ["locked out"])

    def test_pending_login_steps_die_when_the_password_changes_or_is_reset(self):
        # A login started with the old password must not finish after the password changed: the holder of an
        # old challenge could otherwise still get a session (or, mid-setup, enrol their own authenticator).
        started = self.raw_login("admin@example.com").json()
        r = self.client.post("/auth/change-password", headers=self.admin,
                             json={"current_password": PASSWORD, "new_password": "Another-Pass-2027"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.two_factor_step("admin@example.com", started).status_code, 400)  # changed while logged in

        started = self.raw_login("admin@example.com", "Another-Pass-2027").json()
        self.client.post("/auth/password-reset/request", json={"email": "admin@example.com"})
        token = self.token_from_email("admin@example.com", "reset-password")
        r = self.client.post("/auth/password-reset/confirm", json={"token": token, "new_password": "Third-Pass-2028-x"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.two_factor_step("admin@example.com", started).status_code, 400)  # reset by email

        self.admin = self.login("admin@example.com", "Third-Pass-2028-x")  # the reset ended every session
        self.invite_admin()  # a first-login setup step is cut off the same way
        pending = self.raw_login("second@example.com").json()
        self.assertEqual(pending["two_factor"], "setup")
        self.client.post("/auth/password-reset/request", json={"email": "second@example.com"})
        token = self.token_from_email("second@example.com", "reset-password")
        r = self.client.post("/auth/password-reset/confirm", json={"token": token, "new_password": "Fresh-Pass-2029-x"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.post("/auth/two-factor/setup", json={"challenge": pending["challenge"]}).status_code, 400)

    # ------------------------------------------------ a password change landing in the middle of a login
    def reset_password_directly(self, token):
        """The reset from an emailed link, run on its own connection, as another request would."""
        conn = dl.connect(self.db)
        try:
            auth.reset_password(conn, token, "Fresh-Pass-2029-x")
        finally:
            conn.close()

    def count(self, table, uid):
        conn = dl.connect(self.db)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (uid,)).fetchone()[0]
        finally:
            conn.close()

    def test_the_second_step_gets_no_session_if_the_password_was_reset_after_the_code_was_checked(self):
        # The challenge is read, the code is checked, and only then is the challenge used up and the session made.
        # A reset landing in between has deleted the challenge: the right code must not still end in a session.
        self.client.post("/auth/password-reset/request", json={"email": "admin@example.com"})
        reset_token = self.token_from_email("admin@example.com", "reset-password")
        started = self.raw_login("admin@example.com").json()
        uid, real = self.user_id("admin@example.com"), two_factor.check_code

        def check_then_reset(*args, **kwargs):
            used = real(*args, **kwargs)  # the right code is accepted...
            self.reset_password_directly(reset_token)  # ...and then the reset lands
            return used

        with mock.patch.object(two_factor, "check_code", check_then_reset):
            r = self.two_factor_step("admin@example.com", started)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.count("sessions", uid), 0)

    def test_using_up_the_login_challenge_and_making_the_session_are_one_transaction(self):
        # Same as for setup: a reset or password change must not be able to land between the two.
        started = self.raw_login("admin@example.com").json()
        uid = self.user_id("admin@example.com")
        real, seen = auth._insert_session, []

        def watch(conn, user_id, role):
            other = sqlite3.connect(self.db, timeout=0.2)
            try:
                other.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))  # what a reset would do
                other.commit()
                seen.append("let in")
            except sqlite3.OperationalError:
                seen.append("locked out")
            finally:
                other.close()
            return real(conn, user_id, role)

        with mock.patch.object(auth, "_insert_session", watch):
            r = self.two_factor_step("admin@example.com", started)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, ["locked out"])

    def test_setup_confirmation_fails_if_the_password_was_reset_meanwhile(self):
        # As with an admin's reset of two-factor: nothing turned on and no session if the password is reset between
        # reading the setup challenge and using it up.
        self.invite_admin()
        self.client.post("/auth/password-reset/request", json={"email": "second@example.com"})
        reset_token = self.token_from_email("second@example.com", "reset-password")
        started = self.raw_login("second@example.com").json()
        info = self.client.post("/auth/two-factor/setup", json={"challenge": started["challenge"]}).json()
        self.totp_secrets["second@example.com"] = info["secret"].replace(" ", "")
        code = self.totp("second@example.com")
        uid, real = self.user_id("second@example.com"), two_factor.matching_step

        def reset_then_check(*args, **kwargs):
            self.reset_password_directly(reset_token)
            return real(*args, **kwargs)

        with mock.patch.object(two_factor, "clock", lambda: self.totp_now), \
                mock.patch.object(two_factor, "matching_step", reset_then_check):
            r = self.client.post("/auth/two-factor/setup/confirm", json={"challenge": started["challenge"], "code": code})
        self.assertEqual(r.status_code, 400, r.text)
        conn = dl.connect(self.db)
        try:
            self.assertFalse(two_factor.is_enabled(conn, uid))
        finally:
            conn.close()
        self.assertEqual(self.count("sessions", uid), 0)

    def test_a_password_reset_while_the_password_is_being_checked_gets_no_challenge(self):
        # Checking a password is slow on purpose. If it is reset in that time, the password just accepted is an old one:
        # no challenge may come of it, or its holder would get a step towards a session (on a first login, the chance
        # to enrol their own authenticator) that the reset was meant to cut off.
        self.invite_admin()
        for email, step in (("admin@example.com", "required"), ("second@example.com", "setup")):
            with self.subTest(step=step):
                self.client.post("/auth/password-reset/request", json={"email": email})
                reset_token, uid, real = self.token_from_email(email, "reset-password"), self.user_id(email), auth.verify_password

                def check_then_reset(password, stored):
                    ok = real(password, stored)  # the old password is right...
                    self.reset_password_directly(reset_token)  # ...and the reset lands before the login moves on
                    return ok

                with mock.patch.object(auth, "verify_password", check_then_reset):
                    r = self.client.post("/auth/login", json={"email": email, "password": PASSWORD})
                self.assertEqual(r.status_code, 401, r.text)
                self.assertEqual(self.count("login_challenges", uid), 0)
                self.assertEqual(self.raw_login(email, "Fresh-Pass-2029-x").json()["two_factor"], step)  # the new one works

    def test_a_login_challenge_reads_whether_two_factor_is_on_under_the_write_lock(self):
        # Read before the lock, it could change before the challenge is written: an admin's own first-time setup
        # finishing in between would leave a "setup" challenge (which can replace the authenticator) for an account
        # that now has one.
        seen, real = [], two_factor.is_enabled

        def watch(conn, user_id):
            other = sqlite3.connect(self.db, timeout=0.2)
            try:
                other.execute("DELETE FROM rate_events WHERE key = 'nobody'")  # any write at all
                other.commit()
                seen.append("let in")
            except sqlite3.OperationalError:
                seen.append("locked out")
            finally:
                other.close()
            return real(conn, user_id)

        with mock.patch.object(two_factor, "is_enabled", watch):
            self.raw_login("admin@example.com")
        self.assertEqual(seen, ["locked out"])


if __name__ == "__main__":
    unittest.main()
