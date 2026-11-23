"""Mining Match - two-factor login: codes from an authenticator app, plus one-time backup codes.

Standard library only. Works with any authenticator app (Google Authenticator, Microsoft
Authenticator, 1Password, Authy, ...), because it follows the TOTP standard (RFC 6238):
6 digits, a new code every 30 seconds, SHA-1.

Security choices
  * The shared secret is 160 random bits. When the server setting MM_SECRET_KEY is present,
    it is stored encrypted and tamper-checked (see seal/unseal), so a copy of the database
    alone doesn't reveal it.
  * A code is accepted for 30 seconds either side of now (phone clocks drift), and never twice:
    the last time step used is remembered, so a code someone watched being typed is useless.
  * Backup codes are 60-bit random values, shown once and stored only as SHA-256 hashes. Each
    works once.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
from typing import Optional
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
DRIFT_STEPS = 1            # accept the previous and next code too
RECOVERY_CODE_COUNT = 10
ISSUER = "Mining Match"
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: easy to copy by hand
_RECOVERY_LENGTH = 12                                     # about 59 bits


# ------------------------------------------------------------------ codes
def new_secret() -> str:
    """A new shared secret, in the base32 form authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key_bytes(secret: str) -> bytes:
    secret = secret.strip().replace(" ", "").upper()
    return base64.b32decode(secret + "=" * (-len(secret) % 8))


def code_at(secret: str, step: int) -> str:
    """The code for one 30-second time step (RFC 4226 HOTP with the step as counter)."""
    digest = hmac.new(_key_bytes(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def current_step(now: int) -> int:
    return int(now) // STEP_SECONDS


def match_code(secret: str, code: str, now: int, last_step: int = 0) -> Optional[int]:
    """The time step a code belongs to if it is valid now and newer than last_step, else None."""
    code = "".join(str(code or "").split())
    if len(code) != DIGITS or not code.isdigit():
        return None
    found = None
    here = current_step(now)
    for step in range(here - DRIFT_STEPS, here + DRIFT_STEPS + 1):
        # check every step (no early exit), so timing doesn't say which one matched
        if hmac.compare_digest(code_at(secret, step), code) and step > last_step:
            found = step
    return found


def setup_link(secret: str, account: str) -> str:
    """otpauth:// link; opening it on a phone adds the account to the authenticator app."""
    label = quote(f"{ISSUER}:{account}", safe="@:")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&digits={DIGITS}&period={STEP_SECONDS}"


# ------------------------------------------------------------ backup codes
def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list:
    """Fresh backup codes, formatted for reading: 'abcd-efgh-jkmn'."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_LENGTH))
        codes.append("-".join(raw[i:i + 4] for i in range(0, _RECOVERY_LENGTH, 4)))
    return codes


def normalise_recovery_code(code: str) -> Optional[str]:
    """Lower-case, without spaces or dashes; None if it can't be a backup code."""
    raw = "".join(ch for ch in str(code or "").lower() if ch not in " -")
    if len(raw) != _RECOVERY_LENGTH or any(ch not in _RECOVERY_ALPHABET for ch in raw):
        return None
    return raw


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


# ------------------------------------------------- storing the secret safely
def _server_key() -> Optional[bytes]:
    value = os.environ.get("MM_SECRET_KEY", "")
    return hashlib.sha256(value.encode("utf-8")).digest() if value else None


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    out, counter = b"", 0
    while len(out) < length:
        out += hmac.new(key, b"enc" + nonce + struct.pack(">I", counter), hashlib.sha256).digest()
        counter += 1
    return out[:length]


def seal(secret: str, key: Optional[bytes] = None) -> str:
    """Encrypt the secret for storage with the server key (HMAC-SHA256 keystream + HMAC tag).

    Without MM_SECRET_KEY the secret is stored as it is, marked 'plain:'.
    """
    key = key if key is not None else _server_key()
    if key is None:
        return "plain:" + secret
    nonce = secrets.token_bytes(16)
    data = secret.encode("ascii")
    body = bytes(a ^ b for a, b in zip(data, _stream(key, nonce, len(data))))
    tag = hmac.new(key, b"mac" + nonce + body, hashlib.sha256).digest()
    return "v1:" + base64.b64encode(nonce + body + tag).decode("ascii")


def unseal(stored: str, key: Optional[bytes] = None) -> str:
    """The secret back from storage. Raises ValueError if it was changed or the key is wrong."""
    if stored.startswith("plain:"):
        return stored[len("plain:"):]
    key = key if key is not None else _server_key()
    if not stored.startswith("v1:") or key is None:
        raise ValueError("two-factor secret can't be read: check the MM_SECRET_KEY setting")
    raw = base64.b64decode(stored[3:], validate=True)
    nonce, body, tag = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(key, b"mac" + nonce + body, hashlib.sha256).digest()):
        raise ValueError("two-factor secret can't be read: check the MM_SECRET_KEY setting")
    return bytes(a ^ b for a, b in zip(body, _stream(key, nonce, len(body)))).decode("ascii")
