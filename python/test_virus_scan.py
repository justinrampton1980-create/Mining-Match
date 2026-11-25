"""Virus scanning: the ClamAV client against a stand-in clamd speaking the real protocol,
and the upload endpoint refusing infected files and failing closed when scanning is down."""
import io
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import access_control as ac
import api
import auth
import data_layer as dl
import virus_scan
from storage import LocalStorage
from virus_scan import EICAR, ClamdScanner, ScanUnavailable, VirusFound

PASSWORD = "Correct-horse-9"
PDF = b"%PDF-1.4\n1 0 obj << >> endobj\n"
SCANNER_SETTINGS = ("MM_VIRUS_SCAN", "MM_CLAMD_SOCKET", "MM_CLAMD_HOST", "MM_CLAMD_PORT", "MM_CLAMD_TIMEOUT",
                    "MM_CLAMD_ALLOW_REMOTE")


def forget_scanner_settings():
    """Call inside a mock.patch.dict(os.environ): ignore scanner settings that happen to be set on this machine."""
    for name in SCANNER_SETTINGS:
        os.environ.pop(name, None)


class FakeClamd:
    """A clamd stand-in on a Unix socket. `mode` decides how it answers."""

    def __init__(self, path: str, mode: str = "normal"):
        self.path, self.mode, self.scanned = path, mode, []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(8)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                try:
                    self.handle(conn)
                except OSError:
                    pass

    @staticmethod
    def read_exact(conn, n):
        buf = b""
        while len(buf) < n:
            part = conn.recv(n - len(buf))
            if not part:
                raise OSError("closed")
            buf += part
        return buf

    def handle(self, conn):
        command = b""
        while not command.endswith(b"\0"):
            command += self.read_exact(conn, 1)
        if command == b"zPING\0":
            conn.sendall(b"PONG\0" if self.mode != "garbage" else b"HELLO\0")
            return
        assert command == b"zINSTREAM\0", command
        if self.mode == "close":
            return  # hang up without answering
        data = b""
        while True:
            (n,) = struct.unpack("!L", self.read_exact(conn, 4))
            if n == 0:
                break
            data += self.read_exact(conn, n)
        self.scanned.append(data)
        if self.mode == "silent":
            return  # read the whole file, then hang up without an answer
        if self.mode == "hang":
            self.stop.wait(2)
            return
        if self.mode == "unterminated":
            conn.sendall(b"stream: OK")  # the right words, but cut short: no end marker
            return
        if self.mode == "trickle":
            for byte in b"stream: OK\0":  # a valid answer, but one byte at a time
                conn.sendall(bytes([byte]))
                if self.stop.wait(0.2):
                    return
            return
        if self.mode == "error":
            conn.sendall(b"INSTREAM size limit exceeded. ERROR\0")
        elif self.mode == "garbage":
            conn.sendall(b"something odd\0")
        elif EICAR in data:
            conn.sendall(b"stream: Eicar-Signature FOUND\0")
        else:
            conn.sendall(b"stream: OK\0")

    def close(self):
        self.stop.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes the accept() so the thread ends now
        except OSError:
            pass
        self.sock.close()
        self.thread.join(timeout=2)


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "clamd.ctl")
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.close()

    def clamd(self, mode="normal"):
        server = FakeClamd(self.path, mode)
        self.servers.append(server)
        return ClamdScanner(socket_path=self.path, timeout=1)

    def test_clean_files_pass_and_the_whole_file_is_scanned(self):
        scanner = self.clamd()
        big = os.urandom(3 * virus_scan.CHUNK + 17)  # several chunks
        scanner.scan(big)
        self.assertTrue(scanner(PDF))
        self.assertEqual(self.servers[0].scanned[0], big)

    def test_infected_files_are_caught_with_the_virus_name(self):
        scanner = self.clamd()
        with self.assertRaises(VirusFound) as caught:
            scanner.scan(b"%PDF-1.4 " + EICAR)
        self.assertEqual(caught.exception.signature, "Eicar-Signature")
        self.assertFalse(scanner(EICAR))

    def test_every_kind_of_failure_means_unavailable_never_clean(self):
        scanner = ClamdScanner(socket_path=os.path.join(self.dir, "nothing-here"), timeout=1)
        with self.assertRaises(ScanUnavailable):
            scanner(PDF)  # no scanner running
        for mode in ("error", "garbage", "close", "silent", "hang"):
            with self.subTest(mode=mode):
                if self.servers:
                    self.servers.pop().close()
                    os.unlink(self.path)
                scanner = self.clamd(mode)
                with self.assertRaises(ScanUnavailable):
                    scanner(PDF)

    def test_ping_and_the_server_check(self):
        scanner = self.clamd()
        scanner.ping()
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(virus_scan.check(scanner), 0)
        self.assertIn("test file caught as Eicar-Signature", out.getvalue())

    def test_the_server_check_fails_when_the_scanner_misses_the_test_file(self):
        scanner = self.clamd()
        with mock.patch.object(ClamdScanner, "scan", lambda self, data: None), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(virus_scan.check(scanner), 1)
        self.assertIn("passed the EICAR test file", err.getvalue())
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(virus_scan.check(ClamdScanner(socket_path=self.path + "x", timeout=1)), 1)

    def test_an_answer_cut_short_is_never_clean(self):
        # Right words, no end marker: the scanner may have died mid-answer, so it can't be trusted.
        with self.assertRaises(ScanUnavailable):
            self.clamd("unterminated").scan(PDF)

    def test_the_whole_scan_has_one_time_limit(self):
        # Each byte of the answer arrives well inside the 0.5 s socket timeout, so only a limit on the
        # whole scan stops this: without one the scan would run for over two seconds and pass.
        self.servers.append(FakeClamd(self.path, "trickle"))
        scanner = ClamdScanner(socket_path=self.path, timeout=0.5)
        started = time.monotonic()
        with self.assertRaises(ScanUnavailable):
            scanner.scan(PDF)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_settings_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {"MM_CLAMD_SOCKET": "/run/x.ctl", "MM_CLAMD_TIMEOUT": "5"}, clear=False):
            os.environ.pop("MM_CLAMD_HOST", None)
            s = ClamdScanner.from_env()
        self.assertEqual((s.socket_path, s.host, s.timeout), ("/run/x.ctl", None, 5.0))
        with mock.patch.dict(os.environ, {"MM_CLAMD_HOST": "127.0.0.1", "MM_CLAMD_PORT": "3311"}, clear=False):
            s = ClamdScanner.from_env()
        self.assertEqual((s.host, s.port), ("127.0.0.1", 3311))

    def test_a_scanner_on_another_machine_needs_an_explicit_opt_in(self):
        # clamd's network protocol is neither encrypted nor authenticated, so documents would cross the
        # network in the clear and anyone on the route could answer "OK" for an infected file.
        for host in ("10.0.0.5", "scanner.example.com", "8.8.8.8"):
            with self.subTest(host=host):
                with mock.patch.dict(os.environ, {"MM_CLAMD_HOST": host}, clear=False):
                    os.environ.pop("MM_CLAMD_ALLOW_REMOTE", None)
                    with self.assertRaises(ValueError):
                        ClamdScanner.from_env()
                    os.environ["MM_CLAMD_ALLOW_REMOTE"] = "1"  # the operator says it is tunnelled
                    self.assertEqual(ClamdScanner.from_env().host, host)
        for host in ("localhost", "127.0.0.1", "::1"):  # never leaves this machine
            with self.subTest(host=host):
                with mock.patch.dict(os.environ, {"MM_CLAMD_HOST": host}, clear=False):
                    os.environ.pop("MM_CLAMD_ALLOW_REMOTE", None)
                    self.assertEqual(ClamdScanner.from_env().host, host)


