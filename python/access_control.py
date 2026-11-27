"""Mining Match - access control (who may see and do what).

This is the layer the API calls. Every function takes the database connection and the
caller's Principal first, checks the rules, does the work through data_layer and writes
an audit row. Nothing a user can reach should call data_layer's write functions directly.

Login is NOT handled here. The API checks the password/session, then calls
principal_for_user(conn, user_id). A Principal is never trusted: every call re-reads
the account, so suspending an account cuts access at once, and a hand-built Principal
cannot claim a role or profile it doesn't own.

RULES
  candidate  Own profile, licences, qualifications, skills, experience and documents.
             Can hide or delete their profile, see open jobs ranked for them, and approve
             or refuse employers' requests for their contact details.
  employer   Own jobs and shortlists. Can search and rank candidates, but candidates are
             ANONYMISED (only the fields in ANONYMOUS_FIELDS) until BOTH keys turn:
               1. the candidate approves that employer (set_contact_release), and
               2. the employer accepts the current placement fee terms (accept_fee_terms)
                  and unlocks the candidate (unlock_candidate).
             The unlock is recorded as an introduction, the event that starts the fee
             window. Only then are name, email, phone, summary and documents shown.
             Each search returns at most EMPLOYER_MAX_RESULTS candidates.
  admin      Everything, except giving consent on a candidate's behalf or unlocking for an
             employer. Publishes fee terms. Audited.

Privacy details
  * For employers, ties in rankings and shortlists are broken by candidate id, never by
    name. Otherwise an employer could add test candidates with chosen names and use the
    order to work out a hidden name letter by letter.
  * File locations (storage keys) are generated here, never chosen by the client.
  * Audit rows hold ids, actions and short reasons only, never names or contact details.

Errors: AccessDenied (HTTP 403) when the rules say no; data_layer.NotFoundError (404) when
the record doesn't exist. Ids are random UUIDs, so telling the two apart reveals nothing
useful. The SQL here is limited to the lookups the rules need.
"""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, NoReturn, Optional

import data_layer as dl
import two_factor

__all__ = [
    "AccessDenied", "Principal", "principal_for_user",
    "EMPLOYER_MAX_RESULTS", "MAX_UPLOAD_BYTES", "ALLOWED_MIME_TYPES", "ANONYMOUS_FIELDS",
    "view_candidate", "update_candidate",
    "set_candidate_licence", "set_candidate_qualification", "set_candidate_skill",
    "remove_candidate_licence", "remove_candidate_qualification", "remove_candidate_skill",
    "add_experience", "remove_experience", "delete_candidate",
    "add_document", "list_documents", "get_document_file", "verify_document", "remove_document",
    "request_contact", "set_contact_release", "list_contact_requests",
    "publish_fee_terms", "get_fee_terms", "accept_fee_terms", "unlock_candidate", "list_introductions",
    "create_job", "update_job", "set_job_status", "get_job", "list_my_jobs", "list_open_jobs",
    "rank_candidates_for_job", "search_candidates", "rank_jobs_for_candidate",
    "create_shortlist", "list_shortlists", "add_to_shortlist", "set_shortlist_status",
    "remove_from_shortlist", "get_shortlist",
    "set_user_active", "create_admin", "read_audit_log", "list_users", "list_documents_to_verify",
    "list_catalog",
]

EMPLOYER_MAX_RESULTS = 100  # most candidates an employer gets back from one search or ranking
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_MIME_TYPES = frozenset({
    "application/pdf",
    "image/jpeg",
    "image/png",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
})
AUDIT_MAX_ROWS = 1000

# What an employer sees of a candidate before the candidate approves them. This is an
# allow-list: any field added to candidates later stays hidden unless it is added here.
ANONYMOUS_FIELDS = (
    "id", "location", "willing_to_relocate", "willing_fifo", "available_from",
    "licences", "qualifications", "skills", "experience",
)
_RANK_OPTIONS = frozenset({"weights", "min_score", "eligible_only", "limit"})


class AccessDenied(dl.DataError):
    """The caller may not do this. The message never contains personal data."""


@dataclass(frozen=True)
class Principal:
    """Who is calling. Get one from principal_for_user(); don't build it from request data."""

    user_id: str
    role: str
    candidate_id: Optional[str] = None
    employer_id: Optional[str] = None


# --------------------------------------------------------------------------
# Identity and audit
# --------------------------------------------------------------------------
def _text(value: Any, limit: int = 200) -> Optional[str]:
    return None if value is None else str(value)[:limit]


def _audit(conn, principal, action, outcome, resource_type=None, resource_id=None, detail=None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO audit_log (user_id, role, action, outcome, resource_type, resource_id, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _text(getattr(principal, "user_id", None), 64), _text(getattr(principal, "role", None), 20),
                action, outcome, resource_type, _text(resource_id, 64), _text(detail),
            ),
        )


def _ok(conn, principal, action, resource_type=None, resource_id=None, detail=None) -> None:
    _audit(conn, principal, action, "allowed", resource_type, resource_id, detail)


def record_upload_rejected(conn, principal, candidate_id: str, reason: str) -> None:
    """Log an upload refused by the virus scan (or because scanning was unavailable)."""
    _audit(conn, principal, "upload_document", "denied", "candidate", candidate_id, reason)


def _deny(conn, principal, action, resource_type, resource_id, reason) -> NoReturn:
    _audit(conn, principal, action, "denied", resource_type, resource_id, reason)
    raise AccessDenied(f"not allowed: {action}")


