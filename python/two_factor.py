"""Two-factor login for admin accounts (launch blocker B3).

Admins sign in with their password AND a 6-digit code from an authenticator app (Google
Authenticator, Microsoft Authenticator, 1Password and similar: the standard TOTP method,
RFC 6238, 30-second codes). Ten one-time recovery codes cover a lost phone.

How a login goes:
  1. POST /auth/login with the password. For an admin this starts no session; it returns a
     short-lived challenge: "required" (enter a code) or "setup" (first login: set up the app).
  2. "required": POST /auth/login/two-factor with the challenge and a code or recovery code.
     "setup": POST /auth/two-factor/setup to get the secret and QR code, then
     POST /auth/two-factor/setup/confirm with a code from the app. That turns it on, shows the
     recovery codes once, and starts the session.

Rules:
  * An admin session is valid only while two-factor is on (auth.session_user checks), so an
    admin can't use the site without it, even with a session from before it was set up.
  * Each code works once (a code can't be replayed within its 30 seconds), and a challenge
    allows 5 wrong codes; an account allows 10 wrong codes in 15 minutes.
  * Recovery codes are stored hashed and work once each.
  * Another admin can reset two-factor for an admin who lost their phone (logged). If the only
    admin is locked out, reset it from the server's command line:
        python two_factor.py reset admin@example.com     (with MM_DB set)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import secrets
import struct
import sys
import time
from typing import Optional
from urllib.parse import quote

STEP = 30
DIGITS = 6
WINDOW = 1  # also accept the code just before and just after, for phone clocks a little off
CHALLENGE_SECONDS = 10 * 60
CHALLENGE_TRIES = 5
WRONG_CODES_PER_USER = (10, 15 * 60)
RECOVERY_CODES = 10
ISSUER = "Mining Match"


def clock() -> float:
    """Time for working out codes (tests move it to step through codes)."""
    return time.time()


def _now() -> int:
    """Time for challenge expiry."""
    return int(time.time())


class TwoFactorError(Exception):
    """A wrong or expired code or challenge. The API answers 400."""


# ------------------------------------------------------------------ TOTP
def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** DIGITS).zfill(DIGITS)


def current_step(at: Optional[float] = None) -> int:
    return int((clock() if at is None else at) // STEP)


def matching_step(secret: str, code: str, last_step: Optional[int]) -> Optional[int]:
    """The time step this code belongs to, if valid and newer than the last one used."""
    # ASCII digits only: str.isdigit() also accepts other scripts' digits (fullwidth, Arabic-Indic, ...), and
    # hmac.compare_digest raises on non-ASCII text. Dropped here, such input is just a wrong code.
    code = "".join(ch for ch in str(code or "") if ch in "0123456789")
    if len(code) != DIGITS:
        return None
    now = current_step()
    for step in range(now - WINDOW, now + WINDOW + 1):
        if last_step is not None and step <= last_step:
            continue  # used already (or older than a code already used): no replays
        if hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def otpauth_uri(email: str, secret: str) -> str:
    label = quote(f"{ISSUER}:{email}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def qr_svg(text: str) -> Optional[str]:
    """An SVG QR code of text, or None if the segno package isn't installed."""
    try:
        import segno
    except ImportError:
        return None
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=5, border=2, xmldecl=False, svgns=True,
                                     svgclass=None, lineclass=None, dark="#000", light="#fff")
    return buf.getvalue().decode("utf-8")


# ------------------------------------------------------------ recovery codes
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i


def _new_recovery_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def _hash_recovery(code: str) -> str:
    clean = "".join(ch for ch in str(code or "").lower() if ch.isalnum())
    return hashlib.sha256(clean.encode("ascii", "ignore")).hexdigest()


def _looks_like_recovery_code(code: str) -> bool:
    return len("".join(ch for ch in str(code or "") if ch.isalnum())) == 12


# ------------------------------------------------------------------ storage
def is_enabled(conn, user_id: str) -> bool:
    row = conn.execute("SELECT enabled_at FROM two_factor WHERE user_id = ?", (user_id,)).fetchone()
    return bool(row and row["enabled_at"])


def _replace_recovery_codes(conn, user_id: str) -> list:
    codes = [_new_recovery_code() for _ in range(RECOVERY_CODES)]
    conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
    conn.executemany("INSERT INTO recovery_codes (code_hash, user_id) VALUES (?, ?)",
                     [(_hash_recovery(c), user_id) for c in codes])
    return codes


def turn_on(conn, user_id: str, secret: str, step: int, *, setup_challenge: Optional[str] = None) -> list:
    """The writes that turn two-factor on. Doesn't commit: call it inside `with conn:`, together with whatever
    else must happen in the same transaction (the login that follows, in auth.confirm_two_factor).

    setup_challenge: the token_hash of the login challenge this setup belongs to. It is used up here, and this
    raises TwoFactorError, having written nothing, if it is gone or expired (an admin's reset, a password
    change or the clock came in after it was read), so nothing is turned on from a challenge that no longer
    stands. Also ends the user's other sessions. Returns the recovery codes."""
    if setup_challenge is not None:
        used = conn.execute("DELETE FROM login_challenges WHERE token_hash = ? AND user_id = ? "
                            "AND purpose = 'setup' AND expires_at > ?", (setup_challenge, user_id, _now()))
        if used.rowcount != 1:
            raise TwoFactorError("this login has expired, start again")
    conn.execute(
        "INSERT INTO two_factor (user_id, secret, enabled_at, last_step) VALUES (?, ?, CURRENT_TIMESTAMP, ?) "
        "ON CONFLICT (user_id) DO UPDATE SET secret = excluded.secret, enabled_at = excluded.enabled_at, "
        "last_step = excluded.last_step", (user_id, secret, step))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return _replace_recovery_codes(conn, user_id)


