"""Mining Match - licence expiry reminders for candidates.

Once a day, send_expiry_reminders() emails candidates whose licences are about to expire:
90, 60 and 30 days before the expiry date, and once just after it has expired. Rules:
  * One email per candidate per run, listing every licence that reached a new stage.
  * Never the same reminder twice: each one sent is logged against the licence's expiry date,
    so renewing a licence (a new expiry date) starts its reminders afresh.
  * Catch-up without spamming: if the job missed a day, or a licence is added with 45 days
    left, only the latest stage reached is sent (the 60-day one), not every stage since.
  * Expired licences are mentioned once, and only within EXPIRED_GRACE_DAYS of expiring.
  * Only active accounts with a confirmed email that haven't turned reminders off. Every email
    has a link that turns them off without logging in.

licence_rules() gives the usual renewal cycles (from db/catalog_seed.json) so the profile can
suggest an expiry date from the date someone got a licence.
"""
from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from typing import Optional

import access_control as ac
import data_layer as dl

STAGES = (30, 60, 90)       # days before expiry, nearest first
EXPIRED = 0                 # stage logged once a licence has expired
EXPIRED_GRACE_DAYS = 14     # don't announce expiries older than this
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def local_today(tz_name: Optional[str] = None) -> date:
    """Today's date where the business is (MM_TIMEZONE, default Australia/Brisbane)."""
    name = tz_name or os.environ.get("MM_TIMEZONE", "Australia/Brisbane")
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(name)
    except Exception:
        tz = dt_timezone(timedelta(hours=10))  # Queensland: UTC+10 all year
    return datetime.now(tz).date()


def stage_for(days_left: int) -> Optional[int]:
    """Which reminder a licence with this many days left has reached (None: no reminder)."""
    if days_left < 0:
        return EXPIRED if -days_left <= EXPIRED_GRACE_DAYS else None
    for stage in STAGES:
        if days_left <= stage:
            return stage
    return None


def _fmt(d: date) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def _describe(name: str, expiry: date, days_left: int) -> str:
    if days_left < 0:
        return f"{name}: expired {_fmt(expiry)}"
    if days_left == 0:
        return f"{name}: expires today ({_fmt(expiry)})"
    return f"{name}: expires {_fmt(expiry)} (in {days_left} day{'s' if days_left != 1 else ''})"


def _subject(items: list) -> str:
    if len(items) == 1:
        it = items[0]
        if it["days_left"] < 0:
            return f"Your {it['licence']} has expired"
        if it["days_left"] == 0:
            return f"Your {it['licence']} expires today"
        return f"Your {it['licence']} expires in {it['days_left']} day{'s' if it['days_left'] != 1 else ''}"
    if any(it["days_left"] < 0 for it in items):
        return f"{len(items)} of your licences need attention"
    return f"{len(items)} of your licences expire soon"


