"""Virus scanning of uploads with ClamAV's daemon (clamd).

Every uploaded file is streamed to clamd before it's saved. The check fails closed: if clamd
is down, slow, or answers anything other than a clear "OK" or "FOUND", the upload is refused
with ScanUnavailable, never saved unscanned.

Talks clamd's own protocol (INSTREAM) over its Unix socket or TCP, so it needs no extra
packages. On Ubuntu, `apt install clamav-daemon` runs clamd on /var/run/clamav/clamd.ctl
and the clamav-freshclam service keeps its virus signatures up to date.

Check a server from the command line (scans a harmless standard test file, EICAR):

    python virus_scan.py --check
"""
from __future__ import annotations

import os
import socket
import struct
import sys
from dataclasses import dataclass
from typing import Optional

DEFAULT_SOCKET = "/var/run/clamav/clamd.ctl"
CHUNK = 64 * 1024
# The industry-standard harmless test "virus". Every scanner reports it as Eicar-Signature.
# Kept split so this file itself isn't flagged by scanners.
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


class ScanUnavailable(Exception):
    """The file couldn't be checked. Refuse it and ask the user to try again later."""


class VirusFound(ValueError):
    """The file is infected. A ValueError, so the API answers 400 with the message."""

    def __init__(self, signature: str):
        super().__init__("this file was rejected by the virus scan")
        self.signature = signature


@dataclass(frozen=True)
class ClamdScanner:
    socket_path: Optional[str] = DEFAULT_SOCKET  # used unless host is set
    host: Optional[str] = None
    port: int = 3310
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "ClamdScanner":
        host = os.environ.get("MM_CLAMD_HOST", "").strip() or None
        return cls(socket_path=os.environ.get("MM_CLAMD_SOCKET", DEFAULT_SOCKET), host=host,
                   port=int(os.environ.get("MM_CLAMD_PORT", "3310")),
                   timeout=float(os.environ.get("MM_CLAMD_TIMEOUT", "30")))

    def _connect(self) -> socket.socket:
        try:
            if self.host:
                return socket.create_connection((self.host, self.port), timeout=self.timeout)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.settimeout(self.timeout)
                s.connect(self.socket_path)
            except OSError:
                s.close()
                raise
            return s
        except OSError as e:
            raise ScanUnavailable(f"can't reach the virus scanner: {e}") from e

    @staticmethod
    def _reply(s: socket.socket) -> str:
        data = b""
        while not data.endswith(b"\0"):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > 4096:
                break
        return data.rstrip(b"\0").decode("utf-8", "replace").strip()

    def ping(self) -> None:
        """Raise ScanUnavailable unless clamd answers PONG."""
        try:
            with self._connect() as s:
                s.sendall(b"zPING\0")
                reply = self._reply(s)
        except OSError as e:
            raise ScanUnavailable(f"the virus scanner didn't answer: {e}") from e
        if reply != "PONG":
            raise ScanUnavailable(f"unexpected answer from the virus scanner: {reply[:100]!r}")

    def scan(self, data: bytes) -> None:
        """Return if the file is clean; raise VirusFound or ScanUnavailable otherwise."""
        try:
            with self._connect() as s:
                s.sendall(b"zINSTREAM\0")
                for i in range(0, len(data), CHUNK):
                    part = data[i:i + CHUNK]
                    s.sendall(struct.pack("!L", len(part)) + part)
                s.sendall(struct.pack("!L", 0))
                reply = self._reply(s)
        except OSError as e:  # includes timeouts and a scanner closing the connection early
            raise ScanUnavailable(f"the virus scan didn't finish: {e}") from e
        if reply == "stream: OK":
            return
        if reply.startswith("stream: ") and reply.endswith(" FOUND"):
            raise VirusFound(reply[len("stream: "):-len(" FOUND")].strip() or "unknown")
        # "... ERROR" (e.g. the file is over clamd's StreamMaxLength), or anything unexpected.
        raise ScanUnavailable(f"the virus scanner couldn't check this file: {reply[:200]!r}")

    def __call__(self, data: bytes) -> bool:
        """The api's scan_upload hook: True if clean, False if infected, raises if unsure."""
        try:
            self.scan(data)
        except VirusFound:
            return False
        return True


def check(scanner: ClamdScanner) -> int:
    """Confirm clamd answers, passes a clean file and catches the EICAR test file."""
    try:
        scanner.ping()
        scanner.scan(b"Mining Match virus scan check: a clean file")
        try:
            scanner.scan(EICAR)
        except VirusFound as e:
            print(f"virus scanning OK: clean file passed, test file caught as {e.signature}")
            return 0
        print("FAILED: the scanner passed the EICAR test file; its signatures may be missing", file=sys.stderr)
        return 1
    except ScanUnavailable as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if sys.argv[1:] != ["--check"]:
        print(__doc__)
        sys.exit(2)
    sys.exit(check(ClamdScanner.from_env()))
