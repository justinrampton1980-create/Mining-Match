"""Everything Mining Match holds about one person, as a ZIP file (launch blocker B2).

Answers privacy access requests (Australian Privacy Principle 12): a user downloads their own
data from their account page, or an admin exports any user's data for a request made by email.

The ZIP holds:
  README.txt   what each part is
  data.json    the account and everything linked to it, in plain JSON
  documents/   the files the candidate uploaded, as uploaded

Left out on purpose: password hashes, session and email-link tokens, the reminder opt-out
token, and storage keys (they're security secrets, not information about the person), and the
placement fee an employer was charged (the employer's business information, not the candidate's).

Employer accounts get their own company details, jobs and fee-terms acceptances. Their
shortlists, introductions and invoices stay in the app: they hold other people's personal
details, which an export file must not carry away.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Optional

import access_control as ac
import data_layer as dl

ACTIVITY_LIMIT = 10_000  # most recent audit entries by the user

README = """Mining Match: your data
=======================

Exported {exported_at} (UTC) for {email}.

data.json holds everything Mining Match stores about this account:

  account          sign-in email, role, when the account was made and confirmed
  sessions         when you logged in and when each session was last used (no passwords or tokens)
  candidate        (candidates) your profile: contact details, location, availability, licences
                   with expiry dates, qualifications, skills, work history and document details
  contact_requests (candidates) employers who asked to see your details, and your answers
  introductions    (candidates) employers who unlocked your contact details, and when
  shortlists       (candidates) employers' shortlists you're on, with their status and notes
  hires            (candidates) jobs employers recorded you as starting
  reminders        (candidates) licence expiry reminders sent to you
  employer         (employers) your company details, jobs and accepted placement terms
  activity         things you did on the site (most recent {limit:,}), for your records

documents/ holds the files you uploaded, as you uploaded them.

To correct anything, edit it on the site or reply to the email this came with.
"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _rows(conn, sql: str, params: tuple = ()) -> list:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _unix(ts: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts is not None else None


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "file").strip("._") or "file"
    return name[-80:]


def _candidate_part(conn, candidate_id: str) -> dict:
    profile = _rows(conn,
        "SELECT name, phone, location, willing_to_relocate, willing_fifo, available_from, summary, "
        "is_active AS visible_to_employers, expiry_reminders, created_at, updated_at "
        "FROM candidates WHERE id = ?", (candidate_id,))[0]
    for k in ("willing_to_relocate", "willing_fifo", "visible_to_employers", "expiry_reminders"):
        profile[k] = bool(profile[k])
    profile["candidate_id"] = candidate_id
    profile["licences"] = _rows(conn,
        "SELECT ci.name, cl.expiry_date, cl.document_id FROM candidate_licences cl "
        "JOIN catalog_items ci ON ci.id = cl.item_id WHERE cl.candidate_id = ? ORDER BY ci.name", (candidate_id,))
    profile["qualifications"] = _rows(conn,
        "SELECT ci.name, cq.awarded_date, cq.institution, cq.document_id FROM candidate_qualifications cq "
        "JOIN catalog_items ci ON ci.id = cq.item_id WHERE cq.candidate_id = ? ORDER BY ci.name", (candidate_id,))
    profile["skills"] = _rows(conn,
        "SELECT ci.name, cs.years FROM candidate_skills cs "
        "JOIN catalog_items ci ON ci.id = cs.item_id WHERE cs.candidate_id = ? ORDER BY ci.name", (candidate_id,))
    profile["work_history"] = _rows(conn,
        "SELECT role, employer_name, start_date, end_date FROM candidate_experience "
        "WHERE candidate_id = ? ORDER BY start_date DESC", (candidate_id,))
    profile["documents"] = _rows(conn,
        "SELECT id, kind, filename, mime_type, size_bytes, verified_at, uploaded_at FROM documents "
        "WHERE candidate_id = ? ORDER BY uploaded_at", (candidate_id,))
    return profile


def _candidate_links(conn, candidate_id: str) -> dict:
    return {
        "contact_requests": _rows(conn,
            "SELECT e.company_name AS employer, j.title AS job, r.status, r.requested_at, r.decided_at "
            "FROM contact_releases r JOIN employers e ON e.id = r.employer_id "
            "LEFT JOIN jobs j ON j.id = r.job_id WHERE r.candidate_id = ? ORDER BY r.requested_at",
            (candidate_id,)),
        "introductions": _rows(conn,
            "SELECT e.company_name AS employer, j.title AS job, i.unlocked_at, i.terms_version "
            "FROM introductions i LEFT JOIN employers e ON e.id = i.employer_id "
            "LEFT JOIN jobs j ON j.id = i.job_id WHERE i.candidate_id = ? ORDER BY i.unlocked_at",
            (candidate_id,)),
        "shortlists": _rows(conn,
            "SELECT e.company_name AS employer, s.name AS shortlist, j.title AS job, se.status, se.notes, "
            "se.score_at_add, se.added_at, se.updated_at FROM shortlist_entries se "
            "JOIN shortlists s ON s.id = se.shortlist_id JOIN employers e ON e.id = s.employer_id "
            "LEFT JOIN jobs j ON j.id = s.job_id WHERE se.candidate_id = ? ORDER BY se.added_at",
            (candidate_id,)),
        "hires": _rows(conn,
            "SELECT e.company_name AS employer, p.job_title AS job, p.start_date, "
            "p.base_salary_cents / 100.0 AS base_salary_aud, p.status, p.reported_at "
            "FROM placements p LEFT JOIN employers e ON e.id = p.employer_id "
            "WHERE p.candidate_id = ? ORDER BY p.start_date", (candidate_id,)),
        "reminders": _rows(conn,
            "SELECT ci.name AS licence, lr.expiry_date, lr.stage AS days_before_expiry, lr.sent_on "
            "FROM licence_reminders lr JOIN catalog_items ci ON ci.id = lr.item_id "
            "WHERE lr.candidate_id = ? ORDER BY lr.sent_on", (candidate_id,)),
    }


