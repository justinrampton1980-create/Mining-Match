"""Security monitoring (launch blocker B5): emails MM_ALERT_EMAIL when something needs attention.

Two parts:

1. In the app (create_app(alert_email=...)): an alert for every kind of server error (500),
   and when rate limits start refusing people (password guessing, too many wrong codes...).
   Each kind of alert is sent at most once an hour.

2. A check every 5 minutes from cron (`python -m monitoring`):
     site      the website answers on MM_BASE_URL/health
     https     its certificate has more than 14 days left
     disk      the data folder's disk has room (at least 10% and 2 GB free)
     backups   last night's encrypted backup exists (newest under 26 hours old)
     scanner   ClamAV answers (uploads are refused while it doesn't)
     denials   no account has been refused 20+ times in 15 minutes (probing for others' data)
     logins    fewer than 50 failed logins site-wide in 15 minutes (password guessing)
   plus a notice to MM_ALERT_EMAIL for every new admin login, so a stolen admin login shows up.
   A failing check alerts once, reminds every 6 hours while it lasts, and says when it's fixed.

What this can't see: the whole server being down (it runs on the server). Use an outside
uptime monitor on https://YOUR-DOMAIN/health as well; docs/DEPLOYMENT.md says how.

Alerts carry no personal details beyond an account id or IP address where that is the point.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import auth
import data_layer as dl

APP_ALERTS_PER_HOUR = (1, 3600)  # per kind of in-app alert
REMIND_SECONDS = 6 * 3600
CERT_DAYS = 14
DISK_MIN_FRACTION, DISK_MIN_BYTES = 0.10, 2 * 1024 ** 3
BACKUP_MAX_AGE = 26 * 3600
DENIALS_PER_USER = (20, 15 * 60)
FAILED_LOGINS_SITE = (50, 15 * 60)


def now() -> float:
    return time.time()


# ------------------------------------------------------------------ in the app
class AppAlerts:
    """Used by the API: email an alert, at most once an hour per kind (counted in the database)."""

    def __init__(self, db_path: str, mailer: auth.Mailer, to: Optional[str]):
        self.db_path, self.mailer, self.to = db_path, mailer, (to or "").strip() or None

    def send(self, kind: str, subject: str, body: str) -> bool:
        if not self.to:
            return False
        try:
            conn = dl.connect(self.db_path)
            try:
                limit, window = APP_ALERTS_PER_HOUR
                if auth._count(conn, "alert", kind, window) >= limit:
                    return False
                auth._record(conn, "alert", kind)
            finally:
                conn.close()
            self.mailer.send(self.to, f"Mining Match alert: {subject}", body + "\n\nMore of the same within the "
                             "hour won't be emailed. Details are in the server log (journalctl -u mining-match).")
            return True
        except Exception:  # an alert must never break the request that raised it
            return False


# ------------------------------------------------------------------ the 5-minute check
class Check:
    def __init__(self, name: str, ok: bool, detail: str):
        self.name, self.ok, self.detail = name, ok, detail


def check_site(base_url: str, fetch: Optional[Callable] = None) -> Check:
    url = base_url.rstrip("/") + "/health"
    try:
        if fetch:
            status, body = fetch(url)
        else:
            with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 (our own address)
                status, body = r.status, r.read(1000)
        if status == 200 and json.loads(body).get("ok") is True:
            return Check("site", True, f"{url} answers")
        return Check("site", False, f"{url} answered {status}")
    except Exception as e:
        return Check("site", False, f"{url} didn't answer: {e}")


def check_certificate(base_url: str, expiry: Optional[Callable] = None) -> Optional[Check]:
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        return None
    try:
        if expiry:
            expires = expiry(parsed.hostname)
        else:
            ctx = ssl.create_default_context()
            with socket.create_connection((parsed.hostname, parsed.port or 443), timeout=10) as raw, \
                    ctx.wrap_socket(raw, server_hostname=parsed.hostname) as s:
                expires = ssl.cert_time_to_seconds(s.getpeercert()["notAfter"])
        days = (expires - now()) / 86400
        if days < CERT_DAYS:
            return Check("https", False, f"the HTTPS certificate expires in {days:.0f} days (certbot should renew it; "
                         "run: sudo certbot renew)")
        return Check("https", True, f"certificate valid for {days:.0f} more days")
    except Exception as e:
        return Check("https", False, f"couldn't check the HTTPS certificate: {e}")


def check_disk(path: str) -> Check:
    usage = shutil.disk_usage(path)
    free_fraction = usage.free / usage.total
    gb = usage.free / 1024 ** 3
    if free_fraction < DISK_MIN_FRACTION or usage.free < DISK_MIN_BYTES:
        return Check("disk", False, f"only {gb:.1f} GB ({free_fraction:.0%}) free on the disk holding {path}")
    return Check("disk", True, f"{gb:.1f} GB ({free_fraction:.0%}) free")


def check_backups(folder: str) -> Check:
    files = sorted(Path(folder).glob("db-*.age"), key=lambda p: p.stat().st_mtime) if Path(folder).is_dir() else []
    if not files:
        return Check("backups", False, f"no encrypted database backups in {folder}")
    age = now() - files[-1].stat().st_mtime
    if age > BACKUP_MAX_AGE:
        return Check("backups", False, f"the newest backup ({files[-1].name}) is {age / 3600:.0f} hours old; "
                     "see /srv/mining-match/backup.log")
    return Check("backups", True, f"newest backup {files[-1].name}, {age / 3600:.0f} hours old")


def check_scanner(socket_path: str, host: Optional[str] = None, port: int = 3310) -> Check:
    try:
        if host:
            s = socket.create_connection((host, port), timeout=10)
        else:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(10)
            try:
                s.connect(socket_path)
            except OSError:
                s.close()
                raise
        with s:
            s.sendall(b"zPING\0")
            reply = s.recv(100).rstrip(b"\0")
        if reply == b"PONG":
            return Check("scanner", True, "ClamAV answers")
        return Check("scanner", False, f"ClamAV answered {reply[:50]!r}; uploads are being refused")
    except OSError as e:
        return Check("scanner", False, f"ClamAV isn't answering ({e}); uploads are being refused. "
                     "Try: sudo systemctl restart clamav-daemon")


def check_denials(conn) -> Check:
    limit, window = DENIALS_PER_USER
    rows = conn.execute(
        "SELECT user_id, role, COUNT(*) AS n FROM audit_log WHERE outcome = 'denied' AND user_id IS NOT NULL "
        "AND at >= datetime('now', ?) GROUP BY user_id HAVING n >= ? ORDER BY n DESC",
        (f"-{window} seconds", limit)).fetchall()
    if rows:
        who = ", ".join(f"{r['role']} account {r['user_id']} ({r['n']} times)" for r in rows[:5])
        return Check("denials", False, f"refused actions in the last {window // 60} minutes: {who}. "
                     "This can mean someone probing for other people's data. Check Admin > Activity log, "
                     "and suspend the account if it looks deliberate.")
    return Check("denials", True, "no unusual refused actions")


def check_failed_logins(conn) -> Check:
    limit, window = FAILED_LOGINS_SITE
    n = conn.execute("SELECT COUNT(*) FROM rate_events WHERE bucket = 'login_ip' AND at > ?",
                     (int(now()) - window,)).fetchone()[0]
    if n >= limit:
        top = conn.execute("SELECT key, COUNT(*) AS n FROM rate_events WHERE bucket = 'login_ip' AND at > ? "
                           "GROUP BY key ORDER BY n DESC LIMIT 3", (int(now()) - window,)).fetchall()
        where = ", ".join(f"{r['key']} ({r['n']})" for r in top)
        return Check("logins", False, f"{n} failed logins in the last {window // 60} minutes (most from: {where}). "
                     "Possible password guessing; the rate limits are slowing it down.")
    return Check("logins", True, f"{n} failed logins in the last {window // 60} minutes")


# ------------------------------------------------------------------ state and alerting
def _state(conn, key: str) -> Optional[dict]:
    row = conn.execute("SELECT value FROM monitor_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def _set_state(conn, key: str, value: dict) -> None:
    with conn:
        conn.execute("INSERT INTO monitor_state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                     (key, json.dumps(value)))


def new_admin_logins(conn) -> list:
    """Admin sessions started since the last run (the first run only remembers where it is)."""
    seen = _state(conn, "admin_sessions") or {}
    since = seen.get("since")
    current = int(now())
    rows = [] if since is None else conn.execute(
        "SELECT u.email, s.created_at FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE u.role = 'admin' AND s.created_at > ? AND s.created_at <= ? ORDER BY s.created_at", (since, current)).fetchall()
    _set_state(conn, "admin_sessions", {"since": current})
    return [(r["email"], datetime.fromtimestamp(r["created_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")) for r in rows]


def run(conn, checks: list, mailer: auth.Mailer, to: Optional[str], logins: list) -> list:
    """Send what needs sending. Returns the subjects of the emails sent."""
    sent = []

    def email(subject, body):
        if to:
            mailer.send(to, f"Mining Match: {subject}", body)
        sent.append(subject)

    t = int(now())
    for c in checks:
        key = f"check:{c.name}"
        before = _state(conn, key) or {"ok": True, "alerted_at": None}
        if not c.ok:
            if before["ok"] or before["alerted_at"] is None or t - before["alerted_at"] >= REMIND_SECONDS:
                email(f"PROBLEM ({c.name})" if before["ok"] else f"STILL A PROBLEM ({c.name})", c.detail)
                before = {"ok": False, "alerted_at": t}
            _set_state(conn, key, {"ok": False, "alerted_at": before["alerted_at"]})
        else:
            if not before["ok"]:
                email(f"fixed ({c.name})", c.detail)
            _set_state(conn, key, {"ok": True, "alerted_at": None})
    if logins:
        lines = "\n".join(f"  {e} at {when}" for e, when in logins)
        email("admin login", f"New admin login(s):\n\n{lines}\n\nIf one wasn't you or a colleague, change that admin's "
              "password, reset their two-factor login and check Admin > Activity log.")
    return sent


def main(env=os.environ) -> int:
    db = env.get("MM_DB", "mining_match.db")
    conn = dl.connect(db)
    try:
        dl.init_schema(conn)
        checks = [check_site(env.get("MM_BASE_URL", "http://localhost:8000"))]
        cert = check_certificate(env.get("MM_BASE_URL", ""))
        if cert:
            checks.append(cert)
        checks.append(check_disk(str(Path(db).resolve().parent)))
        checks.append(check_backups(env.get("MM_BACKUP_DIR", "/srv/mining-match/backups")))
        if env.get("MM_VIRUS_SCAN", "1") != "0":
            checks.append(check_scanner(env.get("MM_CLAMD_SOCKET", "/var/run/clamav/clamd.ctl"),
                                        env.get("MM_CLAMD_HOST") or None, int(env.get("MM_CLAMD_PORT", "3310"))))
        checks += [check_denials(conn), check_failed_logins(conn)]
        to = env.get("MM_ALERT_EMAIL", "").strip() or None
        sent = run(conn, checks, auth.mailer_from_env(env), to, new_admin_logins(conn))
        for c in checks:
            print(f"{'ok  ' if c.ok else 'FAIL'} {c.name}: {c.detail}")
        if not to:
            print("MM_ALERT_EMAIL isn't set: nothing was emailed", file=sys.stderr)
        return 0 if all(c.ok for c in checks) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
