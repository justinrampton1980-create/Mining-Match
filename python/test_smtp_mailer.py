"""Tests for auth.SMTPMailer: it must check the mail server's certificate before sending anything.

Python's smtplib accepts any certificate when starttls() is called without a context. These tests
use a tiny fake mail server that offers STARTTLS with a certificate made for the test, to prove that
a certificate nobody vouches for, or one for a different name, is refused and that the mail
account's password and the email itself are never sent.
"""
import base64
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import auth

SENDER = "Mining Match <no-reply@example.test>"
USERNAME, PASSWORD = "mailuser", "mail-password-123"


def make_certificate(directory: Path, name: str, subject_alt_names: str):
    """A self-signed certificate and key, made with the openssl command. Returns (cert, key) paths."""
    cert, key = str(directory / f"{name}.crt"), str(directory / f"{name}.key")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-keyout", key, "-out", cert, "-subj", "/CN=test mail server",
         "-addext", f"subjectAltName={subject_alt_names}",
         "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,digitalSignature,keyCertSign"],
        check=True, capture_output=True,
    )
    return cert, key


class FakeMailServer:
    """A tiny SMTP server for one connection. Offers STARTTLS with the given certificate and records
    what the client sent, before and after the connection was encrypted."""

    def __init__(self, cert: str, key: str):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(10)
        self.port = self.listener.getsockname()[1]
        self.before_tls = []        # commands received while the connection was still plain text
        self.tls_established = False
        self.auth_line = None       # the AUTH command, if the client got as far as logging in
        self.messages = []          # texts of emails received
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self):
        """Stop the server and wait for it, so what it recorded is complete."""
        self.listener.close()
        self.thread.join(timeout=10)

    def _serve(self):
        try:
            conn, _ = self.listener.accept()
        except OSError:
            return
        conn.settimeout(10)
        try:
            self._talk(conn)
        except OSError:  # includes ssl.SSLError: the client hung up, e.g. it refused the certificate
            pass

    def _talk(self, conn):
        reader = conn.makefile("rb")
        conn.sendall(b"220 localhost ESMTP test server\r\n")
        encrypted = False
        try:
            while True:
                line = reader.readline()
                if not line:
                    return
                command = line.decode("ascii", "replace").strip()
                verb = command.split(" ", 1)[0].upper()
                if not encrypted:
                    self.before_tls.append(command)
                if verb == "EHLO":
                    conn.sendall(b"250-localhost\r\n250 AUTH PLAIN\r\n" if encrypted
                                 else b"250-localhost\r\n250 STARTTLS\r\n")
                elif verb == "STARTTLS" and not encrypted:
                    conn.sendall(b"220 Ready to start TLS\r\n")
                    reader.close()
                    conn = self.context.wrap_socket(conn, server_side=True)
                    reader = conn.makefile("rb")
                    encrypted = self.tls_established = True
                elif verb == "AUTH":
                    self.auth_line = command
                    conn.sendall(b"235 2.7.0 Authentication successful\r\n")
                elif verb == "DATA":
                    conn.sendall(b"354 Go ahead\r\n")
                    data = b""
                    while not data.endswith(b"\r\n.\r\n"):
                        chunk = reader.readline()
                        if not chunk:
                            return
                        data += chunk
                    self.messages.append(data.decode("utf-8", "replace"))
                    conn.sendall(b"250 Queued\r\n")
                elif verb == "QUIT":
                    conn.sendall(b"221 Bye\r\n")
                    return
                else:  # MAIL FROM, RCPT TO, anything else
                    conn.sendall(b"250 OK\r\n")
        finally:
            conn.close()


class DefaultSettings(unittest.TestCase):
    def test_starttls_is_given_a_context_that_checks_the_certificate_and_name(self):
        with mock.patch.object(auth.smtplib, "SMTP") as smtp_class:
            smtp = smtp_class.return_value.__enter__.return_value
            auth.SMTPMailer("mail.example.test", 587, USERNAME, PASSWORD, SENDER).send("a@example.test", "Hi", "Body")
        context = smtp.starttls.call_args.kwargs.get("context")
        self.assertIsInstance(context, ssl.SSLContext, "starttls() must be given a context")
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        # Encrypted and checked first; only then the password and the email.
        self.assertEqual([call[0] for call in smtp.method_calls], ["starttls", "login", "send_message"])


@unittest.skipUnless(shutil.which("openssl"), "needs the openssl command to make test certificates")
class CertificateChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.for_localhost = make_certificate(cls.dir, "localhost", "DNS:localhost,IP:127.0.0.1")
        cls.for_another_name = make_certificate(cls.dir, "other", "DNS:mail.other.example")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def server(self, certificate):
        server = FakeMailServer(*certificate)
        self.addCleanup(server.close)
        return server

    def mailer(self, server, trusted_certificate=None):
        """A mailer for the fake server. trusted_certificate: a context trusting only that certificate,
        like a private mail relay's. Without it the mailer uses its safe default."""
        context = ssl.create_default_context(cafile=trusted_certificate[0]) if trusted_certificate else None
        return auth.SMTPMailer("127.0.0.1", server.port, USERNAME, PASSWORD, SENDER, tls_context=context)

    def assert_nothing_secret_was_sent(self, server):
        server.close()
        self.assertEqual(server.messages, [], "the email must not be sent")
        self.assertIsNone(server.auth_line, "the mail account's password must not be sent")
        self.assertEqual({c.split(" ", 1)[0].upper() for c in server.before_tls} - {"EHLO", "STARTTLS"}, set(),
                         "nothing but EHLO and STARTTLS may go over the unencrypted connection")

    def test_a_certificate_nobody_vouches_for_is_refused(self):
        server = self.server(self.for_localhost)  # self-signed; the default trust store doesn't know it
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.mailer(server).send("a@example.test", "Reset your password", "secret link")
        self.assert_nothing_secret_was_sent(server)

    def test_a_trusted_certificate_for_a_different_name_is_refused(self):
        server = self.server(self.for_another_name)  # trusted below, but it isn't for 127.0.0.1
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.mailer(server, trusted_certificate=self.for_another_name).send("a@example.test", "Hi", "Body")
        self.assert_nothing_secret_was_sent(server)

    def test_a_trusted_certificate_for_the_right_name_sends_the_email(self):
        # The control: shows the fake server works, so the refusals above are for the right reason.
        server = self.server(self.for_localhost)
        self.mailer(server, trusted_certificate=self.for_localhost).send("a@example.test", "Your invoice", "Pay by Friday")
        server.close()
        self.assertTrue(server.tls_established)
        self.assertEqual(len(server.messages), 1)
        self.assertIn("Subject: Your invoice", server.messages[0])
        self.assertIn("Pay by Friday", server.messages[0])
        self.assertIn(base64.b64encode(f"\0{USERNAME}\0{PASSWORD}".encode()).decode(), server.auth_line)
        self.assertEqual({c.split(" ", 1)[0].upper() for c in server.before_tls} - {"EHLO", "STARTTLS"}, set(),
                         "nothing but EHLO and STARTTLS may go over the unencrypted connection")


if __name__ == "__main__":
    unittest.main()