def enable(conn, user_id: str, secret: str, step: int, *, setup_challenge: Optional[str] = None) -> list:
    """Turn two-factor on with a confirmed secret, as one committed transaction. See turn_on()."""
    with conn:
        return turn_on(conn, user_id, secret, step, setup_challenge=setup_challenge)


def reset(conn, user_id: str) -> None:
    """Turn two-factor off (lost phone). The user must set it up again at their next login."""
    with conn:
        conn.execute("DELETE FROM two_factor WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM login_challenges WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def check_code(conn, user_id: str, code: str) -> str:
    """Accept an app code or an unused recovery code. Returns 'app' or 'recovery'; raises if wrong."""
    row = conn.execute("SELECT secret, last_step FROM two_factor WHERE user_id = ? AND enabled_at IS NOT NULL",
                       (user_id,)).fetchone()
    if row is None:
        raise TwoFactorError("two-factor login isn't set up for this account")
    step = matching_step(row["secret"], code, row["last_step"])
    if step is not None:
        with conn:
            cur = conn.execute("UPDATE two_factor SET last_step = ? WHERE user_id = ? AND (last_step IS NULL OR last_step < ?)",
                               (step, user_id, step))
        if cur.rowcount == 1:
            return "app"
    if _looks_like_recovery_code(code):
        with conn:
            cur = conn.execute("UPDATE recovery_codes SET used_at = CURRENT_TIMESTAMP "
                               "WHERE code_hash = ? AND user_id = ? AND used_at IS NULL", (_hash_recovery(code), user_id))
        if cur.rowcount == 1:
            return "recovery"
    raise TwoFactorError("that code isn't right, or has been used already")


def new_recovery_codes(conn, user_id: str, code: str) -> list:
    """Replace the recovery codes, after checking a current app code."""
    if check_code(conn, user_id, code) != "app":
        raise TwoFactorError("use a code from your authenticator app to make new recovery codes")
    with conn:
        return _replace_recovery_codes(conn, user_id)


def recovery_codes_left(conn, user_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ? AND used_at IS NULL",
                        (user_id,)).fetchone()[0]


# ------------------------------------------------------------------ challenges
def _hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def start_challenge(conn, user_id: str) -> dict:
    """After a correct password: a one-time challenge for the second step."""
    purpose = "required" if is_enabled(conn, user_id) else "setup"
    token, now = secrets.token_urlsafe(32), _now()
    with conn:
        conn.execute("DELETE FROM login_challenges WHERE user_id = ? OR expires_at <= ?", (user_id, now))
        conn.execute("INSERT INTO login_challenges (token_hash, user_id, purpose, expires_at) VALUES (?, ?, ?, ?)",
                     (_hash(token), user_id, purpose, now + CHALLENGE_SECONDS))
    return {"two_factor": purpose, "challenge": token, "expires_at": now + CHALLENGE_SECONDS}


def challenge(conn, token: str, purpose: str) -> dict:
    row = conn.execute("SELECT token_hash, user_id, purpose, expires_at, tries, pending_secret FROM login_challenges "
                       "WHERE token_hash = ?", (_hash(token),)).fetchone()
    if row is None or row["expires_at"] <= _now() or row["purpose"] != purpose or row["tries"] >= CHALLENGE_TRIES:
        raise TwoFactorError("this login has expired, start again")
    return dict(row)


def count_wrong_code(conn, ch: dict) -> None:
    with conn:
        conn.execute("UPDATE login_challenges SET tries = tries + 1 WHERE token_hash = ?", (ch["token_hash"],))


def end_challenge(conn, ch: dict) -> None:
    with conn:
        conn.execute("DELETE FROM login_challenges WHERE token_hash = ?", (ch["token_hash"],))


def begin_setup(conn, ch: dict, email: str) -> dict:
    """A new secret for a setup challenge (asking again replaces it)."""
    secret = new_secret()
    with conn:
        conn.execute("UPDATE login_challenges SET pending_secret = ? WHERE token_hash = ?", (secret, ch["token_hash"]))
    uri = otpauth_uri(email, secret)
    return {"secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)), "otpauth_uri": uri,
            "qr_svg": qr_svg(uri)}


# ------------------------------------------------------------------ command line
def _main(argv) -> int:
    if len(argv) != 3 or argv[1] != "reset":
        print(__doc__)
        return 2
    import data_layer as dl
    conn = dl.connect(os.environ.get("MM_DB", "mining_match.db"))
    try:
        row = conn.execute("SELECT id FROM users WHERE email = ? AND role = 'admin'", (argv[2].strip().lower(),)).fetchone()
        if row is None:
            print(f"no admin account with the email {argv[2]}", file=sys.stderr)
            return 1
        reset(conn, row["id"])
        conn.execute("INSERT INTO audit_log (user_id, role, action, outcome, resource_type, resource_id, detail) "
                     "VALUES (NULL, 'server', 'user.reset_two_factor', 'allowed', 'user', ?, 'from the command line')",
                     (row["id"],))
        conn.commit()
        print(f"two-factor login reset for {argv[2]}: they'll set it up again at their next login")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
