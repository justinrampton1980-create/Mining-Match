"""Security monitoring (launch blocker B5)."""
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import access_control as ac
import api
import auth
import data_layer as dl
import monitoring
from storage import LocalStorage
from test_api import PASSWORD, ApiTest


class AppAlerts(ApiTest):
    def setUp(self):
        super().setUp()
        self.alerts_app = api.create_app(self.db, storage=LocalStorage(self.dir / "files"), mailer=self.outbox,
                                         base_url="https://miningmatch.example", secure_cookies=False,
                                         alert_email="ops@miningmatch.example")
        self.alerts_client = TestClient(self.alerts_app, raise_server_exceptions=False)

    def tearDown(self):
        self.alerts_client.close()
        super().tearDown()

    def alerts(self):
        return [m for m in self.outbox.messages if m["to"] == "ops@miningmatch.example"]

    def test_server_errors_are_emailed_once_an_hour_per_kind(self):
        with mock.patch.object(ac, "list_catalog", side_effect=RuntimeError("database on fire")), \
                self.assertLogs("mining_match", "ERROR"):
            h = self.login("admin@example.com")
            for _ in range(3):
                r = self.alerts_client.get("/catalog/licence", headers=h)
                self.assertEqual(r.status_code, 500)
                self.assertEqual(r.json()["detail"], "something went wrong on our side; please try again")
                self.assertNotIn("on fire", r.text)  # nothing internal leaks to the visitor
        (alert,) = self.alerts()
        self.assertEqual(alert["subject"], "Mining Match alert: server error (RuntimeError)")
        self.assertIn("GET /catalog/{kind}", alert["body"])

    def test_rate_limits_refusing_people_are_emailed(self):
        self.candidate()
        for _ in range(auth.LOGIN_FAILS_PER_EMAIL_AND_IP[0] + 3):
            self.alerts_client.post("/auth/login", json={"email": "jordan@example.com", "password": "wrong-pass-123"})
        (alert,) = self.alerts()
        self.assertEqual(alert["subject"], "Mining Match alert: a rate limit is refusing requests")
        self.assertIn("/auth/login", alert["body"])
        self.assertNotIn("wrong-pass-123", alert["body"])

    def test_no_alert_address_means_no_emails(self):
        app = api.create_app(self.db, storage=LocalStorage(self.dir / "files"), mailer=self.outbox,
                             base_url="https://x.example", secure_cookies=False)
        with TestClient(app, raise_server_exceptions=False) as c, \
                mock.patch.object(ac, "list_catalog", side_effect=RuntimeError("x")), \
                self.assertLogs("mining_match", "ERROR"):
            h = self.login("admin@example.com")
            self.assertEqual(c.get("/catalog/licence", headers=h).status_code, 500)
        self.assertEqual(self.alerts(), [])

    def test_the_live_app_reads_the_alert_address(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"MM_DB": f"{d}/mm.db", "MM_ALERT_EMAIL": "ops@x.example"}), \
                mock.patch.object(api, "create_app", return_value="app") as create:
            api.app_from_environment()
        self.assertEqual(create.call_args.kwargs["alert_email"], "ops@x.example")


