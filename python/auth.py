"""Mining Match - sign-up, login, sessions, email confirmation and password reset.

Standard library only. Works with any mailer that has send(to, subject, body).

Security choices
  * Passwords are hashed with scrypt (memory-hard) and a random salt per password. The
    hash string records its own settings, so they can be raised later without breaking
    existing logins.
  * Session tokens and emailed tokens are 256-bit random values. Only their SHA-256 hash is
    stored, so a stolen copy of the database can't be used to log in or reset passwords.
  * Sessions end after SESSION_IDLE_SECONDS without use, and after SESSION_MAX_SECONDS
    regardless. Resetting a password or suspending an account ends all sessions.
  * Sign-up and password reset always answer the same way whether or not the email is
    registered, so nobody can use them to find out who has an account.
  * Failed logins are limited per account from each IP address, per IP address, and (much
    more loosely) per account overall. A stranger's wrong guesses therefore can't lock the
    real owner out from their own device; the overall limit only stops a large spread-out
    attack on one account.
  * Emailed links put the token after "#", which browsers never send to any server or
    include in the Referer header, so it can't leak into logs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from typing import Optional, Protocol

import data_layer as dl

# ---------------------------------------------------------------- settings
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MAX_SCRYPT_N = 2 ** 20  # refuse stored hashes asking for more work than this
MIN_PASSWORD, MAX_PASSWORD = 10, 128

SESSION_IDLE_SECONDS = 7 * 24 * 3600
SESSION_MAX_SECONDS = 30 * 24 * 3600
VERIFY_TOKEN_SECONDS = 48 * 3600
RESET_TOKEN_SECONDS = 3600
INVITE_TOKEN_SECONDS = 48 * 3600

# (limit, window in seconds)
LOGIN_FAILS_PER_EMAIL_AND_IP = (5, 15 * 60)  # one account guessed at from one address
LOGIN_FAILS_PER_IP = (30, 15 * 60)  # one address guessing at many accounts
LOGIN_FAILS_PER_EMAIL = (100, 60 * 60)  # one account guessed at from many addresses
RESET_REQUESTS_PER_EMAIL = (3, 3600)
EMAILS_PER_IP = (20, 3600)  # sign-ups, resends and reset requests from one address
RESENDS_PER_EMAIL = (3, 3600)
# Employer candidate searches and rankings: generous for real recruiting, but stops one
# employer sweeping the whole candidate pool with thousands of varied searches.
EMPLOYER_SEARCHES_PER_HOUR = (100, 3600)
DATA_EXPORTS_PER_HOUR = (5, 3600)  # each export packs every uploaded file
EMPLOYER_SEARCHES_PER_DAY = (500, 24 * 3600)

COMMON_PASSWORDS = frozenset({
    "password123", "password1234", "qwertyuiop", "1234567890", "123456789a", "iloveyou12",
    "letmein123", "welcome123", "minematch1", "miningmatch", "abcdefghij", "passw0rd123",
})
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clock() -> int:
    """Current time in unix seconds (tests replace this)."""
    return int(time.time())


# ------------------------------------------------------------------ errors
class AuthError(Exception):
    """Base class. Messages are safe to show to the user."""


class InvalidCredentials(AuthError):
    pass


class EmailNotVerified(AuthError):
    pass


class AccountSuspended(AuthError):
    pass


class InvalidToken(AuthError):
    pass


class TooManyAttempts(AuthError):
    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


# ------------------------------------------------------------------ mailers
class Mailer(Protocol):
    """attachments: optional list of (filename, bytes, mime type), e.g. an invoice PDF."""

    def send(self, to: str, subject: str, body: str, attachments: Optional[list] = None) -> None: ...


class Outbox:
    """Keeps messages in memory. For development and tests."""

    def __init__(self) -> None:
        self.messages: list = []

    def send(self, to: str, subject: str, body: str, attachments: Optional[list] = None) -> None:
        self.messages.append({"to": to, "subject": subject, "body": body, "attachments": list(attachments or [])})


class ConsoleMailer:
    """Development only: prints emails to the console instead of sending them."""

    def send(self, to: str, subject: str, body: str, attachments: Optional[list] = None) -> None:
        files = "".join(f"\n[attached: {name}, {len(data)} bytes]" for name, data, _ in (attachments or []))
        print(f"\n--- email to {to}: {subject}\n{body}{files}\n---\n", flush=True)


class SMTPMailer:
    """Sends through an SMTP server (e.g. Amazon SES, Postmark, Microsoft 365) using STARTTLS.

    The mail server's certificate and name are checked before anything secret is sent. Python's
    smtplib doesn't do this by default (a bare starttls() accepts any certificate), which would
    let anyone on the network path read the emails, including password-reset links and invoices,
    and the mail account's password. If the check fails, send() raises ssl.SSLError and nothing
    is sent. tls_context is for a relay with its own certificate authority (and for tests).
    """

    def __init__(self, host: str, port: int, username: str, password: str, sender: str,
                 tls_context: Optional[ssl.SSLContext] = None):
        self.host, self.port, self.username, self.password, self.sender = host, port, username, password, sender
        self.tls_context = tls_context

    def send(self, to: str, subject: str, body: str, attachments: Optional[list] = None) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        for name, data, mime in attachments or []:
            maintype, subtype = mime.split("/", 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
        context = self.tls_context or ssl.create_default_context()  # verifies the certificate and host name
        with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
            smtp.starttls(context=context)
            smtp.login(self.username, self.password)
            smtp.send_message(msg)


def mailer_from_env(env=os.environ) -> Mailer:
    """SMTP when MM_SMTP_HOST is set, otherwise print emails to the console."""
    if env.get("MM_SMTP_HOST"):
        return SMTPMailer(env["MM_SMTP_HOST"], int(env.get("MM_SMTP_PORT", "587")), env["MM_SMTP_USER"],
                          env["MM_SMTP_PASSWORD"], env["MM_MAIL_FROM"])
    return ConsoleMailer()


# ----------------------------------------------------------------- passwords
def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def check_password_rules(password: str, email: str = "") -> None:
    """Raise ValueError if the password is too weak."""
    if not isinstance(password, str) or not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise ValueError(f"password must be {MIN_PASSWORD} to {MAX_PASSWORD} characters")
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or len(set(password)) < 4:
        raise ValueError("password is too easy to guess")
    local = email.split("@")[0].lower()
    if lowered == email.lower() or (len(local) >= 4 and local in lowered):
        raise ValueError("password must not contain your email address")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    try:
        scheme, n, r, p, salt, key = str(stored).split("$")
        n, r, p = int(n), int(r), int(p)
        if scheme != "scrypt" or not (2 <= n <= MAX_SCRYPT_N and 1 <= r <= 32 and 1 <= p <= 16):
            return False
        expected = base64.b64decode(key, validate=True)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt, validate=True),
                                n=n, r=r, p=p, dklen=len(expected), maxmem=256 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


_dummy_hash: Optional[str] = None


def _waste_time_like_a_real_check(password: str) -> None:
    """Make 'no such user' take as long as 'wrong password', so timing reveals nothing."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _dummy_hash)