def _resolve(conn, user_id: Any) -> Optional[Principal]:
    """Build a Principal from the database, or None if the account is missing or suspended."""
    if not isinstance(user_id, str):
        return None
    row = conn.execute("SELECT role, is_active FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or not row["is_active"]:
        return None
    candidate_id = employer_id = None
    if row["role"] == "candidate":
        r = conn.execute("SELECT id FROM candidates WHERE user_id = ?", (user_id,)).fetchone()
        candidate_id = r["id"] if r else None
    elif row["role"] == "employer":
        r = conn.execute("SELECT id FROM employers WHERE user_id = ?", (user_id,)).fetchone()
        employer_id = r["id"] if r else None
    return Principal(user_id, row["role"], candidate_id, employer_id)


def principal_for_user(conn, user_id: str) -> Principal:
    """Call after login succeeds. Raises AccessDenied for unknown or suspended accounts."""
    principal = _resolve(conn, user_id)
    if principal is None:
        _deny(conn, None, "auth.principal", "user", user_id, "unknown or suspended account")
    return principal


def _authorize(conn, principal: Any, action: str, *roles: str) -> Principal:
    """Re-check the account on every call; returns the Principal as the database sees it."""
    claimed = principal if isinstance(principal, Principal) else None
    fresh = _resolve(conn, claimed.user_id) if claimed else None
    if fresh is None:
        _deny(conn, claimed, action, None, None, "no such active account")
    if fresh != claimed:
        _deny(conn, fresh, action, None, None, "principal does not match account")
    if fresh.role not in roles:
        _deny(conn, fresh, action, None, None, f"role {fresh.role} may not do this")
    return fresh


# --------------------------------------------------------------------------
# Ownership and visibility lookups
# --------------------------------------------------------------------------
def _marks(items: list) -> str:
    return ",".join("?" * len(items))


def _label(candidate_id: str) -> str:
    return f"Candidate {str(candidate_id)[:8].upper()}"


def _own_candidate(conn, p: Principal, action: str, candidate_id: str) -> None:
    if p.role == "admin" or (p.role == "candidate" and p.candidate_id == candidate_id):
        return
    _deny(conn, p, action, "candidate", candidate_id, "not your profile")


def _candidate_visible(conn, candidate_id: str) -> bool:
    """Profile not hidden, account not suspended, email confirmed."""
    return conn.execute(
        "SELECT 1 FROM candidates c JOIN users u ON u.id = c.user_id "
        "WHERE c.id = ? AND c.is_active = 1 AND u.is_active = 1 AND u.email_verified_at IS NOT NULL",
        (candidate_id,),
    ).fetchone() is not None


def _contact_statuses(conn, employer_id: str, candidate_ids: list) -> dict:
    """Where each candidate stands with this employer.

    none | requested | declined | approved (candidate said yes, employer hasn't unlocked yet)
    | unlocked (both keys turned: details are shown)
    """
    ids = list(dict.fromkeys(candidate_ids))
    status = {cid: "none" for cid in ids}
    if not ids:
        return status
    rows = conn.execute(
        "SELECT cr.candidate_id, cr.status, i.id AS intro FROM contact_releases cr "
        "LEFT JOIN introductions i ON i.candidate_id = cr.candidate_id AND i.employer_id = cr.employer_id "
        f"WHERE cr.employer_id = ? AND cr.candidate_id IN ({_marks(ids)})",
        (employer_id, *ids),
    )
    for r in rows:
        if r["status"] == "granted":
            status[r["candidate_id"]] = "unlocked" if r["intro"] is not None else "approved"
        else:
            status[r["candidate_id"]] = r["status"]
    return status


def _unlocked(conn, employer_id: str, candidate_ids: list) -> set:
    """Candidates whose details this employer may see (candidate approved AND employer unlocked)."""
    return {c for c, st in _contact_statuses(conn, employer_id, candidate_ids).items() if st == "unlocked"}


def _employer_needs_unlock(conn, p: Principal, action: str, candidate_id: str) -> None:
    if not _candidate_visible(conn, candidate_id):
        _deny(conn, p, action, "candidate", candidate_id, "candidate not available")
    if candidate_id not in _unlocked(conn, p.employer_id, [candidate_id]):
        _deny(conn, p, action, "candidate", candidate_id, "details not unlocked for this employer")


def _job_row(conn, job_id: str):
    row = conn.execute("SELECT id, employer_id, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise dl.NotFoundError(f"job not found: {job_id}")
    return row


def _own_job(conn, p: Principal, action: str, job_id: str):
    row = _job_row(conn, job_id)
    if p.role == "admin" or (p.role == "employer" and row["employer_id"] == p.employer_id):
        return row
    _deny(conn, p, action, "job", job_id, "not your job")


def _job_public(conn, job_id: str) -> bool:
    """Open, and the employer's account is active and confirmed."""
    return conn.execute(
        "SELECT 1 FROM jobs j JOIN employers e ON e.id = j.employer_id JOIN users u ON u.id = e.user_id "
        "WHERE j.id = ? AND j.status = 'open' AND u.is_active = 1 AND u.email_verified_at IS NOT NULL",
        (job_id,),
    ).fetchone() is not None


def _own_shortlist(conn, p: Principal, action: str, shortlist_id: str) -> None:
    row = conn.execute("SELECT employer_id FROM shortlists WHERE id = ?", (shortlist_id,)).fetchone()
    if row is None:
        raise dl.NotFoundError(f"shortlist not found: {shortlist_id}")
    if p.role == "admin" or (p.role == "employer" and row["employer_id"] == p.employer_id):
        return
    _deny(conn, p, action, "shortlist", shortlist_id, "not your shortlist")


def _employer_scope(conn, p: Principal, action: str, employer_id: Optional[str]) -> str:
    """Employers act for themselves; admins must say which employer."""
    if p.role == "employer":
        if employer_id not in (None, p.employer_id):
            _deny(conn, p, action, "employer", employer_id, "not your organisation")
        return p.employer_id
    if employer_id is None:
        raise ValueError("employer_id is required for admins")
    return employer_id


def _company_names(conn, employer_ids: list) -> dict:
    ids = list(dict.fromkeys(employer_ids))
    if not ids:
        return {}
    rows = conn.execute(f"SELECT id, company_name FROM employers WHERE id IN ({_marks(ids)})", ids)
    return {r["id"]: r["company_name"] for r in rows}


# --------------------------------------------------------------------------
# Candidate views
# --------------------------------------------------------------------------
def _public_document(d: dict) -> dict:
    """Document metadata without the storage key (files go through get_document_file)."""
    keep = ("id", "kind", "filename", "mime_type", "size_bytes", "verified_at", "uploaded_at")
    return {k: d[k] for k in keep}


def _full_profile(conn, candidate_id: str, as_of: Any) -> dict:
    found = dl.load_candidates(conn, [candidate_id], active_only=False, as_of=as_of)
    if not found:
        raise dl.NotFoundError(f"candidate not found: {candidate_id}")
    profile = found[0]
    row = conn.execute(
        "SELECT c.phone, c.summary, c.is_active, c.expiry_reminders, u.email FROM candidates c "
        "JOIN users u ON u.id = c.user_id WHERE c.id = ?",
        (candidate_id,),
    ).fetchone()
    history = conn.execute(
        "SELECT id, role, employer_name, start_date, end_date FROM candidate_experience "
        "WHERE candidate_id = ? ORDER BY COALESCE(end_date, '9999-12-31') DESC, start_date DESC",
        (candidate_id,),
    ).fetchall()
    profile.update(
        email=row["email"], phone=row["phone"], summary=row["summary"], is_active=bool(row["is_active"]),
        expiry_reminders=bool(row["expiry_reminders"]),
        documents=[_public_document(d) for d in dl.list_documents(conn, candidate_id)],
        work_history=[dict(r) for r in history],  # with dates and past employers' names
    )
    return profile


def _employer_view(conn, p: Principal, candidate_id: str, as_of: Any) -> dict:
    full = _full_profile(conn, candidate_id, as_of)
    view = {k: full[k] for k in ANONYMOUS_FIELDS}
    status = _contact_statuses(conn, p.employer_id, [candidate_id])[candidate_id]
    released = status == "unlocked"
    view["contact_status"] = status
    view["contact_released"] = released
    view["display_name"] = full["name"] if released else _label(candidate_id)
    if released:
        for k in ("name", "email", "phone", "summary", "documents", "work_history"):
            view[k] = full[k]
    return view


def view_candidate(conn, principal, candidate_id: str, *, as_of: Any = None) -> dict:
    """Candidate: own full profile. Employer: anonymised unless approved. Admin: full."""
    action = "candidate.view"
    p = _authorize(conn, principal, action, "candidate", "employer", "admin")
    if p.role == "candidate":
        _own_candidate(conn, p, action, candidate_id)
        return _full_profile(conn, candidate_id, as_of)
    if p.role == "admin":
        view = _full_profile(conn, candidate_id, as_of)
        _ok(conn, p, action, "candidate", candidate_id)
        return view
    if not _candidate_visible(conn, candidate_id):
        _deny(conn, p, action, "candidate", candidate_id, "candidate not available")
    view = _employer_view(conn, p, candidate_id, as_of)
    _ok(conn, p, action, "candidate", candidate_id, "full" if view["contact_released"] else "anonymised")
    return view


# --------------------------------------------------------------------------
# Candidate profile (owner or admin)
# --------------------------------------------------------------------------
def _as_owner(conn, principal, action: str, candidate_id: str, work, detail=None):
    p = _authorize(conn, principal, action, "candidate", "admin")
    _own_candidate(conn, p, action, candidate_id)
    result = work()
    _ok(conn, p, action, "candidate", candidate_id, detail)
    return result


def update_candidate(conn, principal, candidate_id: str, **fields: Any) -> None:
    """Fields as data_layer.update_candidate; is_active=False hides the profile from employers."""
    _as_owner(conn, principal, "candidate.update", candidate_id,
              lambda: dl.update_candidate(conn, candidate_id, **fields), ",".join(sorted(fields)))


def set_candidate_licence(conn, principal, candidate_id, name, expiry=None, document_id=None) -> None:
    _as_owner(conn, principal, "candidate.licence.set", candidate_id,
              lambda: dl.set_candidate_licence(conn, candidate_id, name, expiry, document_id))


def set_candidate_qualification(conn, principal, candidate_id, name, **kw: Any) -> None:
    _as_owner(conn, principal, "candidate.qualification.set", candidate_id,
              lambda: dl.set_candidate_qualification(conn, candidate_id, name, **kw))


def set_candidate_skill(conn, principal, candidate_id, name, years=None) -> None:
    _as_owner(conn, principal, "candidate.skill.set", candidate_id,
              lambda: dl.set_candidate_skill(conn, candidate_id, name, years))


def remove_candidate_licence(conn, principal, candidate_id, name) -> bool:
    return _as_owner(conn, principal, "candidate.licence.remove", candidate_id,
                     lambda: dl.remove_candidate_licence(conn, candidate_id, name))


def remove_candidate_qualification(conn, principal, candidate_id, name) -> bool:
    return _as_owner(conn, principal, "candidate.qualification.remove", candidate_id,
                     lambda: dl.remove_candidate_qualification(conn, candidate_id, name))


def remove_candidate_skill(conn, principal, candidate_id, name) -> bool:
    return _as_owner(conn, principal, "candidate.skill.remove", candidate_id,
                     lambda: dl.remove_candidate_skill(conn, candidate_id, name))


def add_experience(conn, principal, candidate_id, role, start_date, end_date=None, **kw: Any) -> str:
    return _as_owner(conn, principal, "candidate.experience.add", candidate_id,
                     lambda: dl.add_experience(conn, candidate_id, role, start_date, end_date, **kw))


def remove_experience(conn, principal, experience_id: str) -> bool:
    action = "candidate.experience.remove"
    p = _authorize(conn, principal, action, "candidate", "admin")
    row = conn.execute("SELECT candidate_id FROM candidate_experience WHERE id = ?", (experience_id,)).fetchone()
    if row is None:
        raise dl.NotFoundError(f"experience not found: {experience_id}")
    _own_candidate(conn, p, action, row["candidate_id"])
    removed = dl.remove_experience(conn, experience_id)
    _ok(conn, p, action, "candidate", row["candidate_id"])
    return removed


def delete_candidate(conn, principal, candidate_id: str) -> list:
    """Delete the account and everything attached. Returns storage keys of files to delete."""
    return _as_owner(conn, principal, "candidate.delete", candidate_id,
                     lambda: dl.delete_candidate(conn, candidate_id))


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ ()\-]")


def _clean_filename(filename: Any) -> str:
    name = os.path.basename(str(filename or "").replace("\\", "/"))
    name = _UNSAFE_FILENAME_CHARS.sub("_", name)[:120].strip(" .")
    if not name:
        raise ValueError("a filename is required")
    return name


def add_document(conn, principal, candidate_id: str, kind: str, filename: str, *,
                 mime_type: str, size_bytes: int) -> dict:
    """Register an upload. Returns {'id', 'storage_key'}; save the file bytes at storage_key.

    The storage key is generated here so a client can never point a document at someone
    else's file. mime_type is what the client declared: also check the file content itself
    when saving it.
    """
    action = "document.add"
    p = _authorize(conn, principal, action, "candidate", "admin")
    _own_candidate(conn, p, action, candidate_id)
    clean = _clean_filename(filename)
    if mime_type not in ALLOWED_MIME_TYPES:
        raise ValueError(f"file type not accepted: {mime_type}")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or not 0 < size_bytes <= MAX_UPLOAD_BYTES:
        raise ValueError(f"file size must be between 1 byte and {MAX_UPLOAD_BYTES} bytes")
    key = f"candidates/{candidate_id}/{uuid.uuid4().hex}"
    doc_id = dl.add_document(conn, candidate_id, kind, clean, key, mime_type=mime_type, size_bytes=size_bytes)
    _ok(conn, p, action, "document", doc_id)
    return {"id": doc_id, "storage_key": key}


def list_documents(conn, principal, candidate_id: str) -> list:
    """Owner, admin, or an employer the candidate has approved."""
    action = "document.list"
    p = _authorize(conn, principal, action, "candidate", "employer", "admin")
    if p.role == "employer":
        _employer_needs_unlock(conn, p, action, candidate_id)
    else:
        _own_candidate(conn, p, action, candidate_id)
    docs = [_public_document(d) for d in dl.list_documents(conn, candidate_id)]
    if p.role != "candidate":
        _ok(conn, p, action, "candidate", candidate_id)
    return docs


def get_document_file(conn, principal, document_id: str) -> dict:
    """Check access to a file before serving it. Returns storage_key, filename, mime_type."""
    action = "document.download"
    p = _authorize(conn, principal, action, "candidate", "employer", "admin")
    row = conn.execute(
        "SELECT candidate_id, filename, mime_type, storage_key FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if row is None:
        raise dl.NotFoundError(f"document not found: {document_id}")
    if p.role == "employer":
        _employer_needs_unlock(conn, p, action, row["candidate_id"])
    else:
        _own_candidate(conn, p, action, row["candidate_id"])
    _ok(conn, p, action, "document", document_id)
    return {"storage_key": row["storage_key"], "filename": row["filename"], "mime_type": row["mime_type"]}


def remove_document(conn, principal, document_id: str) -> str:
    """Owner or admin. Returns the storage_key so the caller can delete the file.

    Licences or qualifications that pointed at it keep their entry, just without the document.
    """
    action = "document.remove"
    p = _authorize(conn, principal, action, "candidate", "admin")
    row = conn.execute("SELECT candidate_id, storage_key FROM documents WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        raise dl.NotFoundError(f"document not found: {document_id}")
    _own_candidate(conn, p, action, row["candidate_id"])
    with conn:
        conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    _ok(conn, p, action, "document", document_id)
    return row["storage_key"]


def list_documents_to_verify(conn, principal, *, limit: int = 100) -> list:
    """Admin: uploaded documents nobody has checked yet, oldest first."""
    action = "document.list_unverified"
    p = _authorize(conn, principal, action, "admin")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    rows = conn.execute(
        "SELECT d.id, d.candidate_id, c.name AS candidate_name, d.kind, d.filename, d.mime_type, d.size_bytes, "
        "d.uploaded_at FROM documents d JOIN candidates c ON c.id = d.candidate_id "
        "WHERE d.verified_at IS NULL ORDER BY d.uploaded_at, d.id LIMIT ?",
        (limit,),
    ).fetchall()
    _ok(conn, p, action, None, None, f"{len(rows)} rows")
    return [dict(r) for r in rows]


def verify_document(conn, principal, document_id: str) -> None:
    """Admin only: candidates can't vouch for their own documents."""
    action = "document.verify"
    p = _authorize(conn, principal, action, "admin")
    dl.verify_document(conn, document_id)
    _ok(conn, p, action, "document", document_id)


# --------------------------------------------------------------------------
# Contact details: employer asks, candidate decides
# --------------------------------------------------------------------------
def _release_status(conn, candidate_id: str, employer_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT status FROM contact_releases WHERE candidate_id = ? AND employer_id = ?",
        (candidate_id, employer_id),
    ).fetchone()
    return row["status"] if row else None


def request_contact(conn, principal, candidate_id: str, job_id: Optional[str] = None) -> str:
    """Employer asks for a candidate's details. Returns the status: requested, granted or declined.

    Asking again changes nothing, so a candidate who said no isn't asked repeatedly.
    """
    action = "contact.request"
    p = _authorize(conn, principal, action, "employer")
    if not _candidate_visible(conn, candidate_id):
        _deny(conn, p, action, "candidate", candidate_id, "candidate not available")
    if job_id is not None:
        _own_job(conn, p, action, job_id)
    with conn:
        conn.execute(
            "INSERT INTO contact_releases (candidate_id, employer_id, job_id, status, requested_at) "
            "VALUES (?, ?, ?, 'requested', CURRENT_TIMESTAMP) ON CONFLICT(candidate_id, employer_id) DO NOTHING",
            (candidate_id, p.employer_id, job_id),
        )
    status = _release_status(conn, candidate_id, p.employer_id)
    _ok(conn, p, action, "candidate", candidate_id, status)
    return status


def set_contact_release(conn, principal, employer_id: str, grant: bool) -> str:
    """Candidate approves (grant=True) or refuses/withdraws (grant=False) an employer.

    Works whether or not the employer has asked. Only the candidate can do this.
    """
    action = "contact.set"
    p = _authorize(conn, principal, action, "candidate")
    if not _company_names(conn, [employer_id]):
        raise dl.NotFoundError(f"employer not found: {employer_id}")
    status = "granted" if grant else "declined"
    with conn:
        conn.execute(
            "INSERT INTO contact_releases (candidate_id, employer_id, status, decided_at) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP) ON CONFLICT(candidate_id, employer_id) DO UPDATE SET "
            "status = excluded.status, decided_at = excluded.decided_at",
            (p.candidate_id, employer_id, status),
        )
    _ok(conn, p, action, "employer", employer_id, status)
    return status


def list_contact_requests(conn, principal) -> list:
    """Candidate: who asked (company and job title). Employer: status of their requests."""
    p = _authorize(conn, principal, "contact.list", "candidate", "employer")
    if p.role == "candidate":
        rows = conn.execute(
            "SELECT cr.employer_id, e.company_name, cr.job_id, j.title AS job_title, cr.status, "
            "cr.requested_at, cr.decided_at FROM contact_releases cr "
            "JOIN employers e ON e.id = cr.employer_id JOIN users u ON u.id = e.user_id AND u.is_active = 1 "
            "LEFT JOIN jobs j ON j.id = cr.job_id WHERE cr.candidate_id = ? "
            "ORDER BY cr.requested_at DESC, e.company_name",
            (p.candidate_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    rows = conn.execute(
        "SELECT candidate_id, job_id, status, requested_at, decided_at FROM contact_releases "
        "WHERE employer_id = ? ORDER BY requested_at DESC, candidate_id",
        (p.employer_id,),
    ).fetchall()
    statuses = _contact_statuses(conn, p.employer_id, [r["candidate_id"] for r in rows])
    return [
        {**dict(r), "contact_status": statuses[r["candidate_id"]], "display_name": _label(r["candidate_id"])}
        for r in rows
    ]


# --------------------------------------------------------------------------
# Placement fee terms and the unlock (second key)
# --------------------------------------------------------------------------
_TERMS_FIELDS = ("version", "fee_percent", "min_fee_aud", "fee_basis", "window_months",
                 "guarantee_days", "terms_text", "published_at")


def _current_terms(conn):
    return conn.execute("SELECT * FROM fee_terms ORDER BY version DESC LIMIT 1").fetchone()


def _number(value: Any, label: str, *, whole: bool = False) -> None:
    ok_types = (int,) if whole else (int, float)
    if isinstance(value, bool) or not isinstance(value, ok_types):
        raise ValueError(f"{label} must be a {'whole ' if whole else ''}number")


def publish_fee_terms(conn, principal, *, fee_percent: float, fee_basis: str, window_months: int,
                      guarantee_days: int, terms_text: str, min_fee_aud: Optional[float] = None) -> int:
    """Admin publishes a new version of the placement fee terms. Returns the version number.

    Published versions can never be edited or deleted (the database refuses), so they stay a
    reliable record of what each employer agreed to. Employers must accept the newest version
    before unlocking anyone else.
    """
    action = "fees.publish"
    p = _authorize(conn, principal, action, "admin")
    _number(fee_percent, "fee_percent")
    _number(window_months, "window_months", whole=True)
    _number(guarantee_days, "guarantee_days", whole=True)
    if min_fee_aud is not None:
        _number(min_fee_aud, "min_fee_aud")
    if not 0 < fee_percent <= 100:
        raise ValueError("fee_percent must be above 0 and at most 100")
    if window_months < 1 or guarantee_days < 0 or (min_fee_aud is not None and min_fee_aud < 0):
        raise ValueError("window_months must be 1 or more; guarantee_days and min_fee_aud can't be negative")
    basis, text = str(fee_basis or "").strip(), str(terms_text or "").strip()
    if not basis or not text:
        raise ValueError("fee_basis and terms_text are required")
    if len(text) > 100_000 or len(basis) > 500:
        raise ValueError("terms are too long")
    with conn:
        row = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM fee_terms").fetchone()
        version = row[0]
        conn.execute(
            "INSERT INTO fee_terms (version, fee_percent, min_fee_aud, fee_basis, window_months, guarantee_days, "
            "terms_text, published_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (version, float(fee_percent), min_fee_aud, basis, window_months, guarantee_days, text, p.user_id),
        )
    _ok(conn, p, action, "fee_terms", str(version))
    return version


def get_fee_terms(conn, principal, version: Optional[int] = None) -> dict:
    """The current terms (or a given version). Employers also see whether they've accepted it."""
    p = _authorize(conn, principal, "fees.view", "employer", "admin")
    if version is None:
        row = _current_terms(conn)
    else:
        row = conn.execute("SELECT * FROM fee_terms WHERE version = ?", (version,)).fetchone()
    if row is None:
        raise dl.NotFoundError("no fee terms published" if version is None else f"fee terms not found: {version}")
    current = _current_terms(conn)
    terms = {k: row[k] for k in _TERMS_FIELDS}
    terms["is_current"] = row["version"] == current["version"]
    if p.role == "employer":
        terms["accepted"] = conn.execute(
            "SELECT 1 FROM terms_acceptances WHERE employer_id = ? AND terms_version = ?",
            (p.employer_id, row["version"]),
        ).fetchone() is not None
    return terms


def accept_fee_terms(conn, principal, version: int) -> None:
    """Employer accepts the current fee terms. Only the current version can be accepted."""
    action = "fees.accept"
    p = _authorize(conn, principal, action, "employer")
    current = _current_terms(conn)
    if current is None:
        raise dl.NotFoundError("no fee terms published")
    if version != current["version"]:
        raise dl.DataError(f"terms have changed: review and accept version {current['version']}")
    with conn:
        conn.execute(
            "INSERT INTO terms_acceptances (employer_id, terms_version, accepted_by) VALUES (?, ?, ?) "
            "ON CONFLICT(employer_id, terms_version) DO NOTHING",
            (p.employer_id, version, p.user_id),
        )
    _ok(conn, p, action, "fee_terms", str(version))


def unlock_candidate(conn, principal, candidate_id: str, terms_version: int,
                     job_id: Optional[str] = None) -> dict:
    """Employer unlocks a candidate's details: the second key.

    Needs the candidate's approval and the employer's acceptance of the current fee terms.
    terms_version must be the current version: the client shows those terms on the unlock
    button and sends back the version the user saw, so nobody unlocks under terms they
    haven't been shown. The first unlock is recorded as the introduction (fee window starts);
    unlocking again later reuses it.
    """
    action = "candidate.unlock"
    p = _authorize(conn, principal, action, "employer")
    if not _candidate_visible(conn, candidate_id):
        _deny(conn, p, action, "candidate", candidate_id, "candidate not available")
    if job_id is not None:
        _own_job(conn, p, action, job_id)
    current = _current_terms(conn)
    if current is None:
        raise dl.NotFoundError("no fee terms published")
    if terms_version != current["version"]:
        raise dl.DataError(f"terms have changed: review and accept version {current['version']}")
    accepted = conn.execute(
        "SELECT 1 FROM terms_acceptances WHERE employer_id = ? AND terms_version = ?",
        (p.employer_id, terms_version),
    ).fetchone()
    if accepted is None:
        _deny(conn, p, action, "candidate", candidate_id, "current fee terms not accepted")
    if _release_status(conn, candidate_id, p.employer_id) != "granted":
        _deny(conn, p, action, "candidate", candidate_id, "candidate has not approved this employer")
    with conn:
        cur = conn.execute(
            "INSERT INTO introductions (candidate_id, employer_id, job_id, terms_version, unlocked_by) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(candidate_id, employer_id) DO NOTHING",
            (candidate_id, p.employer_id, job_id, terms_version, p.user_id),
        )
    new = cur.rowcount == 1
    intro = conn.execute(
        "SELECT job_id, terms_version, unlocked_at FROM introductions WHERE candidate_id = ? AND employer_id = ?",
        (candidate_id, p.employer_id),
    ).fetchone()
    _ok(conn, p, action, "candidate", candidate_id,
        f"introduced under terms v{intro['terms_version']}" if new else "already introduced")
    return {"candidate_id": candidate_id, "new_introduction": new, **dict(intro)}


def list_introductions(conn, principal, *, employer_id: Optional[str] = None) -> list:
    """Unlocks that may lead to a placement fee. Employer: own. Admin: all, or one employer's.

    Employers see the name only while the candidate still approves them; admins see it unless
    the candidate has deleted their account (then candidate_name is None).
    """
    action = "introductions.list"
    p = _authorize(conn, principal, action, "employer", "admin")
    if p.role == "employer":
        employer_id = _employer_scope(conn, p, action, employer_id)
    where, params = ("WHERE i.employer_id = ?", [employer_id]) if employer_id else ("", [])
    rows = conn.execute(
        "SELECT i.candidate_id, i.employer_id, i.job_id, i.terms_version, i.unlocked_by, i.unlocked_at, "
        "c.name AS candidate_name, e.company_name FROM introductions i "
        "LEFT JOIN candidates c ON c.id = i.candidate_id LEFT JOIN employers e ON e.id = i.employer_id "
        f"{where} ORDER BY i.unlocked_at DESC, i.id DESC",
        params,
    ).fetchall()
    out = [dict(r) for r in rows]
    if p.role == "employer":
        unlocked = _unlocked(conn, p.employer_id, [r["candidate_id"] for r in out])
        for r in out:
            name = r.pop("candidate_name")
            r["display_name"] = name if r["candidate_id"] in unlocked else _label(r["candidate_id"])
    else:
        _ok(conn, p, action, "employer", employer_id, f"{len(out)} rows")
    return out


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------
def create_job(conn, principal, job: dict, *, status: str = "draft", employer_id: Optional[str] = None) -> str:
    action = "job.create"
    p = _authorize(conn, principal, action, "employer", "admin")
    owner = _employer_scope(conn, p, action, employer_id)
    job_id = dl.create_job(conn, owner, job, status=status)
    _ok(conn, p, action, "job", job_id)
    return job_id


def update_job(conn, principal, job_id: str, job: dict) -> None:
    action = "job.update"
    p = _authorize(conn, principal, action, "employer", "admin")
    _own_job(conn, p, action, job_id)
    dl.update_job(conn, job_id, job)
    _ok(conn, p, action, "job", job_id)


def set_job_status(conn, principal, job_id: str, status: str) -> None:
    action = "job.status"
    p = _authorize(conn, principal, action, "employer", "admin")
    _own_job(conn, p, action, job_id)
    dl.set_job_status(conn, job_id, status)
    _ok(conn, p, action, "job", job_id, status)


def _with_company(conn, jobs: list) -> list:
    names = _company_names(conn, [j["employer_id"] for j in jobs])
    for j in jobs:
        j["company_name"] = names.get(j["employer_id"])
    return jobs


def get_job(conn, principal, job_id: str) -> dict:
    """Employer: own jobs. Candidate: open jobs only. Admin: any."""
    action = "job.view"
    p = _authorize(conn, principal, action, "candidate", "employer", "admin")
    row = _job_row(conn, job_id)
    if p.role == "employer" and row["employer_id"] != p.employer_id:
        _deny(conn, p, action, "job", job_id, "not your job")
    if p.role == "candidate" and not _job_public(conn, job_id):
        _deny(conn, p, action, "job", job_id, "job not open")
    return _with_company(conn, [dl.get_job(conn, job_id)])[0]


def list_my_jobs(conn, principal, *, status: Optional[str] = None, employer_id: Optional[str] = None) -> list:
    """Employer: own jobs (drafts included). Admin: all jobs, or one employer's."""
    action = "job.list"
    p = _authorize(conn, principal, action, "employer", "admin")
    if p.role == "employer":
        employer_id = _employer_scope(conn, p, action, employer_id)
    return _with_company(conn, dl.load_jobs(conn, status=status, employer_id=employer_id))


def list_open_jobs(conn, principal) -> list:
    _authorize(conn, principal, "job.list_open", "candidate", "admin")
    return _with_company(conn, dl.load_jobs(conn, status="open", active_employers_only=True))


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
def _rank_options(p: Principal, options: dict):
    unknown = set(options) - _RANK_OPTIONS
    if unknown:
        raise ValueError(f"unknown options: {sorted(unknown)}")
    options = dict(options)
    limit = options.pop("limit", None)
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a whole number of 1 or more")
    if p.role == "employer":
        limit = EMPLOYER_MAX_RESULTS if limit is None else min(limit, EMPLOYER_MAX_RESULTS)
    return options, limit


def _present_candidates(conn, p: Principal, ranked: list, limit: Optional[int]) -> list:
    if p.role == "admin":
        return [{**r, "display_name": r["candidate_name"]} for r in ranked[:limit]]
    # Ties broken by id, not name: the order must not depend on anything the employer can't see.
    ordered = sorted(ranked, key=lambda r: (not r["eligible"], -r["score"], str(r["candidate_id"])))[:limit]
    statuses = _contact_statuses(conn, p.employer_id, [r["candidate_id"] for r in ordered])
    out = []
    for r in ordered:
        shown = statuses[r["candidate_id"]] == "unlocked"
        row = {k: v for k, v in r.items() if k != "candidate_name"}
        row["display_name"] = r["candidate_name"] if shown else _label(r["candidate_id"])
        row["contact_status"] = statuses[r["candidate_id"]]
        row["contact_released"] = shown
        out.append(row)
    return out


def rank_candidates_for_job(conn, principal, job_id: str, *, as_of: Any = None, **options: Any) -> list:
    """Owner employer or admin. options: weights, min_score, eligible_only, limit."""
    action = "job.rank_candidates"
    p = _authorize(conn, principal, action, "employer", "admin")
    _own_job(conn, p, action, job_id)
    options, limit = _rank_options(p, options)
    ranked = dl.rank_candidates_for_job(conn, job_id, as_of=as_of, **options)
    out = _present_candidates(conn, p, ranked, limit)
    _ok(conn, p, action, "job", job_id, f"{len(out)} results")
    return out


def search_candidates(conn, principal, criteria: dict, *, as_of: Any = None, **options: Any) -> list:
    """Employer or admin search with ad-hoc criteria (an engine-shaped job dict)."""
    action = "candidate.search"
    p = _authorize(conn, principal, action, "employer", "admin")
    options, limit = _rank_options(p, options)
    ranked = dl.search_candidates(conn, criteria, as_of=as_of, **options)
    out = _present_candidates(conn, p, ranked, limit)
    _ok(conn, p, action, None, None, f"{len(out)} results")
    return out


def rank_jobs_for_candidate(conn, principal, candidate_id: str, *, as_of: Any = None, **options: Any) -> list:
    """Owner candidate or admin. Open jobs from active employers only."""
    action = "candidate.rank_jobs"
    p = _authorize(conn, principal, action, "candidate", "admin")
    _own_candidate(conn, p, action, candidate_id)
    options, limit = _rank_options(p, options)
    ranked = dl.rank_jobs_for_candidate(conn, candidate_id, as_of=as_of, **options)[:limit]
    ids = [r["job_id"] for r in ranked]
    owners = {}
    if ids:
        owners = {r["id"]: r["employer_id"] for r in conn.execute(
            f"SELECT id, employer_id FROM jobs WHERE id IN ({_marks(ids)})", ids)}
    names = _company_names(conn, list(owners.values()))
    return [{**r, "company_name": names.get(owners.get(r["job_id"]))} for r in ranked]


# --------------------------------------------------------------------------
# Shortlists (owner employer or admin)
# --------------------------------------------------------------------------
def create_shortlist(conn, principal, name: str, job_id: Optional[str] = None, *,
                     employer_id: Optional[str] = None) -> str:
    action = "shortlist.create"
    p = _authorize(conn, principal, action, "employer", "admin")
    owner = _employer_scope(conn, p, action, employer_id)
    if job_id is not None and p.role == "employer":
        _own_job(conn, p, action, job_id)
    shortlist_id = dl.create_shortlist(conn, owner, name, job_id)
    _ok(conn, p, action, "shortlist", shortlist_id)
    return shortlist_id


def list_shortlists(conn, principal, *, employer_id: Optional[str] = None) -> list:
    action = "shortlist.list"
    p = _authorize(conn, principal, action, "employer", "admin")
    return dl.list_shortlists(conn, _employer_scope(conn, p, action, employer_id))


def add_to_shortlist(conn, principal, shortlist_id: str, candidate_id: str, *,
                     notes: Optional[str] = None, as_of: Any = None) -> None:
    """Score is taken from the engine when the shortlist is linked to a job (never from the client)."""
    action = "shortlist.add"
    p = _authorize(conn, principal, action, "employer", "admin")
    _own_shortlist(conn, p, action, shortlist_id)
    if p.role == "employer" and not _candidate_visible(conn, candidate_id):
        _deny(conn, p, action, "candidate", candidate_id, "candidate not available")
    dl.add_to_shortlist(conn, shortlist_id, candidate_id, notes=notes, as_of=as_of)
    _ok(conn, p, action, "shortlist", shortlist_id)


def set_shortlist_status(conn, principal, shortlist_id: str, candidate_id: str, status: str) -> None:
    action = "shortlist.status"
    p = _authorize(conn, principal, action, "employer", "admin")
    _own_shortlist(conn, p, action, shortlist_id)
    dl.set_shortlist_status(conn, shortlist_id, candidate_id, status)
    _ok(conn, p, action, "shortlist", shortlist_id, status)


def remove_from_shortlist(conn, principal, shortlist_id: str, candidate_id: str) -> bool:
    action = "shortlist.remove"
    p = _authorize(conn, principal, action, "employer", "admin")
    _own_shortlist(conn, p, action, shortlist_id)
    removed = dl.remove_from_shortlist(conn, shortlist_id, candidate_id)
    _ok(conn, p, action, "shortlist", shortlist_id)
    return removed


def get_shortlist(conn, principal, shortlist_id: str) -> dict:
    """Entries show display_name; real names only for candidates who approved this employer."""
    action = "shortlist.view"
    p = _authorize(conn, principal, action, "employer", "admin")
    _own_shortlist(conn, p, action, shortlist_id)
    shortlist = dl.get_shortlist(conn, shortlist_id)
    entries = shortlist["entries"]
    if p.role == "admin":
        shortlist["entries"] = [{**e, "display_name": e["name"]} for e in entries]
        return shortlist
    employer_id = shortlist["employer_id"]
    entries = sorted(entries, key=lambda e: (
        e["score_at_add"] is None, -(e["score_at_add"] or 0.0), e["added_at"], e["candidate_id"]))
    statuses = _contact_statuses(conn, employer_id, [e["candidate_id"] for e in entries])
    out = []
    for e in entries:
        shown = statuses[e["candidate_id"]] == "unlocked"
        row = {k: v for k, v in e.items() if k != "name"}
        row["display_name"] = e["name"] if shown else _label(e["candidate_id"])
        row["contact_status"] = statuses[e["candidate_id"]]
        row["contact_released"] = shown
        out.append(row)
    shortlist["entries"] = out
    return shortlist


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------
def set_user_active(conn, principal, user_id: str, active: bool, reason: Optional[str] = None) -> None:
    """Suspend (active=False) or reactivate any account. Takes effect on the user's next call.

    reason: short and free of personal details; it goes into the audit log.
    """
    action = "user.set_active"
    p = _authorize(conn, principal, action, "admin")
    if user_id == p.user_id:
        _deny(conn, p, action, "user", user_id, "cannot change your own account status")
    found = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    if active and found and is_closed_email(found["email"]):
        raise dl.DataError("this account was closed and can't be reactivated; the employer can sign up again")
    with conn:
        cur = conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, user_id))
        if not active:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))  # logged out everywhere
    if cur.rowcount == 0:
        raise dl.NotFoundError(f"user not found: {user_id}")
    _ok(conn, p, action, "user", user_id, ("reactivated" if active else "suspended") + (f": {reason}" if reason else ""))


def create_admin(conn, email: str, password_hash: Optional[str] = None, *, by: Optional[Principal] = None) -> str:
    """Create an admin account. Returns the user id.

    by=None works only while there are no admins (first-time setup from a command line).
    Never expose by=None through the API. After that, only an admin can create admins.
    """
    action = "admin.create"
    if by is None:
        if conn.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone():
            _deny(conn, None, action, "user", None, "setup already done: an admin must create admins")
        actor = None
    else:
        actor = _authorize(conn, by, action, "admin")
    # The first admin is created at a terminal by whoever runs the server, so their email counts
    # as confirmed. Admins created through the API must confirm theirs before they can log in.
    user_id = dl.register_admin(conn, email, password_hash=password_hash, email_verified=by is None)
    _ok(conn, actor, action, "user", user_id)
    return user_id


def authorize_data_export(conn, principal, user_id: str) -> Principal:
    """A user may export their own data; an admin may export anyone's. Logged either way."""
    action = "user.export_data"
    p = _authorize(conn, principal, action, "candidate", "employer", "admin")
    if p.user_id != user_id and p.role != "admin":
        _deny(conn, p, action, "user", user_id, "only the account itself or an admin")
    if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
        raise dl.NotFoundError(f"user not found: {user_id}")
    _ok(conn, p, action, "user", user_id, "own data" if p.user_id == user_id else "by an admin")
    return p


CLOSED_EMAIL_DOMAIN = "closed.invalid"  # sign-in emails of closed accounts are replaced with this


def is_closed_email(email: Optional[str]) -> bool:
    return bool(email) and email.endswith("@" + CLOSED_EMAIL_DOMAIN)


def close_employer(conn, principal, employer_id: str, *, password: Optional[str] = None,
                   reason: Optional[str] = None) -> dict:
    """Close an employer account: by the employer (with their password) or by an admin.

    Removed: the sign-in email and password, the contact name, phone and billing email, all
    sessions and email links, shortlists (with their notes on candidates) and contact requests.
    Every job is closed, so none show to candidates. Kept, as business and tax records: the
    company name, ABN and address, introductions, hires, invoices and accepted fee terms.
    Refused while an invoice is unpaid or not yet issued, so a fee can't be dodged by closing.
    """
    import auth  # here, not at the top: auth is the layer above this one

    action = "employer.close"
    p = _authorize(conn, principal, action, "employer", "admin")
    if p.role == "employer" and p.employer_id != employer_id:
        _deny(conn, p, action, "employer", employer_id, "not your employer account")
    row = conn.execute("SELECT e.user_id, u.email, u.password_hash FROM employers e JOIN users u ON u.id = e.user_id "
                       "WHERE e.id = ?", (employer_id,)).fetchone()
    if row is None:
        raise dl.NotFoundError(f"employer not found: {employer_id}")
    if is_closed_email(row["email"]):
        raise dl.DataError("this account is already closed")
    if p.role == "employer" and not auth.verify_password(str(password or ""), row["password_hash"]):
        raise auth.InvalidCredentials("your password is wrong")
    owing = conn.execute("SELECT COUNT(*) FROM invoices WHERE employer_id = ? AND status IN ('scheduled', 'issued')",
                         (employer_id,)).fetchone()[0]
    if owing:
        raise dl.DataError(f"the account can't be closed while {owing} invoice{'s are' if owing != 1 else ' is'} "
                           "unpaid or still to be issued; pay or settle them first")
    user_id = row["user_id"]
    with conn:
        conn.execute("UPDATE users SET email = ?, password_hash = NULL, is_active = 0 WHERE id = ?",
                     (f"closed-{user_id}@{CLOSED_EMAIL_DOMAIN}", user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE employers SET contact_name = NULL, phone = NULL, billing_email = NULL WHERE id = ?",
                     (employer_id,))
        jobs = conn.execute("UPDATE jobs SET status = 'closed', updated_at = CURRENT_TIMESTAMP "
                            "WHERE employer_id = ? AND status IN ('draft', 'open')", (employer_id,)).rowcount
        lists = conn.execute("DELETE FROM shortlists WHERE employer_id = ?", (employer_id,)).rowcount
        requests = conn.execute("DELETE FROM contact_releases WHERE employer_id = ?", (employer_id,)).rowcount
    note = "closed by the employer" if p.role == "employer" else "closed by an admin" + (f": {reason}" if reason else "")
    _ok(conn, p, action, "employer", employer_id, note)
    return {"closed": True, "jobs_closed": jobs, "shortlists_removed": lists, "contact_requests_removed": requests}


def reset_two_factor(conn, principal, user_id: str) -> None:
    """Admin: turn off another admin's two-factor login (lost phone). They set it up again at next login."""
    action = "user.reset_two_factor"
    p = _authorize(conn, principal, action, "admin")
    if user_id == p.user_id:
        _deny(conn, p, action, "user", user_id, "cannot reset your own two-factor login")
    if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
        raise dl.NotFoundError(f"user not found: {user_id}")
    two_factor.reset(conn, user_id)
    _ok(conn, p, action, "user", user_id, "two-factor reset; logged out everywhere")


def list_users(conn, principal, *, search: Optional[str] = None, role: Optional[str] = None,
               limit: int = 100) -> list:
    """Admin: find accounts by email or name, newest first."""
    action = "user.list"
    p = _authorize(conn, principal, action, "admin")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if role is not None and role not in ("candidate", "employer", "admin"):
        raise ValueError("role must be candidate, employer or admin")
    where, params = [], []
    if search:
        # % and _ are searched for literally, not as wildcards
        escaped = str(search).strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        term = f"%{escaped}%"
        where.append("(lower(u.email) LIKE ? ESCAPE '\\' OR lower(c.name) LIKE ? ESCAPE '\\' "
                     "OR lower(e.company_name) LIKE ? ESCAPE '\\')")
        params += [term, term, term]
    if role:
        where.append("u.role = ?")
        params.append(role)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        "SELECT u.id AS user_id, u.email, u.role, u.is_active, u.email_verified_at IS NOT NULL AS email_verified, "
        "u.created_at, c.id AS candidate_id, c.name AS candidate_name, e.id AS employer_id, e.company_name "
        "FROM users u LEFT JOIN candidates c ON c.user_id = u.id LEFT JOIN employers e ON e.user_id = u.id "
        f"{clause} ORDER BY u.created_at DESC, u.id LIMIT ?",
        (*params, limit),
    ).fetchall()
    _ok(conn, p, action, None, None, f"{len(rows)} rows")
    out = []
    for r in rows:
        row = dict(r)
        row["is_active"], row["email_verified"] = bool(row["is_active"]), bool(row["email_verified"])
        row["closed"] = is_closed_email(row["email"])
        out.append(row)
    return out


def list_catalog(conn, principal, kind: str) -> list:
    """Known licence, qualification or skill names, for suggestions while typing. Any logged-in user."""
    _authorize(conn, principal, "catalog.list", "candidate", "employer", "admin")
    return dl.list_items(conn, kind)


def read_audit_log(conn, principal, *, user_id: Optional[str] = None, resource_type: Optional[str] = None,
                   resource_id: Optional[str] = None, outcome: Optional[str] = None,
                   limit: int = 100) -> list:
    """Admin only. Newest first. Reading the log is itself logged."""
    action = "audit.read"
    p = _authorize(conn, principal, action, "admin")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= AUDIT_MAX_ROWS:
        raise ValueError(f"limit must be between 1 and {AUDIT_MAX_ROWS}")
    where, params = [], []
    for column, value in (("user_id", user_id), ("resource_type", resource_type),
                          ("resource_id", resource_id), ("outcome", outcome)):
        if value is not None:
            where.append(f"{column} = ?")
            params.append(value)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(f"SELECT * FROM audit_log{clause} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
    _ok(conn, p, action, None, None, f"{len(rows)} rows")
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# For other guarded modules (billing.py): the same checks and the same audit trail.
# --------------------------------------------------------------------------
authorize = _authorize
audit_allowed = _ok
deny = _deny
employer_scope = _employer_scope
display_label = _label