def _token(conn, candidate_id: str) -> str:
    row = conn.execute("SELECT reminder_token FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
    if row["reminder_token"]:
        return row["reminder_token"]
    token = secrets.token_urlsafe(24)
    with conn:
        conn.execute("UPDATE candidates SET reminder_token = ? WHERE id = ?", (token, candidate_id))
    return token


def due_reminders(conn, today: date) -> dict:
    """{candidate_id: {"name", "email", "items": [...]}} for reminders not yet sent."""
    rows = conn.execute(
        "SELECT c.id AS candidate_id, c.name, u.email, cl.item_id, ci.name AS licence, cl.expiry_date "
        "FROM candidate_licences cl JOIN candidates c ON c.id = cl.candidate_id "
        "JOIN users u ON u.id = c.user_id JOIN catalog_items ci ON ci.id = cl.item_id "
        "WHERE cl.expiry_date IS NOT NULL AND c.expiry_reminders = 1 AND u.is_active = 1 "
        "AND u.email_verified_at IS NOT NULL AND cl.expiry_date BETWEEN ? AND ? "
        "ORDER BY c.id, cl.expiry_date, ci.name",
        ((today - timedelta(days=EXPIRED_GRACE_DAYS)).isoformat(), (today + timedelta(days=max(STAGES))).isoformat()),
    ).fetchall()
    sent = {(r["candidate_id"], r["item_id"], r["expiry_date"], r["stage"]) for r in conn.execute(
        "SELECT candidate_id, item_id, expiry_date, stage FROM licence_reminders WHERE expiry_date >= ?",
        ((today - timedelta(days=EXPIRED_GRACE_DAYS)).isoformat(),))}
    due: dict = {}
    for r in rows:
        expiry = date.fromisoformat(r["expiry_date"])
        days_left = (expiry - today).days
        stage = stage_for(days_left)
        if stage is None or (r["candidate_id"], r["item_id"], r["expiry_date"], stage) in sent:
            continue
        person = due.setdefault(r["candidate_id"], {"name": r["name"], "email": r["email"], "items": []})
        person["items"].append({"item_id": r["item_id"], "licence": r["licence"], "expiry_date": r["expiry_date"],
                                "days_left": days_left, "stage": stage})
    return due


def send_expiry_reminders(conn, mailer, base_url: str, principal=None, *, today: Optional[date] = None) -> dict:
    """Email everyone with licences that reached a reminder stage. Safe to run more than once a day.

    principal=None is the daily job on the server (python -m daily); from the app, admins only.
    Returns {"emails": sent, "licences": covered, "failed": emails that couldn't be sent}.
    A failed email isn't logged, so it's tried again on the next run.
    """
    if principal is not None:
        principal = ac.authorize(conn, principal, "reminders.run", "admin")
    today = today or local_today()
    base_url = base_url.rstrip("/")
    emails = licences = failed = 0
    for candidate_id, person in due_reminders(conn, today).items():
        items = person["items"]
        body = [
            f"Hello {person['name'].split(' ')[0] or person['name']},",
            "",
            "These licences on your Mining Match profile need renewing:" if len(items) > 1
            else "This licence on your Mining Match profile needs renewing:",
            "",
            *[f"  - {_describe(it['licence'], date.fromisoformat(it['expiry_date']), it['days_left'])}" for it in items],
            "",
            "Employers can't count an expired licence, so jobs that need it will show you as unable to start.",
            "",
            f"Renewed already? Update the expiry date in your profile: {base_url}/#/profile",
            "",
            f"Don't want these reminders? Turn them off: {base_url}/#/unsubscribe?token={_token(conn, candidate_id)}",
            "You can turn them back on in your profile.",
            "",
            "Mining Match",
        ]
        try:
            mailer.send(person["email"], _subject(items), "\n".join(body))
        except Exception:
            failed += 1
            continue
        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO licence_reminders (candidate_id, item_id, expiry_date, stage, sent_on) "
                "VALUES (?, ?, ?, ?, ?)",
                [(candidate_id, it["item_id"], it["expiry_date"], it["stage"], today.isoformat()) for it in items])
        emails += 1
        licences += len(items)
    ac.audit_allowed(conn, principal, "reminders.run", None, None,
                     f"{emails} emails, {licences} licences" + (f", {failed} failed" if failed else ""))
    return {"emails": emails, "licences": licences, "failed": failed}


def unsubscribe(conn, token: str) -> None:
    """Turn reminders off from the link in an email (no login needed; the token is the key)."""
    token = str(token or "").strip()
    if not 20 <= len(token) <= 100:
        raise ValueError("This link isn't valid. You can turn reminders off in your profile instead.")
    row = conn.execute("SELECT id FROM candidates WHERE reminder_token = ?", (token,)).fetchone()
    if row is None:
        raise ValueError("This link isn't valid. You can turn reminders off in your profile instead.")
    with conn:
        conn.execute("UPDATE candidates SET expiry_reminders = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],))
    ac.audit_allowed(conn, None, "reminders.unsubscribe", "candidate", row["id"])


def list_recent_reminders(conn, principal, *, limit: int = 100) -> list:
    """Admin: the latest reminders sent, newest first."""
    action = "reminders.list"
    p = ac.authorize(conn, principal, action, "admin")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    rows = conn.execute(
        "SELECT r.sent_on, r.candidate_id, c.name AS candidate_name, ci.name AS licence, r.expiry_date, r.stage "
        "FROM licence_reminders r JOIN candidates c ON c.id = r.candidate_id "
        "JOIN catalog_items ci ON ci.id = r.item_id ORDER BY r.sent_on DESC, c.name, ci.name LIMIT ?", (limit,)).fetchall()
    ac.audit_allowed(conn, p, action, None, None, f"{len(rows)} rows")
    return [dict(r) for r in rows]


def licence_rules(conn, principal) -> list:
    """Usual renewal cycles and notes for common licences. Any logged-in user."""
    ac.authorize(conn, principal, "catalog.rules", "candidate", "employer", "admin")
    rows = conn.execute(
        "SELECT name, renew_years, renew_note FROM catalog_items WHERE kind = 'licence' "
        "AND (renew_years IS NOT NULL OR renew_note IS NOT NULL) ORDER BY normalized_name").fetchall()
    return [{"name": r["name"], "renew_years": r["renew_years"], "note": r["renew_note"]} for r in rows]