# ------------------------------------------------------------- rate limiting
def _count(conn, bucket: str, key: str, window: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM rate_events WHERE bucket = ? AND key = ? AND at > ?",
        (bucket, key, clock() - window),
    ).fetchone()[0]


def _record(conn, bucket: str, key: str) -> None:
    now = clock()
    with conn:
        conn.execute("INSERT INTO rate_events (bucket, key, at) VALUES (?, ?, ?)", (bucket, key, now))
        conn.execute("DELETE FROM rate_events WHERE at < ?", (now - 2 * 24 * 3600,))


def _limit(conn, bucket: str, key: str, rule: tuple, message: str) -> None:
    limit, window = rule
    if _count(conn, bucket, key, window) >= limit:
        oldest = conn.execute(
            "SELECT MIN(at) FROM (SELECT at FROM rate_events WHERE bucket = ? AND key = ? AND at > ? "
            "ORDER BY at DESC LIMIT ?)",
            (bucket, key, clock() - window, limit),
        ).fetchone()[0]
        raise TooManyAttempts(message, retry_after=max(1, oldest + window - clock()))


def limit_data_export(conn, user_id: str) -> None:
    """Count one self-service data export; raise TooManyAttempts over the limit."""
    _limit(conn, "data_export", user_id, DATA_EXPORTS_PER_HOUR, "too many data downloads, try again later")
    _record(conn, "data_export", user_id)


