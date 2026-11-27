"""Virus scanning of uploads with ClamAV's daemon (clamd).

Every uploaded file is streamed to clamd before it's saved. The check fails closed: if clamd
is down, slow, or answers anything other than a clear "OK" or "FOUND", the upload is refused
with ScanUnavailable, never saved unscanned.

Talks clamd's own protocol (INSTREAM) over its Unix socket, so it needs no extra packages.
On Ubuntu, `apt install clamav-daemon` runs clamd on /var/run/clamav/clamd.ctl and the
clamav-freshclam service keeps its virus signatures up to date.

ClamAV must run on the same server. Scanning through a network connection is not supported:
clamd's network protocol is neither encrypted nor authenticated, so documents would cross the
network in the clear and anyone on the route could answer "OK" for an infected file. A leftover
MM_CLAMD_HOST, MM_CLAMD_PORT or MM_CLAMD_ALLOW_REMOTE makes from_env() raise, so the app won't
start, instead of being ignored.

Check a server from the command line (scans a harmless standard test file, EICAR):

    python virus_scan.py --check
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import time
from dataclasses import dataclass

DEFAULT_SOCKET = "/var/run/clamav/clamd.ctl"
# Settings for scanning over the network, which was removed. Refused, never ignored: an operator who
# still has one set would otherwise believe uploads go to a remote scanner when they go to the local one.
REMOVED_SETTINGS = ("MM_CLAMD_HOST", "MM_CLAMD_PORT", "MM_CLAMD_ALLOW_REMOTE")
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
    socket_path: str = DEFAULT_SOCKET
    timeout: float = 30.0  # for the whole scan: connecting, sending the file and waiting for the answer

    @classmethod
    def from_env(cls) -> "ClamdScanner":
        for name in REMOVED_SETTINGS:
            if os.environ.get(name, "").strip():
                raise ValueError(
                    f"{name} is set, but scanning over the network is not supported. ClamAV's network protocol is "
                    "neither encrypted nor authenticated: candidates' documents would cross the network in the "
                    "clear, and anyone on the route could answer 'OK' for an infected file. Run ClamAV on this "
                    f"server, use MM_CLAMD_SOCKET, and remove {name}.")
        return cls(socket_path=os.environ.get("MM_CLAMD_SOCKET", DEFAULT_SOCKET),
                   timeout=float(os.environ.get("MM_CLAMD_TIMEOUT", "30")))

    @staticmethod
    def _arm(s: socket.socket, deadline: float) -> None:
        """Give the next socket step only the time left in the scan's overall budget.

        A socket timeout alone applies to each step, so a scanner that sends one byte every few
        seconds could hold a request open far longer than the limit."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("the virus scan took longer than its time limit")
        s.settimeout(remaining)

    def _connect(self, deadline: float) -> socket.socket:
        remaining = deadline - time.monotonic()
        try:
            if remaining <= 0:
                raise socket.timeout("the virus scan took longer than its time limit")
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.settimeout(remaining)
                s.connect(self.socket_path)
            except OSError:
                s.close()
                raise
            return s
        except OSError as e:
            raise ScanUnavailable(f"can't reach the virus scanner: {e}") from e

    @classmethod
    def _reply(cls, s: socket.socket, deadline: float) -> str:
        """clamd's answer, which ends with a NUL byte. An answer cut short is an error, never a verdict."""
        data = b""
        while not data.endswith(b"\0"):
            cls._arm(s, deadline)
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("the virus scanner hung up before finishing its answer")
            data += chunk
            if len(data) > 4096:
                raise ConnectionError("the virus scanner's answer was far longer than expected")
        return data.rstrip(b"\0").decode("utf-8", "replace").strip()

    def ping(self) -> None:
        """Raise ScanUnavailable unless clamd answers PONG."""
        deadline = time.monotonic() + self.timeout
        try:
            with self._connect(deadline) as s:
                self._arm(s, deadline)
                s.sendall(b"zPING\0")
                reply = self._reply(s, deadline)
        except OSError as e:
            raise ScanUnavailable(f"the virus scanner didn't answer: {e}") from e
        if reply != "PONG":
            raise ScanUnavailable(f"unexpected answer from the virus scanner: {reply[:100]!r}")

    def scan(self, data: bytes) -> None:
        """Return if the file is clean; raise VirusFound or ScanUnavailable otherwise."""
        deadline = time.monotonic() + self.timeout
        try:
            with self._connect(deadline) as s:
                self._arm(s, deadline)
                s.sendall(b"zINSTREAM\0")
                for i in range(0, len(data), CHUNK):
                    part = data[i:i + CHUNK]
                    self._arm(s, deadline)
                    s.sendall(struct.pack("!L", len(part)) + part)
                self._arm(s, deadline)
                s.sendall(struct.pack("!L", 0))
                reply = self._reply(s, deadline)
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