class UploadsAreScanned(unittest.TestCase):
    """The real ClamdScanner plugged into the API, as app_from_environment does."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.sock = str(self.dir / "clamd.ctl")
        self.server = None
        self.outbox = auth.Outbox()
        self.files = self.dir / "files"
        self.app = api.create_app(str(self.dir / "mm.db"), storage=LocalStorage(self.files), mailer=self.outbox,
                                  base_url="https://miningmatch.example", secure_cookies=False,
                                  scan_upload=ClamdScanner(socket_path=self.sock, timeout=1))
        self.client = TestClient(self.app)
        r = self.client.post("/auth/register/candidate",
                             json={"email": "jordan@example.com", "password": PASSWORD, "name": "Jordan"})
        self.assertEqual(r.status_code, 202, r.text)
        token = self.outbox.messages[-1]["body"].split("token=")[1].split()[0]
        self.client.post("/auth/verify-email", json={"token": token})
        r = self.client.post("/auth/login", json={"email": "jordan@example.com", "password": PASSWORD})
        self.h = {"Authorization": f"Bearer {r.json()['token']}"}
        self.cid = self.client.get("/me", headers=self.h).json()["candidate_id"]

    def tearDown(self):
        if self.server:
            self.server.close()
        self.client.close()

    def upload(self, data, name="licence.pdf"):
        return self.client.post(f"/candidates/{self.cid}/documents", headers=self.h, data={"kind": "licence"},
                                files={"file": (name, data, "application/pdf")})

    def stored_files(self):
        return [p for p in self.files.rglob("*") if p.is_file()] if self.files.exists() else []

    def audit(self):
        conn = dl.connect(str(self.dir / "mm.db"))
        try:
            return [tuple(r) for r in conn.execute("SELECT action, outcome, detail FROM audit_log WHERE action = 'upload_document'")]
        finally:
            conn.close()

    def test_clean_upload_is_saved(self):
        self.server = FakeClamd(self.sock)
        r = self.upload(PDF)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(len(self.stored_files()), 1)
        self.assertEqual(self.server.scanned, [PDF])

    def test_infected_upload_is_refused_and_logged(self):
        self.server = FakeClamd(self.sock)
        r = self.upload(PDF + EICAR)
        self.assertEqual(r.status_code, 400)
        self.assertIn("virus scan", r.json()["detail"])
        self.assertEqual(self.stored_files(), [])
        self.assertEqual(self.client.get(f"/candidates/{self.cid}/documents", headers=self.h).json(), [])
        self.assertIn(("upload_document", "denied", "virus scan: infected"), self.audit())

    def test_no_scanner_means_no_upload(self):
        r = self.upload(PDF)  # clamd isn't running
        self.assertEqual(r.status_code, 503)
        self.assertIn("virus scanning is unavailable", r.json()["detail"])
        self.assertEqual(self.stored_files(), [])
        self.assertEqual(self.client.get(f"/candidates/{self.cid}/documents", headers=self.h).json(), [])
        self.assertIn(("upload_document", "denied", "virus scan unavailable"), self.audit())

    def test_a_scanner_error_means_no_upload(self):
        self.server = FakeClamd(self.sock, "error")
        self.assertEqual(self.upload(PDF).status_code, 503)
        self.assertEqual(self.stored_files(), [])


REAL_CLAMD = os.environ.get("MM_TEST_CLAMD_SOCKET")


@unittest.skipUnless(REAL_CLAMD, "set MM_TEST_CLAMD_SOCKET to a running clamd to test against it")
class RealClamd(UploadsAreScanned):
    """The same upload tests, plus the client's own checks, against a real ClamAV daemon.

    Needs clamd's signatures to detect the EICAR test file, as the official ones do."""

    def setUp(self):
        super().setUp()
        self.scanner = ClamdScanner(socket_path=REAL_CLAMD, timeout=30)
        self.app = api.create_app(str(self.dir / "mm2.db"), storage=LocalStorage(self.files), mailer=self.outbox,
                                  base_url="https://miningmatch.example", secure_cookies=False,
                                  scan_upload=self.scanner)
        self.client.close()
        self.client = TestClient(self.app)
        r = self.client.post("/auth/register/candidate",
                             json={"email": "jordan@example.com", "password": PASSWORD, "name": "Jordan"})
        token = self.outbox.messages[-1]["body"].split("token=")[1].split()[0]
        self.client.post("/auth/verify-email", json={"token": token})
        r = self.client.post("/auth/login", json={"email": "jordan@example.com", "password": PASSWORD})
        self.h = {"Authorization": f"Bearer {r.json()['token']}"}
        self.cid = self.client.get("/me", headers=self.h).json()["candidate_id"]

    def audit(self):
        conn = dl.connect(str(self.dir / "mm2.db"))
        try:
            return [tuple(r) for r in conn.execute(
                "SELECT action, outcome, detail FROM audit_log WHERE action = 'upload_document'")]
        finally:
            conn.close()

    def test_clean_upload_is_saved(self):
        r = self.upload(PDF)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(len(self.stored_files()), 1)

    def test_a_scanner_error_means_no_upload(self):
        self.skipTest("covered by the stand-in daemon")

    def test_no_scanner_means_no_upload(self):
        self.skipTest("covered by the stand-in daemon")

    def test_the_largest_allowed_upload_is_scanned(self):
        big = PDF + os.urandom(ac.MAX_UPLOAD_BYTES - len(PDF) - 1000)
        self.scanner.scan(big)
        with self.assertRaises(VirusFound):
            self.scanner.scan(big + EICAR)

    def test_the_server_check_passes(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(virus_scan.check(self.scanner), 0)


class ProductionSettings(unittest.TestCase):
    def test_the_live_app_always_scans(self):
        # The start-up ping is made to fail, so this passes whether or not ClamAV runs on this machine.
        with mock.patch.dict(os.environ, {"MM_SECURE_COOKIES": "1"}, clear=False), \
                mock.patch.object(ClamdScanner, "ping", side_effect=ScanUnavailable("no scanner here")):
            forget_scanner_settings()
            with self.assertLogs("mining_match", "ERROR"):
                scanner = api.scanner_from_environment()
        self.assertIsInstance(scanner, ClamdScanner)

    def test_turning_scanning_off_is_refused_on_a_live_site(self):
        for cookies in ("1", None):  # None = unset, which means live
            with self.subTest(cookies=cookies):
                env = {"MM_VIRUS_SCAN": "0"}
                with mock.patch.dict(os.environ, env, clear=False):
                    if cookies is None:
                        os.environ.pop("MM_SECURE_COOKIES", None)
                    else:
                        os.environ["MM_SECURE_COOKIES"] = cookies
                    with self.assertRaises(RuntimeError):
                        api.scanner_from_environment()

    def test_local_development_can_turn_it_off(self):
        with mock.patch.dict(os.environ, {"MM_VIRUS_SCAN": "0", "MM_SECURE_COOKIES": "0"}, clear=False):
            with self.assertLogs("mining_match", "WARNING"):
                self.assertIsNone(api.scanner_from_environment())

    def test_app_from_environment_wires_the_scanner_in(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"MM_DB": f"{d}/mm.db", "MM_FILES": f"{d}/files"}, clear=False), \
                mock.patch.object(ClamdScanner, "ping", side_effect=ScanUnavailable("no scanner here")), \
                mock.patch.object(api, "create_app", return_value="app") as create:
            forget_scanner_settings()
            with self.assertLogs("mining_match", "ERROR"):
                api.app_from_environment()
        self.assertIsInstance(create.call_args.kwargs["scan_upload"], ClamdScanner)


if __name__ == "__main__":
    unittest.main()