def limit_employer_search(conn, employer_id: str) -> None:
    """Count one candidate search or ranking by an employer; raise TooManyAttempts over the limit."""
    message = "too many candidate searches, try again later"
    _limit(conn, "employer_search", employer_id, EMPLOYER_SEARCHES_PER_HOUR, message)
    _limit(conn, "employer_search", employer_id, EMPLOYER_SEARCHES_PER_DAY, message)
    _record(conn, "employer_search", employer_id)


# ------------------------------------------------------------------- tokens
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_token(conn, user_id: str, purpose: str, seconds: int) -> str:
    token = secrets.token_urlsafe(32)
    with conn:
        conn.execute("DELETE FROM auth_tokens WHERE user_id = ? AND purpose = ?", (user_id, purpose))
        conn.execute(
            "INSERT INTO auth_tokens (token_hash, user_id, purpose, expires_at) VALUES (?, ?, ?, ?)",
            (_hash_token(token), user_id, purpose, clock() + seconds),
        )
    return token


def _token_user(conn, token: str, purpose: str) -> str:
    row = conn.execute(
        "SELECT user_id, expires_at, used_at FROM auth_tokens WHERE token_hash = ? AND purpose = ?",
        (_hash_token(str(token or "")), purpose),
    ).fetchone()
    if row is None or row["used_at"] is not None or row["expires_at"] <= clock():
        raise InvalidToken("this link is invalid or has expired")
    return row["user_id"]


def _use_token(conn, token: str) -> None:
    conn.execute("UPDATE auth_tokens SET used_at = ? WHERE token_hash = ?", (clock(), _hash_token(token)))


def _normalise_email(email: str) -> str:
    email = str(email or "").strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise ValueError("a valid email address is required")
    return email


# --------------------------------------------------------------- sign-up
def _send_verification(conn, mailer: Mailer, base_url: str, user_id: str, email: str) -> None:
    token = _issue_token(conn, user_id, "verify_email", VERIFY_TOKEN_SECONDS)
    mailer.send(email, "Confirm your Mining Match account",
                f"Confirm your email address to finish setting up your account:\n\n"
                f"{base_url}/#/verify-email?token={token}\n\n"
                f"This link expires in 48 hours. If you didn't sign up, ignore this email.")


def _send_already_registered(mailer: Mailer, base_url: str, email: str) -> None:
    mailer.send(email, "You already have a Mining Match account",
                f"Someone tried to sign up with this email address, which already has an account.\n\n"
                f"If it was you, log in, or reset your password here:\n{base_url}/#/forgot-password\n\n"
                f"If it wasn't you, you can ignore this email.")