class Checks(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = str(self.dir / "mm.db")
        self.conn = dl.connect(self.db)
        dl.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.dir)

    def test_site(self):
        ok = monitoring.check_site("https://x.example", fetch=lambda url: (200, b'{"ok": true}'))
        self.assertTrue(ok.ok)
        for fetch in (lambda url: (502, b"bad gateway"), lambda url: (200, b"<html>"), mock.Mock(side_effect=OSError("refused"))):
            self.assertFalse(monitoring.check_site("https://x.example", fetch=fetch).ok)

    def test_site_for_real(self):
        """Against the real API on a local port."""
        import uvicorn
        app = api.create_app(self.db, storage=LocalStorage(self.dir / "f"), mailer=auth.Outbox(),
                             base_url="http://x", secure_cookies=False, web_dir=None)
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            self.assertTrue(monitoring.check_site(f"http://127.0.0.1:{port}").ok)
        finally:
            server.should_exit = True
            t.join(timeout=5)
        self.assertFalse(monitoring.check_site(f"http://127.0.0.1:{port}").ok)  # now it's down

    def test_certificate(self):
        self.assertIsNone(monitoring.check_certificate("http://localhost:8000"))
        soon, later = time.time() + 5 * 86400, time.time() + 60 * 86400
        self.assertFalse(monitoring.check_certificate("https://x.example", expiry=lambda host: soon).ok)
        self.assertTrue(monitoring.check_certificate("https://x.example", expiry=lambda host: later).ok)
        self.assertFalse(monitoring.check_certificate("https://x.example", expiry=mock.Mock(side_effect=OSError)).ok)

    def test_disk(self):
        Usage = namedtuple("Usage", "total used free")
        with mock.patch.object(monitoring.shutil, "disk_usage", return_value=Usage(100 * 2 ** 30, 95 * 2 ** 30, 5 * 2 ** 30)):
            self.assertFalse(monitoring.check_disk(str(self.dir)).ok)  # 5%
        with mock.patch.object(monitoring.shutil, "disk_usage", return_value=Usage(10 * 2 ** 30, 8.5 * 2 ** 30, 1.5 * 2 ** 30)):
            self.assertFalse(monitoring.check_disk(str(self.dir)).ok)  # under 2 GB
        with mock.patch.object(monitoring.shutil, "disk_usage", return_value=Usage(100 * 2 ** 30, 50 * 2 ** 30, 50 * 2 ** 30)):
            self.assertTrue(monitoring.check_disk(str(self.dir)).ok)

    def test_backups(self):
        folder = self.dir / "backups"
        self.assertFalse(monitoring.check_backups(str(folder)).ok)  # no folder
        folder.mkdir()
        (folder / "notes.txt").write_text("x")
        self.assertFalse(monitoring.check_backups(str(folder)).ok)  # nothing encrypted
        old = folder / "db-2026-11-01.sql.gz.age"
        old.write_bytes(b"age-encryption.org/v1\n")
        t = time.time() - 30 * 3600
        os.utime(old, (t, t))
        self.assertFalse(monitoring.check_backups(str(folder)).ok)  # last night's is missing
        (folder / "db-2026-11-02.sql.gz.age").write_bytes(b"age-encryption.org/v1\n")
        self.assertTrue(monitoring.check_backups(str(folder)).ok)

    def test_scanner(self):
        path = str(self.dir / "clamd.ctl")
        self.assertFalse(monitoring.check_scanner(path).ok)  # not running
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(2)
        replies = [b"PONG\0", b"HUH\0"]

        def serve():
            for reply in replies:
                c, _ = srv.accept()
                with c:
                    c.recv(100)
                    c.sendall(reply)
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        try:
            self.assertTrue(monitoring.check_scanner(path).ok)
            self.assertFalse(monitoring.check_scanner(path).ok)
        finally:
            t.join(timeout=2)
            srv.close()

    def deny(self, user_id, n, minutes_ago=0):
        with self.conn:
            for _ in range(n):
                self.conn.execute("INSERT INTO audit_log (at, user_id, role, action, outcome) "
                                  "VALUES (datetime('now', ?), ?, 'employer', 'candidate.view', 'denied')",
                                  (f"-{minutes_ago} minutes", user_id))

    def test_bursts_of_refused_actions(self):
        self.deny("u-quiet", 5)
        self.deny("u-old", 30, minutes_ago=60)
        self.assertTrue(monitoring.check_denials(self.conn).ok)
        self.deny("u-probe", 25)
        c = monitoring.check_denials(self.conn)
        self.assertFalse(c.ok)
        self.assertIn("employer account u-probe (25 times)", c.detail)
        self.assertNotIn("u-quiet", c.detail)

    def test_site_wide_failed_logins(self):
        with self.conn:
            for i in range(monitoring.FAILED_LOGINS_SITE[0] - 1):
                self.conn.execute("INSERT INTO rate_events VALUES ('login_ip', ?, ?)", (f"10.0.0.{i % 3}", int(time.time())))
        self.assertTrue(monitoring.check_failed_logins(self.conn).ok)
        with self.conn:
            self.conn.execute("INSERT INTO rate_events VALUES ('login_ip', '10.0.0.9', ?)", (int(time.time()),))
        c = monitoring.check_failed_logins(self.conn)
        self.assertFalse(c.ok)
        self.assertIn("10.0.0.", c.detail)