def _employer_part(conn, employer_id: str) -> dict:
    company = _rows(conn,
        "SELECT company_name, contact_name, phone, location, abn, billing_email, billing_address, created_at "
        "FROM employers WHERE id = ?", (employer_id,))[0]
    company["employer_id"] = employer_id
    jobs = _rows(conn,
        "SELECT id, title, description, location, start_date, min_experience_years, status, created_at, updated_at "
        "FROM jobs WHERE employer_id = ? ORDER BY created_at", (employer_id,))
    for job in jobs:
        job["requirements"] = _rows(conn,
            "SELECT ci.name, jr.kind, jr.required, jr.min_years FROM job_requirements jr "
            "JOIN catalog_items ci ON ci.id = jr.item_id WHERE jr.job_id = ? ORDER BY jr.kind, ci.name", (job["id"],))
        for r in job["requirements"]:
            r["required"] = bool(r["required"])
        job["relevant_roles"] = [r["role"] for r in _rows(conn,
            "SELECT role FROM job_relevant_roles WHERE job_id = ? ORDER BY role", (job["id"],))]
    company["jobs"] = jobs
    company["accepted_placement_terms"] = _rows(conn,
        "SELECT terms_version, accepted_by, accepted_at FROM terms_acceptances WHERE employer_id = ? "
        "ORDER BY terms_version", (employer_id,))
    return company


def collect(conn, user_id: str) -> tuple[dict, list]:
    """The export's data and the (document id, filename, storage key) of each uploaded file."""
    users = _rows(conn, "SELECT id, email, role, is_active, email_verified_at, created_at FROM users WHERE id = ?",
                  (user_id,))
    if not users:
        raise dl.NotFoundError(f"user not found: {user_id}")
    account = users[0]
    account["is_active"] = bool(account["is_active"])
    data: dict[str, Any] = {"exported_at": _now(), "account": account}
    data["sessions"] = [
        {"started": _unix(s["created_at"]), "last_used": _unix(s["last_seen_at"]), "expires": _unix(s["expires_at"])}
        for s in _rows(conn, "SELECT created_at, last_seen_at, expires_at FROM sessions WHERE user_id = ? "
                             "ORDER BY created_at", (user_id,))]
    files = []
    cand = conn.execute("SELECT id FROM candidates WHERE user_id = ?", (user_id,)).fetchone()
    if cand:
        data["candidate"] = _candidate_part(conn, cand["id"])
        data.update(_candidate_links(conn, cand["id"]))
        files = [(r["id"], r["filename"], r["storage_key"]) for r in conn.execute(
            "SELECT id, filename, storage_key FROM documents WHERE candidate_id = ? ORDER BY uploaded_at", (cand["id"],))]
    emp = conn.execute("SELECT id FROM employers WHERE user_id = ?", (user_id,)).fetchone()
    if emp:
        data["employer"] = _employer_part(conn, emp["id"])
    data["activity"] = _rows(conn,
        "SELECT at, action, outcome, resource_type, resource_id, detail FROM audit_log WHERE user_id = ? "
        "ORDER BY id DESC LIMIT ?", (user_id, ACTIVITY_LIMIT))
    return data, files


def build_zip(conn, storage, user_id: str) -> bytes:
    data, files = collect(conn, user_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        names = {}
        for doc_id, filename, key in files:
            path = f"documents/{doc_id[:8]}-{_safe_name(filename)}"
            try:
                z.writestr(path, storage.read(key))
                names[doc_id] = path
            except Exception:  # a missing file shouldn't stop the rest of the export
                names[doc_id] = None
        for d in data.get("candidate", {}).get("documents", []):
            d["file_in_this_export"] = names.get(d["id"])
        z.writestr("data.json", json.dumps(data, indent=2, ensure_ascii=False, default=str))
        z.writestr("README.txt", README.format(exported_at=data["exported_at"], email=data["account"]["email"],
                                               limit=ACTIVITY_LIMIT))
    return buf.getvalue()


def export_user_data(conn, principal, storage, user_id: str) -> bytes:
    """Checks the caller may export this user's data (themselves, or an admin), logs it, builds the ZIP."""
    ac.authorize_data_export(conn, principal, user_id)
    return build_zip(conn, storage, user_id)