def register(conn, mailer: Mailer, base_url: str, *, role: str, email: str, password: str,
             ip: str, profile: dict) -> None:
    """Create a candidate or employer account and email a confirmation link.

    Returns nothing either way: if the email is already registered, its owner gets an email
    saying so instead, and the caller answers exactly as for a new account.
    profile: candidate {name, location?, phone?}; employer {company_name, contact_name?, phone?, location?}.
    """
    if role not in ("candidate", "employer"):
        raise ValueError("role must be candidate or employer")
    email = _normalise_email(email)
    check_password_rules(password, email)
    required = "name" if role == "candidate" else "company_name"
    allowed = {"name", "location", "phone"} if role == "candidate" else {"company_name", "contact_name", "phone", "location"}
    if set(profile) - allowed:
        raise ValueError(f"unknown profile fields: {sorted(set(profile) - allowed)}")
    if not str(profile.get(required) or "").strip():
        raise ValueError(f"{required} is required")
    _limit(conn, "email_ip", ip, EMAILS_PER_IP, "too many requests from this address, try again later")
    _record(conn, "email_ip", ip)

    # Hash before checking, so a taken email takes as long to answer as a new one (otherwise
    # the response time would show who has an account).
    password_hash = hash_password(password)
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        _send_already_registered(mailer, base_url, email)
        return
    try:
        if role == "candidate":
            candidate_id = dl.register_candidate(conn, email, password_hash=password_hash, **profile)
            user_id = conn.execute("SELECT user_id FROM candidates WHERE id = ?", (candidate_id,)).fetchone()[0]
        else:
            employer_id = dl.register_employer(conn, email, password_hash=password_hash, **profile)
            user_id = conn.execute("SELECT user_id FROM employers WHERE id = ?", (employer_id,)).fetchone()[0]
    except dl.DuplicateError:  # registered by someone else a moment ago
        _send_already_registered(mailer, base_url, email)
        return
    _send_verification(conn, mailer, base_url, user_id, email)


def verify_email(conn, token: str) -> None:
    user_id = _token_user(conn, token, "verify_email")
    with conn:
        _use_token(conn, token)
        conn.execute("UPDATE users SET email_verified_at = COALESCE(email_verified_at, CURRENT_TIMESTAMP) "
                     "WHERE id = ?", (user_id,))


def resend_verification(conn, mailer: Mailer, base_url: str, email: str, ip: str) -> None:
    """Send a new confirmation link if the account exists and isn't confirmed. Same answer either way."""
    email = _normalise_email(email)
    _limit(conn, "email_ip", ip, EMAILS_PER_IP, "too many requests from this address, try again later")
    _limit(conn, "resend", email, RESENDS_PER_EMAIL, "too many requests for this email, try again later")
    _record(conn, "email_ip", ip)
    _record(conn, "resend", email)
    row = conn.execute("SELECT id, email_verified_at, is_active FROM users WHERE email = ?", (email,)).fetchone()
    if row and row["email_verified_at"] is None and row["is_active"]:
        _send_verification(conn, mailer, base_url, row["id"], email)


# ------------------------------------------------------------------ login
def login(conn, email: str, password: str, ip: str) -> dict:
    """Check the password and start a session. Returns {token, expires_at, user_id, role}."""
    try:
        email = _normalise_email(email)
    except ValueError:
        raise InvalidCredentials("wrong email or password") from None
    _limit(conn, "login_ip", ip, LOGIN_FAILS_PER_IP, "too many failed logins from this address, try again later")
    _limit(conn, "login_email_ip", f"{email} {ip}", LOGIN_FAILS_PER_EMAIL_AND_IP,
           "too many failed logins for this account, try again later")
    _limit(conn, "login_email", email, LOGIN_FAILS_PER_EMAIL, "too many failed logins for this account, try again later")
    row = conn.execute(
        "SELECT id, role, password_hash, is_active, email_verified_at FROM users WHERE email = ?", (email,)
    ).fetchone()
    if row is None or not row["password_hash"]:
        _waste_time_like_a_real_check(str(password))
        ok = False
    else:
        ok = verify_password(str(password), row["password_hash"])
    if not ok:
        _record(conn, "login_ip", ip)
        _record(conn, "login_email_ip", f"{email} {ip}")
        _record(conn, "login_email", email)
        raise InvalidCredentials("wrong email or password")
    # Only someone with the right password learns about the account's state.
    if not row["is_active"]:
        raise AccountSuspended("this account is suspended, contact support")
    if row["email_verified_at"] is None:
        raise EmailNotVerified("confirm your email address first (check your inbox)")
    token, now = secrets.token_urlsafe(32), clock()
    with conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND expires_at <= ?", (row["id"], now))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
            (_hash_token(token), row["id"], now, now + SESSION_MAX_SECONDS, now),
        )
    return {"token": token, "expires_at": now + SESSION_MAX_SECONDS, "user_id": row["id"], "role": row["role"]}


