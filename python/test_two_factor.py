"""Two-factor login: the code maths, and every way through the API. Run: python -m unittest -v"""
import base64
import unittest
from unittest import mock

import api
import auth
import data_layer as dl
import test_api
import two_factor as tf
from storage import LocalStorage
from test_api import PASSWORD

T0 = 2_000_000_010  # a fixed "now" for tests that move the clock


def setUpModule():
    global _patch
    _patch = mock.patch.object(auth, "SCRYPT_N", 2 ** 10)
    _patch.start()


def tearDownModule():
    _patch.stop()


class Codes(unittest.TestCase):
    # RFC 6238 appendix B (SHA-1 secret "12345678901234567890"), last 6 of the 8 digits.
    SECRET = base64.b32encode(b"12345678901234567890").decode()

    def test_matches_the_published_test_vectors(self):
        for t, code in [(59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
                        (1234567890, "005924"), (2000000000, "279037"), (20000000000, "353130")]:
            self.assertEqual(tf.code_at(self.SECRET, tf.current_step(t)), code, t)

    def test_accepts_one_step_of_clock_drift_and_never_the_same_code_twice(self):
        secret, now = tf.new_secret(), T0
        here = tf.current_step(now)
        for offset in (-1, 0, 1):
            self.assertEqual(tf.match_code(secret, tf.code_at(secret, here + offset), now), here + offset)
        for offset in (-2, 2):
            self.assertIsNone(tf.match_code(secret, tf.code_at(secret, here + offset), now))
        self.assertIsNone(tf.match_code(secret, tf.code_at(secret, here), now, last_step=here))
        self.assertIsNone(tf.match_code(secret, "12345", now))
        self.assertIsNone(tf.match_code(secret, "abcdef", now))
        spaced = tf.code_at(secret, here)
        self.assertEqual(tf.match_code(secret, f"{spaced[:3]} {spaced[3:]}", now), here)

    def test_backup_codes_are_random_and_forgiving_to_type(self):
        codes = tf.new_recovery_codes()
        self.assertEqual(len(set(codes)), tf.RECOVERY_CODE_COUNT)
        self.assertRegex(codes[0], r"^[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}$")
        self.assertEqual(tf.normalise_recovery_code(f" {codes[0].upper()} "), codes[0].replace("-", ""))
        self.assertIsNone(tf.normalise_recovery_code("0000-0000-0000"))  # 0 isn't in the alphabet
        self.assertIsNone(tf.normalise_recovery_code("abcd"))

    def test_setup_link_opens_an_authenticator_app(self):
        link = tf.setup_link("ABCDEFGH", "jo@example.com")
        self.assertTrue(link.startswith("otpauth://totp/Mining%20Match:jo@example.com?secret=ABCDEFGH"))
        self.assertIn("issuer=Mining%20Match", link)

    def test_sealed_secrets_need_the_key_and_detect_tampering(self):
        key, secret = b"k" * 32, tf.new_secret()
        sealed = tf.seal(secret, key)
        self.assertTrue(sealed.startswith("v1:"))
        self.assertNotIn(secret, sealed)
        self.assertEqual(tf.unseal(sealed, key), secret)
        with self.assertRaises(ValueError):
            tf.unseal(sealed, b"x" * 32)
        raw = bytearray(base64.b64decode(sealed[3:]))
        raw[20] ^= 1
        with self.assertRaises(ValueError):
            tf.unseal("v1:" + base64.b64encode(bytes(raw)).decode(), key)
        self.assertEqual(tf.unseal("plain:" + secret), secret)  # no server key set


class TwoFactorLogin(test_api.ApiTest):
    def code(self, secret, now, offset=0):
        return tf.code_at(secret, tf.current_step(now) + offset)

    def start(self, email="jordan@example.com", password=PASSWORD, **extra):
        r = self.client.post("/auth/login", json={"email": email, "password": password, **extra})
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def finish(self, challenge, code, **extra):
        return self.client.post("/auth/login/code", json={"challenge": challenge, "code": code, **extra})

    def emails_to(self, to):
        return [m["subject"] for m in self.outbox.messages if m["to"] == to]

    def test_turning_it_on_then_logging_in_needs_a_code(self):
        h, _ = self.candidate()
        other = self.login("jordan@example.com")
        self.assertFalse(self.client.get("/me", headers=h).json()["two_factor"])
        r = self.client.post("/auth/two-factor/setup", json={"password": "wrong-pass-123"}, headers=h)
        self.assertEqual(r.status_code, 401)
        r = self.client.post("/auth/two-factor/setup", json={"password": PASSWORD}, headers=h)
        secret, link = r.json()["secret"], r.json()["setup_link"]
        self.assertIn(secret, link)
        self.assertEqual(self.client.post("/auth/two-factor/enable", json={"code": "000000"}, headers=h).status_code, 400)
        self.assertFalse(self.client.get("/me", headers=h).json()["two_factor"])  # not on until a code works
        r = self.client.post("/auth/two-factor/enable", json={"code": self.code(secret, auth.clock())}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["recovery_codes"]), 10)
        self.assertEqual(self.client.get("/me", headers=h).status_code, 200)
        self.assertEqual(self.client.get("/me", headers=other).status_code, 401)  # other devices logged out
        self.assertIn("Two-factor login is on for your Mining Match account", self.emails_to("jordan@example.com"))
        self.assertEqual(self.client.get("/auth/two-factor", headers=h).json(),
                         {"enabled": True, "backup_codes_left": 10, "required": False})

        now = [auth.clock() + 60]  # a later code than the one used to turn it on
        with mock.patch.object(auth, "clock", lambda: now[0]):
            r = self.start()
            self.assertTrue(r.json()["two_factor_required"])
            self.assertNotIn("token", r.json())  # the password alone gives no session
            challenge = r.json()["challenge"]
            self.assertEqual(self.client.get("/me", headers={"Authorization": f"Bearer {challenge}"}).status_code, 401)
            self.assertEqual(self.finish(challenge, "000000").status_code, 401)
            good = self.code(secret, now[0])
            r = self.finish(challenge, good)
            self.assertEqual(r.status_code, 200, r.text)
            session = {"Authorization": f"Bearer {r.json()['token']}"}
            self.assertEqual(self.client.get("/me", headers=session).status_code, 200)
            self.assertEqual(self.finish(challenge, good).status_code, 400)  # challenges work once
            # someone who watched the code being typed can't reuse it, even with the password
            self.assertEqual(self.finish(self.start().json()["challenge"], good).status_code, 401)

    def test_backup_codes_work_once_and_the_owner_is_told(self):
        h, _ = self.candidate()
        _, codes = self.enable_two_factor(h)
        r = self.finish(self.start().json()["challenge"], codes[0].upper().replace("-", " "))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("A backup code was used to log in to Mining Match", self.emails_to("jordan@example.com"))
        self.assertIn("9 backup codes left", self.outbox.messages[-1]["body"])
        self.assertEqual(self.finish(self.start().json()["challenge"], codes[0]).status_code, 401)

        r = self.client.post("/auth/two-factor/recovery-codes", json={"password": PASSWORD, "code": codes[1]}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        fresh = r.json()["recovery_codes"]
        self.assertEqual(self.finish(self.start().json()["challenge"], codes[2]).status_code, 401)  # old set gone
        self.assertEqual(self.finish(self.start().json()["challenge"], fresh[0]).status_code, 200)

    def test_guessing_is_limited_per_login_per_account_and_in_time(self):
        h, _ = self.candidate()
        secret, _ = self.enable_two_factor(h)
        now = [auth.clock() + 60]
        with mock.patch.object(auth, "clock", lambda: now[0]):
            challenge = self.start().json()["challenge"]
            for _ in range(auth.CHALLENGE_TRIES):
                self.assertEqual(self.finish(challenge, "000000").status_code, 401)
            # the right code no longer helps: the password has to be typed again
            self.assertEqual(self.finish(challenge, self.code(secret, now[0])).status_code, 400)

            for _ in range(auth.CODE_FAILS_PER_USER[0] - auth.CHALLENGE_TRIES):
                self.finish(self.start().json()["challenge"], "000000")
            r = self.finish(self.start().json()["challenge"], self.code(secret, now[0]))
            self.assertEqual(r.status_code, 429)  # even the right code waits
            now[0] += auth.CODE_FAILS_PER_USER[1] + 1

            late = self.start().json()["challenge"]
            now[0] += auth.CHALLENGE_SECONDS + 1
            self.assertEqual(self.finish(late, self.code(secret, now[0])).status_code, 400)
            self.assertEqual(self.finish(self.start().json()["challenge"], self.code(secret, now[0])).status_code, 200)

    def test_website_cookie_is_set_only_after_the_code(self):
        h, _ = self.candidate()
        secret, _ = self.enable_two_factor(h)
        with mock.patch.object(auth, "clock", lambda: T0):
            r = self.start(use_cookie=True)
            self.assertNotIn(api.SESSION_COOKIE, r.cookies)
            r = self.finish(r.json()["challenge"], self.code(secret, T0), use_cookie=True)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn(api.SESSION_COOKIE, r.cookies)
            self.assertNotIn("token", r.json())

    def test_turning_it_off_needs_the_password_and_a_code(self):
        h, _ = self.candidate()
        secret, _ = self.enable_two_factor(h)
        now = auth.clock() + 60
        with mock.patch.object(auth, "clock", lambda: now):
            good = self.code(secret, now)
            for body in ({"password": "wrong-pass-123", "code": good}, {"password": PASSWORD, "code": "000000"}):
                self.assertEqual(self.client.post("/auth/two-factor/disable", json=body, headers=h).status_code, 401)
            r = self.client.post("/auth/two-factor/disable", json={"password": PASSWORD, "code": good}, headers=h)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("Two-factor login is off for your Mining Match account", self.emails_to("jordan@example.com"))
            self.assertIn("token", self.start().json())

    def test_password_reset_does_not_turn_it_off(self):
        h, _ = self.candidate()
        secret, _ = self.enable_two_factor(h)
        self.client.post("/auth/password-reset/request", json={"email": "jordan@example.com"})
        token = self.token_from_email("jordan@example.com", "reset-password")
        self.client.post("/auth/password-reset/confirm", json={"token": token, "new_password": "New-Pass-2027!"})
        self.assertTrue(self.start(password="New-Pass-2027!").json()["two_factor_required"])

    def test_secret_is_encrypted_in_the_database_when_a_server_key_is_set(self):
        h, _ = self.candidate()
        with mock.patch.dict("os.environ", {"MM_SECRET_KEY": "test-server-key"}):
            secret, _ = self.enable_two_factor(h)
            conn = dl.connect(self.db)
            stored = conn.execute("SELECT secret FROM two_factor").fetchall()
            conn.close()
            candidate_row = [r["secret"] for r in stored if r["secret"].startswith("v1:")]
            self.assertEqual(len(candidate_row), 1)
            self.assertNotIn(secret, candidate_row[0])
            now = auth.clock() + 60
            with mock.patch.object(auth, "clock", lambda: now):
                r = self.finish(self.start().json()["challenge"], self.code(secret, now))
                self.assertEqual(r.status_code, 200, r.text)

    def test_admins_must_turn_it_on_and_keep_it_on(self):
        r = self.client.post("/admin/admins", json={"email": "second@example.com"}, headers=self.admin)
        token = self.token_from_email("second@example.com", "reset-password")
        self.client.post("/auth/password-reset/confirm", json={"token": token, "new_password": "Admin-Two-2027"})
        h = self.login("second@example.com", "Admin-Two-2027")
        self.assertTrue(self.client.get("/me", headers=h).json()["must_set_up_two_factor"])
        r = self.client.get("/admin/users", headers=h)
        self.assertEqual((r.status_code, r.json()["detail"]),
                         (403, "administrators must turn on two-factor login first (Account page)"))
        secret, _ = self.enable_two_factor(h, "Admin-Two-2027")
        self.assertFalse(self.client.get("/me", headers=h).json()["must_set_up_two_factor"])
        self.assertEqual(self.client.get("/admin/users", headers=h).status_code, 200)
        r = self.client.post("/auth/two-factor/disable", headers=h,
                             json={"password": "Admin-Two-2027", "code": self.code(secret, auth.clock(), 1)})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "administrators must keep two-factor login on"))

    def test_setting_can_be_turned_off_for_local_development(self):
        app = api.create_app(self.db, storage=LocalStorage(self.dir / "files2"), mailer=self.outbox,
                             base_url="https://x.example", secure_cookies=False, require_admin_two_factor=False)
        client = test_api.TestClient(app)
        r = client.post("/admin/admins", json={"email": "third@example.com"}, headers=self.admin)
        token = self.token_from_email("third@example.com", "reset-password")
        client.post("/auth/password-reset/confirm", json={"token": token, "new_password": "Admin-Three-2027"})
        r = client.post("/auth/login", json={"email": "third@example.com", "password": "Admin-Three-2027"})
        self.assertEqual(client.get("/admin/users", headers={"Authorization": f"Bearer {r.json()['token']}"}).status_code, 200)
        client.close()

    def test_admin_can_reset_it_for_someone_who_lost_their_phone(self):
        h, _ = self.candidate()
        self.enable_two_factor(h)
        user_id = self.client.get("/me", headers=h).json()["user_id"]
        listed = {u["user_id"]: u for u in self.client.get("/admin/users", headers=self.admin).json()}
        self.assertTrue(listed[user_id]["two_factor"])

        other, _ = self.employer()
        self.assertEqual(self.client.post(f"/admin/users/{user_id}/reset-two-factor", json={"reason": "x"},
                                          headers=other).status_code, 403)
        admin_id = self.client.get("/me", headers=self.admin).json()["user_id"]
        self.assertEqual(self.client.post(f"/admin/users/{admin_id}/reset-two-factor", json={"reason": "x"},
                                          headers=self.admin).status_code, 403)
        self.assertEqual(self.client.post(f"/admin/users/{user_id}/reset-two-factor", json={},
                                          headers=self.admin).status_code, 422)  # a reason is required
        r = self.client.post(f"/admin/users/{user_id}/reset-two-factor", json={"reason": "lost phone, ID checked"},
                             headers=self.admin)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get("/me", headers=h).status_code, 401)  # logged out everywhere
        self.assertIn("Two-factor login was turned off by Mining Match support", self.emails_to("jordan@example.com"))
        self.assertIn("token", self.start().json())
        log = self.client.get(f"/admin/audit-log?resource_id={user_id}", headers=self.admin).json()
        self.assertTrue(any(e["action"] == "user.reset_two_factor" and e["outcome"] == "allowed" for e in log))

    def test_suspension_between_password_and_code_stops_the_login(self):
        h, _ = self.candidate()
        secret, _ = self.enable_two_factor(h)
        user_id = self.client.get("/me", headers=h).json()["user_id"]
        now = auth.clock() + 60
        with mock.patch.object(auth, "clock", lambda: now):
            challenge = self.start().json()["challenge"]
            self.client.put(f"/admin/users/{user_id}/active", json={"active": False, "reason": "spam"},
                            headers=self.admin)
            self.assertEqual(self.finish(challenge, self.code(secret, now)).status_code, 403)


if __name__ == "__main__":
    unittest.main()