class Alerting(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.conn = dl.connect(str(self.dir / "mm.db"))
        dl.init_schema(self.conn)
        self.outbox = auth.Outbox()
        self.t = [1_800_000_000.0]
        self.clock = mock.patch.object(monitoring, "now", lambda: self.t[0])
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.conn.close()
        shutil.rmtree(self.dir)

    def tick(self, check):
        return monitoring.run(self.conn, [check], self.outbox, "ops@x.example", [])

    def test_alert_once_remind_every_six_hours_then_all_clear(self):
        bad, good = monitoring.Check("disk", False, "1 GB free"), monitoring.Check("disk", True, "40 GB free")
        self.assertEqual(self.tick(good), [])
        self.assertEqual(self.tick(bad), ["PROBLEM (disk)"])
        self.t[0] += 300
        self.assertEqual(self.tick(bad), [])  # no repeat every 5 minutes
        self.t[0] += monitoring.REMIND_SECONDS
        self.assertEqual(self.tick(bad), ["STILL A PROBLEM (disk)"])
        self.t[0] += 300
        self.assertEqual(self.tick(good), ["fixed (disk)"])
        self.assertEqual(self.tick(good), [])
        self.assertEqual([m["subject"] for m in self.outbox.messages],
                         ["Mining Match: PROBLEM (disk)", "Mining Match: STILL A PROBLEM (disk)", "Mining Match: fixed (disk)"])
        self.assertEqual(self.outbox.messages[0]["body"], "1 GB free")

    def test_new_admin_logins_are_reported_once(self):
        admin = ac.create_admin(self.conn, "boss@x.example", auth.hash_password(PASSWORD))
        self.assertEqual(monitoring.new_admin_logins(self.conn), [])  # first run: remembers the time
        with self.conn:
            self.conn.execute("INSERT INTO sessions VALUES ('h1', ?, ?, ?, ?)",
                              (admin, int(self.t[0]) - 10, int(self.t[0]) + 99999, int(self.t[0]) - 10))
        self.t[0] += 300
        with self.conn:  # a candidate logging in isn't reported
            self.conn.execute("INSERT INTO users (id, email, role) VALUES ('cand-1', 'jo@x.example', 'candidate')")
            self.conn.execute("INSERT INTO sessions VALUES ('h3', 'cand-1', ?, ?, ?)",
                              (int(self.t[0]) - 30, int(self.t[0]) + 99999, int(self.t[0]) - 30))
            self.conn.execute("INSERT INTO sessions VALUES ('h2', ?, ?, ?, ?)",
                              (admin, int(self.t[0]) - 60, int(self.t[0]) + 99999, int(self.t[0]) - 60))
        logins = monitoring.new_admin_logins(self.conn)
        self.assertEqual([e for e, _ in logins], ["boss@x.example"])  # only the one since the last run
        sent = monitoring.run(self.conn, [], self.outbox, "ops@x.example", logins)
        self.assertEqual(sent, ["admin login"])
        self.assertIn("boss@x.example", self.outbox.messages[-1]["body"])
        self.t[0] += 300
        self.assertEqual(monitoring.new_admin_logins(self.conn), [])

    def test_main_runs_every_check_and_says_what_failed(self):
        backups = self.dir / "backups"
        backups.mkdir()
        (backups / "db-x.sql.gz.age").write_bytes(b"age")
        env = {"MM_DB": str(self.dir / "mm.db"), "MM_BASE_URL": "http://127.0.0.1:9", "MM_BACKUP_DIR": str(backups),
               "MM_CLAMD_SOCKET": str(self.dir / "none.ctl"), "MM_ALERT_EMAIL": "ops@x.example"}
        self.clock.stop()
        try:
            with mock.patch.object(auth, "mailer_from_env", return_value=self.outbox), \
                    mock.patch("sys.stdout") as out:
                self.assertEqual(monitoring.main(env), 1)
        finally:
            self.clock.start()
        printed = "".join(str(c.args[0]) for c in out.write.call_args_list)
        self.assertIn("FAIL site", printed)
        self.assertIn("FAIL scanner", printed)
        self.assertIn("ok   backups", printed)
        self.assertEqual(sorted(m["subject"] for m in self.outbox.messages),
                         ["Mining Match: PROBLEM (scanner)", "Mining Match: PROBLEM (site)"])


if __name__ == "__main__":
    unittest.main()