def session_user(conn, token: Optional[str]) -> Optional[str]:
    """The user id for a valid session token, or None. Keeps the session alive while in use."""
    if not token:
        return None
    row = conn.execute(
        "SELECT token_hash, user_id, expires_at, last_seen_at FROM sessions WHERE token_hash = ?",
        (_hash_token(token),),
    ).fetchone()
    now = clock()
    if row is None:
        return None
    if row["expires_at"] <= now or now - row["last_seen_at"] >= SESSION_IDLE_SECONDS:
        with conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (row["token_hash"],))
        return None
    if now - row["last_seen_at"] > 60:
        with conn:
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now, row["token_hash"]))
    return row["user_id"]


def logout(conn, token: str) -> None:
    with conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(str(token or "")),))


def change_password(conn, token: str, current_password: str, new_password: str) -> None:
    """Logged-in change. Ends every other session."""
    user_id = session_user(conn, token)
    if user_id is None:
        raise InvalidCredentials("not logged in")
    row = conn.execute("SELECT email, password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if not verify_password(str(current_password), row["password_hash"]):
        raise InvalidCredentials("current password is wrong")
    check_password_rules(new_password, row["email"])
    with conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, _hash_token(token)))


# ----------------------------------------------------------- password reset
def request_password_reset(conn, mailer: Mailer, base_url: str, email: str, ip: str) -> None:
    """Email a reset link if the account exists and is active. Same answer either way."""
    email = _normalise_email(email)
    _limit(conn, "email_ip", ip, EMAILS_PER_IP, "too many requests from this address, try again later")
    _limit(conn, "reset", email, RESET_REQUESTS_PER_EMAIL, "too many reset requests for this email, try again later")
    _record(conn, "email_ip", ip)
    _record(conn, "reset", email)
    row = conn.execute("SELECT id, is_active FROM users WHERE email = ?", (email,)).fetchone()
    if row and row["is_active"]:
        token = _issue_token(conn, row["id"], "reset_password", RESET_TOKEN_SECONDS)
        mailer.send(email, "Reset your Mining Match password",
                    f"Use this link to choose a new password:\n\n{base_url}/#/reset-password?token={token}\n\n"
                    f"It expires in 1 hour. If you didn't ask for this, ignore this email; "
                    f"your password hasn't changed.")


def reset_password(conn, token: str, new_password: str) -> None:
    """Set a new password from an emailed link. Also confirms the email and ends all sessions."""
    user_id = _token_user(conn, token, "reset_password")
    email = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()["email"]
    check_password_rules(new_password, email)  # checked before using up the link
    with conn:
        _use_token(conn, token)
        conn.execute(
            "UPDATE users SET password_hash = ?, email_verified_at = COALESCE(email_verified_at, CURRENT_TIMESTAMP) "
            "WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def send_admin_invite(conn, mailer: Mailer, base_url: str, user_id: str) -> None:
    """Email a new admin a link to set their password (which also confirms their email)."""
    email = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()["email"]
    token = _issue_token(conn, user_id, "reset_password", INVITE_TOKEN_SECONDS)
    mailer.send(email, "You've been added as a Mining Match administrator",
                f"Set your password to get started:\n\n{base_url}/#/reset-password?token={token}\n\n"
                f"This link expires in 48 hours.")
