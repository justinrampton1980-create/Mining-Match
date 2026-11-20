"""Mining Match - data layer (SQLite, standard library only).

Reads and writes the schema in db/schema.sql and hands the matching engine
the plain-dict shapes it expects (see matching_engine.py / README.md), so:

    candidates = load_candidates(conn)            # engine-shaped dicts
    job = get_job(conn, job_id)                   # engine-shaped dict
    rank_candidates_for_job(conn, job_id)         # DB -> engine -> ranked results

Conventions
  * Every public function takes the connection first and commits its own work.
  * Multi-step writes (register, create_job, ...) are atomic.
  * Access control is NOT done here: callers (API layer) decide who may read or
    write what. This layer only enforces data integrity.
"""
from __future__ import annotations

import math
import re
import sqlite3
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import matching_engine as me

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

KINDS = ("licence", "qualification", "skill")
JOB_STATUSES = ("draft", "open", "filled", "closed")
SHORTLIST_STATUSES = ("shortlisted", "contacted", "interviewing", "offered", "placed", "rejected")
DOCUMENT_KINDS = ("licence", "qualification", "resume", "other")


class DataError(Exception):
    """Base class for data-layer errors."""


class NotFoundError(DataError):
    pass


class DuplicateError(DataError):
    pass


# --------------------------------------------------------------------------
# Connection / schema
# --------------------------------------------------------------------------
def connect(path: str = ":memory:", *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection with foreign keys on.

    check_same_thread=False lets a web framework use the connection from another worker
    thread; only do that with one connection per request.
    """
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes (safe to call on every start-up)."""
    conn.executescript(SCHEMA_PATH.read_text())


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _new_id() -> str:
    return uuid.uuid4().hex


def _iso(value: Any) -> Optional[str]:
    """date / datetime / 'YYYY-MM-DD' -> 'YYYY-MM-DD' (or None). Raises ValueError if invalid."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)[:10]).isoformat()


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower()).replace("license", "licence")


def _marks(items: list) -> str:
    return ",".join("?" * len(items))


def _bool(value: Any) -> int:
    return 1 if value else 0


def _round1(x: float) -> float:
    return math.floor(x * 10 + 0.5) / 10


_TABLES = {"users", "candidates", "employers", "jobs", "shortlists", "documents"}


def _require(conn: sqlite3.Connection, table: str, row_id: str, label: str) -> None:
    if table not in _TABLES:
        raise ValueError(f"bad table: {table}")
    if conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)).fetchone() is None:
        raise NotFoundError(f"{label} not found: {row_id}")


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")


# --------------------------------------------------------------------------
# Catalog (licences / qualifications / skills) and aliases
# --------------------------------------------------------------------------
def _item_id(conn: sqlite3.Connection, kind: str, name: str) -> str:
    """Find or create a catalog item (no commit; used inside larger transactions)."""
    _check_kind(kind)
    display = " ".join(str(name or "").split())
    if not display:
        raise ValueError(f"{kind} name is required")
    key = me.normalize(display)
    row = conn.execute(
        "SELECT id FROM catalog_items WHERE kind = ? AND normalized_name = ?", (kind, key)
    ).fetchone()
    if row:
        return row["id"]
    item_id = _new_id()
    conn.execute(
        "INSERT INTO catalog_items (id, kind, name, normalized_name) VALUES (?, ?, ?, ?)",
        (item_id, kind, display, key),
    )
    return item_id


def get_or_create_item(conn: sqlite3.Connection, kind: str, name: str) -> str:
    with conn:
        return _item_id(conn, kind, name)


def seed_catalog(conn: sqlite3.Connection, path: Optional[Path] = None) -> int:
    """Add the common licence, qualification and skill names (safe to run on every start-up).

    Reads db/catalog_seed.json: {"licence": [...], "qualification": [...], "skill": [...]}. An entry
    is a name, or {"name": ..., "renew_years": ..., "note": ...} for a licence with a usual renewal
    cycle; the file is the source of truth for those rules. Returns how many names it added.
    """
    import json

    path = path or SCHEMA_PATH.parent / "catalog_seed.json"
    if not path.exists():
        return 0
    data = json.loads(path.read_text())
    before = conn.execute("SELECT COUNT(*) FROM catalog_items").fetchone()[0]
    with conn:
        for kind in KINDS:
            for entry in data.get(kind, []):
                if isinstance(entry, dict):
                    item = _item_id(conn, kind, entry["name"])
                    conn.execute("UPDATE catalog_items SET renew_years = ?, renew_note = ? WHERE id = ?",
                                 (entry.get("renew_years"), entry.get("note"), item))
                else:
                    _item_id(conn, kind, entry)
    return conn.execute("SELECT COUNT(*) FROM catalog_items").fetchone()[0] - before


def list_items(conn: sqlite3.Connection, kind: str) -> list:
    _check_kind(kind)
    rows = conn.execute(
        "SELECT id, name FROM catalog_items WHERE kind = ? ORDER BY normalized_name", (kind,)
    ).fetchall()
    return [dict(r) for r in rows]


def add_alias(conn: sqlite3.Connection, alias: str, canonical: str) -> None:
    """Teach the system that `alias` means `canonical` (e.g. 'SS11' -> 'Standard 11').

    Add aliases BEFORE data using the alias is entered; existing rows are not merged.
    """
    a, c = _clean(alias), _clean(canonical)
    if not a or not c:
        raise ValueError("alias and canonical are required")
    with conn:
        conn.execute(
            "INSERT INTO catalog_aliases (alias, canonical) VALUES (?, ?) "
            "ON CONFLICT(alias) DO UPDATE SET canonical = excluded.canonical",
            (a, c),
        )
    me.register_aliases({a: c})


def load_aliases(conn: sqlite3.Connection) -> int:
    """Load stored aliases into the matching engine. Call once at start-up."""
    rows = conn.execute("SELECT alias, canonical FROM catalog_aliases").fetchall()
    me.register_aliases({r["alias"]: r["canonical"] for r in rows})
    return len(rows)


# --------------------------------------------------------------------------
# Registration (users + profiles)
# --------------------------------------------------------------------------
def _create_user(
    conn: sqlite3.Connection, email: str, role: str, password_hash: Optional[str], email_verified: bool = False
) -> str:
    email = str(email or "").strip().lower()
    if "@" not in email:
        raise ValueError("a valid email is required")
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        raise DuplicateError(f"email already registered: {email}")
    user_id = _new_id()
    conn.execute(
        "INSERT INTO users (id, email, password_hash, role, email_verified_at) "
        "VALUES (?, ?, ?, ?, CASE WHEN ? THEN CURRENT_TIMESTAMP END)",
        (user_id, email, password_hash, role, 1 if email_verified else 0),
    )
    return user_id


def register_candidate(
    conn: sqlite3.Connection,
    email: str,
    name: str,
    *,
    location: Optional[str] = None,
    phone: Optional[str] = None,
    willing_to_relocate: bool = False,
    willing_fifo: bool = False,
    available_from: Any = None,
    summary: Optional[str] = None,
    password_hash: Optional[str] = None,
    email_verified: bool = False,
) -> str:
    """Create a user + candidate profile. Returns the candidate id.

    Unverified accounts can't log in and are left out of searches until the email is confirmed.
    """
    if not str(name or "").strip():
        raise ValueError("name is required")
    with conn:
        user_id = _create_user(conn, email, "candidate", password_hash, email_verified)
        candidate_id = _new_id()
        conn.execute(
            "INSERT INTO candidates (id, user_id, name, phone, location, willing_to_relocate, "
            "willing_fifo, available_from, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate_id, user_id, name.strip(), phone, location, _bool(willing_to_relocate),
                _bool(willing_fifo), _iso(available_from), summary,
            ),
        )
    return candidate_id


def register_employer(
    conn: sqlite3.Connection,
    email: str,
    company_name: str,
    *,
    contact_name: Optional[str] = None,
    phone: Optional[str] = None,
    location: Optional[str] = None,
    password_hash: Optional[str] = None,
    email_verified: bool = False,
) -> str:
    """Create a user + employer profile. Returns the employer id."""
    if not str(company_name or "").strip():
        raise ValueError("company_name is required")
    with conn:
        user_id = _create_user(conn, email, "employer", password_hash, email_verified)
        employer_id = _new_id()
        conn.execute(
            "INSERT INTO employers (id, user_id, company_name, contact_name, phone, location) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (employer_id, user_id, company_name.strip(), contact_name, phone, location),
        )
    return employer_id


def register_admin(
    conn: sqlite3.Connection, email: str, *, password_hash: Optional[str] = None, email_verified: bool = False
) -> str:
    """Create an admin user. Returns the user id.

    Unguarded: call access_control.create_admin, never this, from anything a user can reach.
    """
    with conn:
        return _create_user(conn, email, "admin", password_hash, email_verified)


_CANDIDATE_FIELDS = {
    "name", "phone", "location", "willing_to_relocate", "willing_fifo",
    "available_from", "summary", "is_active", "expiry_reminders",
}


def update_candidate(conn: sqlite3.Connection, candidate_id: str, **fields: Any) -> None:
    unknown = set(fields) - _CANDIDATE_FIELDS
    if unknown:
        raise ValueError(f"unknown candidate fields: {sorted(unknown)}")
    if not fields:
        return
    values = []
    for key, value in fields.items():
        if key in ("willing_to_relocate", "willing_fifo", "is_active", "expiry_reminders"):
            value = _bool(value)
        elif key == "available_from":
            value = _iso(value)
        values.append(value)
    assignments = ", ".join(f"{k} = ?" for k in fields)  # keys come from the whitelist above
    with conn:
        cur = conn.execute(
            f"UPDATE candidates SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (*values, candidate_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"candidate not found: {candidate_id}")


def delete_candidate(conn: sqlite3.Connection, candidate_id: str) -> list:
    """Delete a candidate, their login and everything attached (privacy / account removal).

    Returns the storage_keys of their uploaded documents so the caller can delete the files.
    """
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise DataError("foreign keys must be ON for cascading deletes (use connect())")
    with conn:
        row = conn.execute("SELECT user_id FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"candidate not found: {candidate_id}")
        keys = [
            r["storage_key"]
            for r in conn.execute("SELECT storage_key FROM documents WHERE candidate_id = ?", (candidate_id,))
        ]
        conn.execute("DELETE FROM users WHERE id = ?", (row["user_id"],))  # cascades to the candidate
    return keys


# --------------------------------------------------------------------------
# Candidate profile: documents, licences, qualifications, skills, experience
# --------------------------------------------------------------------------
def add_document(
    conn: sqlite3.Connection,
    candidate_id: str,
    kind: str,
    filename: str,
    storage_key: str,
    *,
    mime_type: Optional[str] = None,
    size_bytes: Optional[int] = None,
) -> str:
    if kind not in DOCUMENT_KINDS:
        raise ValueError(f"document kind must be one of {DOCUMENT_KINDS}")
    with conn:
        _require(conn, "candidates", candidate_id, "candidate")
        doc_id = _new_id()
        try:
            conn.execute(
                "INSERT INTO documents (id, candidate_id, kind, filename, mime_type, size_bytes, storage_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (doc_id, candidate_id, kind, filename, mime_type, size_bytes, storage_key),
            )
        except sqlite3.IntegrityError as exc:
            if "storage_key" in str(exc):
                raise DuplicateError(f"storage_key already used: {storage_key}") from exc
            raise
    return doc_id


def list_documents(conn: sqlite3.Connection, candidate_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM documents WHERE candidate_id = ? ORDER BY uploaded_at, filename", (candidate_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def verify_document(conn: sqlite3.Connection, document_id: str) -> None:
    with conn:
        cur = conn.execute(
            "UPDATE documents SET verified_at = CURRENT_TIMESTAMP WHERE id = ?", (document_id,)
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"document not found: {document_id}")


def _check_document(conn: sqlite3.Connection, candidate_id: str, document_id: Optional[str]) -> None:
    if document_id is None:
        return
    row = conn.execute("SELECT candidate_id FROM documents WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"document not found: {document_id}")
    if row["candidate_id"] != candidate_id:
        raise DataError("document belongs to a different candidate")


def set_candidate_licence(
    conn: sqlite3.Connection,
    candidate_id: str,
    name: str,
    expiry: Any = None,
    document_id: Optional[str] = None,
) -> None:
    """Add a licence, or update its expiry on renewal. expiry=None means no expiry."""
    expiry = _iso(expiry)
    with conn:
        _require(conn, "candidates", candidate_id, "candidate")
        _check_document(conn, candidate_id, document_id)
        item = _item_id(conn, "licence", name)
        conn.execute(
            "INSERT INTO candidate_licences (candidate_id, item_id, expiry_date, document_id) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(candidate_id, item_id) DO UPDATE SET "
            "expiry_date = excluded.expiry_date, "
            "document_id = COALESCE(excluded.document_id, candidate_licences.document_id)",
            (candidate_id, item, expiry, document_id),
        )


def set_candidate_qualification(
    conn: sqlite3.Connection,
    candidate_id: str,
    name: str,
    *,
    awarded_date: Any = None,
    institution: Optional[str] = None,
    document_id: Optional[str] = None,
) -> None:
    awarded = _iso(awarded_date)
    with conn:
        _require(conn, "candidates", candidate_id, "candidate")
        _check_document(conn, candidate_id, document_id)
        item = _item_id(conn, "qualification", name)
        conn.execute(
            "INSERT INTO candidate_qualifications (candidate_id, item_id, awarded_date, institution, document_id) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(candidate_id, item_id) DO UPDATE SET "
            "awarded_date = excluded.awarded_date, institution = excluded.institution, "
            "document_id = COALESCE(excluded.document_id, candidate_qualifications.document_id)",
            (candidate_id, item, awarded, institution, document_id),
        )


def set_candidate_skill(
    conn: sqlite3.Connection, candidate_id: str, name: str, years: Optional[float] = None
) -> None:
    """Add a skill or update its years. years=None means 'years not stated'."""
    if years is not None and years < 0:
        raise ValueError("years must be >= 0")
    with conn:
        _require(conn, "candidates", candidate_id, "candidate")
        item = _item_id(conn, "skill", name)
        conn.execute(
            "INSERT INTO candidate_skills (candidate_id, item_id, years) VALUES (?, ?, ?) "
            "ON CONFLICT(candidate_id, item_id) DO UPDATE SET years = excluded.years",
            (candidate_id, item, years),
        )


_LINK_TABLES = {
    "licence": "candidate_licences",
    "qualification": "candidate_qualifications",
    "skill": "candidate_skills",
}


def _remove_link(conn: sqlite3.Connection, kind: str, candidate_id: str, name: str) -> bool:
    table = _LINK_TABLES[kind]
    with conn:
        cur = conn.execute(
            f"DELETE FROM {table} WHERE candidate_id = ? AND item_id IN "
            "(SELECT id FROM catalog_items WHERE kind = ? AND normalized_name = ?)",
            (candidate_id, kind, me.normalize(name)),
        )
    return cur.rowcount > 0


def remove_candidate_licence(conn: sqlite3.Connection, candidate_id: str, name: str) -> bool:
    return _remove_link(conn, "licence", candidate_id, name)


def remove_candidate_qualification(conn: sqlite3.Connection, candidate_id: str, name: str) -> bool:
    return _remove_link(conn, "qualification", candidate_id, name)


def remove_candidate_skill(conn: sqlite3.Connection, candidate_id: str, name: str) -> bool:
    return _remove_link(conn, "skill", candidate_id, name)


def add_experience(
    conn: sqlite3.Connection,
    candidate_id: str,
    role: str,
    start_date: Any,
    end_date: Any = None,
    *,
    employer_name: Optional[str] = None,
) -> str:
    """Add a work-history entry. end_date=None means the candidate is still in the role."""
    if not str(role or "").strip():
        raise ValueError("role is required")
    start, end = _iso(start_date), _iso(end_date)
    if start is None:
        raise ValueError("start_date is required")
    if end is not None and end < start:
        raise ValueError("end_date is before start_date")
    with conn:
        _require(conn, "candidates", candidate_id, "candidate")
        exp_id = _new_id()
        conn.execute(
            "INSERT INTO candidate_experience (id, candidate_id, role, employer_name, start_date, end_date) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (exp_id, candidate_id, role.strip(), employer_name, start, end),
        )
    return exp_id


def remove_experience(conn: sqlite3.Connection, experience_id: str) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM candidate_experience WHERE id = ?", (experience_id,))
    return cur.rowcount > 0


# --------------------------------------------------------------------------
# Loading candidates in the matching engine's shape
# --------------------------------------------------------------------------
def _years_between(start: str, end: date) -> float:
    days = (end - date.fromisoformat(start)).days
    return _round1(max(0.0, days / 365.25))


def load_candidates(
    conn: sqlite3.Connection,
    candidate_ids: Optional[Iterable[str]] = None,
    *,
    active_only: bool = True,
    as_of: Any = None,
) -> list:
    """Load candidates as engine-shaped dicts using 5 queries however many there are.

    active_only skips hidden profiles, suspended accounts and unconfirmed emails.
    Experience years come from the dates; open-ended roles run to `as_of` (default today).
    """
    where, params = [], []
    if active_only:
        where.append("is_active = 1")
        where.append("user_id IN (SELECT id FROM users WHERE is_active = 1 AND email_verified_at IS NOT NULL)")
    if candidate_ids is not None:
        ids = list(candidate_ids)
        if not ids:
            return []
        where.append(f"id IN ({_marks(ids)})")
        params += ids
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    sub = f"SELECT id FROM candidates{clause}"
    today = date.fromisoformat(_iso(as_of)) if as_of else date.today()

    rows = conn.execute(f"SELECT * FROM candidates{clause} ORDER BY name, id", params).fetchall()
    out = {
        r["id"]: {
            "id": r["id"],
            "name": r["name"],
            "location": r["location"],
            "willing_to_relocate": bool(r["willing_to_relocate"]),
            "willing_fifo": bool(r["willing_fifo"]),
            "available_from": r["available_from"],
            "licences": [],
            "qualifications": [],
            "skills": [],
            "experience": [],
        }
        for r in rows
    }
    for r in conn.execute(
        "SELECT cl.candidate_id, ci.name, cl.expiry_date FROM candidate_licences cl "
        f"JOIN catalog_items ci ON ci.id = cl.item_id WHERE cl.candidate_id IN ({sub}) "
        "ORDER BY ci.normalized_name",
        params,
    ):
        out[r["candidate_id"]]["licences"].append({"type": r["name"], "expiry": r["expiry_date"]})
    for r in conn.execute(
        "SELECT cq.candidate_id, ci.name FROM candidate_qualifications cq "
        f"JOIN catalog_items ci ON ci.id = cq.item_id WHERE cq.candidate_id IN ({sub}) "
        "ORDER BY ci.normalized_name",
        params,
    ):
        out[r["candidate_id"]]["qualifications"].append(r["name"])
    for r in conn.execute(
        "SELECT cs.candidate_id, ci.name, cs.years FROM candidate_skills cs "
        f"JOIN catalog_items ci ON ci.id = cs.item_id WHERE cs.candidate_id IN ({sub}) "
        "ORDER BY ci.normalized_name",
        params,
    ):
        out[r["candidate_id"]]["skills"].append({"name": r["name"], "years": r["years"]})
    for r in conn.execute(
        "SELECT candidate_id, role, start_date, end_date FROM candidate_experience "
        f"WHERE candidate_id IN ({sub}) ORDER BY start_date, id",
        params,
    ):
        end = date.fromisoformat(r["end_date"]) if r["end_date"] else today
        out[r["candidate_id"]]["experience"].append(
            {"role": r["role"], "years": _years_between(r["start_date"], end)}
        )
    return list(out.values())


# --------------------------------------------------------------------------
# Jobs (engine-shaped in and out)
# --------------------------------------------------------------------------
_REQUIREMENT_KEYS = (
    ("required_licences", "licence", True),
    ("preferred_licences", "licence", False),
    ("required_qualifications", "qualification", True),
    ("preferred_qualifications", "qualification", False),
    ("required_skills", "skill", True),
    ("preferred_skills", "skill", False),
)
_PLURAL = {"licence": "licences", "qualification": "qualifications", "skill": "skills"}


def _write_job_content(conn: sqlite3.Connection, job_id: str, job: dict) -> None:
    """(Re)write requirements and relevant roles for a job (no commit)."""
    conn.execute("DELETE FROM job_requirements WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM job_relevant_roles WHERE job_id = ?", (job_id,))
    wanted: dict = {}  # item_id -> (kind, required, min_years); required wins over preferred
    for key, kind, required in _REQUIREMENT_KEYS:
        for entry in job.get(key) or []:
            if isinstance(entry, dict):
                name, min_years = entry.get("name"), entry.get("min_years")
            else:
                name, min_years = entry, None
            if min_years is not None and (kind != "skill" or min_years < 0):
                raise ValueError(f"min_years only applies to skills and must be >= 0 ({name})")
            item = _item_id(conn, kind, name)
            previous = wanted.get(item)
            if previous is None or (required and not previous[1]):
                wanted[item] = (kind, required, min_years)
    for item, (kind, required, min_years) in wanted.items():
        conn.execute(
            "INSERT INTO job_requirements (job_id, item_id, kind, required, min_years) VALUES (?, ?, ?, ?, ?)",
            (job_id, item, kind, _bool(required), min_years),
        )
    for role in job.get("relevant_roles") or []:
        role = " ".join(str(role).split())
        if role:
            conn.execute(
                "INSERT INTO job_relevant_roles (job_id, role) VALUES (?, ?) ON CONFLICT DO NOTHING",
                (job_id, role),
            )


def create_job(conn: sqlite3.Connection, employer_id: str, job: dict, *, status: str = "draft") -> str:
    """Create a job from an engine-shaped dict (requirement lists use the engine's keys)."""
    if status not in JOB_STATUSES:
        raise ValueError(f"status must be one of {JOB_STATUSES}")
    if not str(job.get("title") or "").strip():
        raise ValueError("title is required")
    with conn:
        _require(conn, "employers", employer_id, "employer")
        job_id = _new_id()
        conn.execute(
            "INSERT INTO jobs (id, employer_id, title, description, location, start_date, "
            "min_experience_years, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id, employer_id, job["title"].strip(), job.get("description"), job.get("location"),
                _iso(job.get("start_date")), job.get("min_experience_years"), status,
            ),
        )
        _write_job_content(conn, job_id, job)
    return job_id


def update_job(conn: sqlite3.Connection, job_id: str, job: dict) -> None:
    """Replace a job's content (PUT semantics: requirement lists not given are cleared).

    Use set_job_status to change status.
    """
    if not str(job.get("title") or "").strip():
        raise ValueError("title is required")
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET title = ?, description = ?, location = ?, start_date = ?, "
            "min_experience_years = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (
                job["title"].strip(), job.get("description"), job.get("location"),
                _iso(job.get("start_date")), job.get("min_experience_years"), job_id,
            ),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"job not found: {job_id}")
        _write_job_content(conn, job_id, job)


def set_job_status(conn: sqlite3.Connection, job_id: str, status: str) -> None:
    if status not in JOB_STATUSES:
        raise ValueError(f"status must be one of {JOB_STATUSES}")
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, job_id)
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"job not found: {job_id}")


def load_jobs(
    conn: sqlite3.Connection,
    *,
    job_ids: Optional[Iterable[str]] = None,
    status: Optional[str] = None,
    employer_id: Optional[str] = None,
    active_employers_only: bool = False,
) -> list:
    """Load jobs as engine-shaped dicts (4 queries regardless of count).

    active_employers_only skips jobs whose employer account is suspended or unconfirmed.
    """
    where, params = [], []
    if job_ids is not None:
        ids = list(job_ids)
        if not ids:
            return []
        where.append(f"id IN ({_marks(ids)})")
        params += ids
    if status:
        where.append("status = ?")
        params.append(status)
    if employer_id:
        where.append("employer_id = ?")
        params.append(employer_id)
    if active_employers_only:
        where.append(
            "employer_id IN (SELECT e.id FROM employers e JOIN users u ON u.id = e.user_id "
            "WHERE u.is_active = 1 AND u.email_verified_at IS NOT NULL)"
        )
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    sub = f"SELECT id FROM jobs{clause}"

    rows = conn.execute(f"SELECT * FROM jobs{clause} ORDER BY created_at DESC, title, id", params).fetchall()
    out = {}
    for r in rows:
        job = {
            "id": r["id"],
            "employer_id": r["employer_id"],
            "title": r["title"],
            "description": r["description"],
            "location": r["location"],
            "start_date": r["start_date"],
            "min_experience_years": r["min_experience_years"],
            "status": r["status"],
            "relevant_roles": [],
        }
        for key, _, _ in _REQUIREMENT_KEYS:
            job[key] = []
        out[r["id"]] = job
    for r in conn.execute(
        "SELECT jr.job_id, jr.kind, jr.required, jr.min_years, ci.name FROM job_requirements jr "
        f"JOIN catalog_items ci ON ci.id = jr.item_id WHERE jr.job_id IN ({sub}) "
        "ORDER BY ci.normalized_name",
        params,
    ):
        key = f"{'required' if r['required'] else 'preferred'}_{_PLURAL[r['kind']]}"
        entry = {"name": r["name"], "min_years": r["min_years"]} if r["kind"] == "skill" else r["name"]
        out[r["job_id"]][key].append(entry)
    for r in conn.execute(
        f"SELECT job_id, role FROM job_relevant_roles WHERE job_id IN ({sub}) ORDER BY role", params
    ):
        out[r["job_id"]]["relevant_roles"].append(r["role"])
    return list(out.values())


def get_job(conn: sqlite3.Connection, job_id: str) -> dict:
    jobs = load_jobs(conn, job_ids=[job_id])
    if not jobs:
        raise NotFoundError(f"job not found: {job_id}")
    return jobs[0]


# --------------------------------------------------------------------------
# Matching (DB -> engine -> ranked results)
# --------------------------------------------------------------------------
def _rank_candidates(conn: sqlite3.Connection, job: dict, as_of: Any, options: dict) -> list:
    candidates = load_candidates(conn, as_of=as_of)
    names = {c["id"]: c["name"] for c in candidates}
    ranked = me.rank_candidates(candidates, job, as_of=as_of, **options)
    return [{**r.to_dict(), "candidate_name": names[r.candidate_id]} for r in ranked]


def rank_candidates_for_job(conn: sqlite3.Connection, job_id: str, *, as_of: Any = None, **options: Any) -> list:
    """Rank all active candidates for a stored job.

    options: weights, min_score, limit, eligible_only (see matching_engine.rank_candidates).
    Returns result dicts (engine contract) plus candidate_name.
    """
    return _rank_candidates(conn, get_job(conn, job_id), as_of, options)


def search_candidates(conn: sqlite3.Connection, criteria: dict, *, as_of: Any = None, **options: Any) -> list:
    """Employer search: rank active candidates against ad-hoc criteria (an engine-shaped job dict)."""
    return _rank_candidates(conn, dict(criteria), as_of, options)


def rank_jobs_for_candidate(
    conn: sqlite3.Connection, candidate_id: str, *, as_of: Any = None, **options: Any
) -> list:
    """Rank open jobs (from active employers) for a candidate. Returns result dicts plus job_title."""
    found = load_candidates(conn, [candidate_id], active_only=False, as_of=as_of)
    if not found:
        raise NotFoundError(f"candidate not found: {candidate_id}")
    jobs = load_jobs(conn, status="open", active_employers_only=True)
    titles = {j["id"]: j["title"] for j in jobs}
    ranked = me.match_jobs_for_candidate(found[0], jobs, as_of=as_of, **options)
    return [{**r.to_dict(), "job_title": titles[r.job_id]} for r in ranked]


# --------------------------------------------------------------------------
# Shortlists
# --------------------------------------------------------------------------
def create_shortlist(conn: sqlite3.Connection, employer_id: str, name: str, job_id: Optional[str] = None) -> str:
    if not str(name or "").strip():
        raise ValueError("name is required")
    with conn:
        _require(conn, "employers", employer_id, "employer")
        if job_id is not None:
            row = conn.execute("SELECT employer_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"job not found: {job_id}")
            if row["employer_id"] != employer_id:
                raise DataError("job belongs to a different employer")
        shortlist_id = _new_id()
        conn.execute(
            "INSERT INTO shortlists (id, employer_id, job_id, name) VALUES (?, ?, ?, ?)",
            (shortlist_id, employer_id, job_id, name.strip()),
        )
    return shortlist_id


def list_shortlists(conn: sqlite3.Connection, employer_id: str) -> list:
    rows = conn.execute(
        "SELECT s.id, s.name, s.job_id, s.created_at, "
        "(SELECT COUNT(*) FROM shortlist_entries e WHERE e.shortlist_id = s.id) AS entries "
        "FROM shortlists s WHERE s.employer_id = ? ORDER BY s.created_at DESC, s.name",
        (employer_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_to_shortlist(
    conn: sqlite3.Connection,
    shortlist_id: str,
    candidate_id: str,
    *,
    notes: Optional[str] = None,
    score: Optional[float] = None,
    as_of: Any = None,
) -> None:
    """Add a candidate (or update notes if already there; status and first score are kept).

    If the shortlist is linked to a job and no score is given, the engine score is stored.
    """
    with conn:
        sl = conn.execute("SELECT job_id FROM shortlists WHERE id = ?", (shortlist_id,)).fetchone()
        if sl is None:
            raise NotFoundError(f"shortlist not found: {shortlist_id}")
        _require(conn, "candidates", candidate_id, "candidate")
        if score is None and sl["job_id"]:
            candidate = load_candidates(conn, [candidate_id], active_only=False, as_of=as_of)[0]
            score = me.score_candidate(candidate, get_job(conn, sl["job_id"]), as_of=as_of).score
        conn.execute(
            "INSERT INTO shortlist_entries (shortlist_id, candidate_id, notes, score_at_add) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(shortlist_id, candidate_id) DO UPDATE SET "
            "notes = COALESCE(excluded.notes, shortlist_entries.notes), updated_at = CURRENT_TIMESTAMP",
            (shortlist_id, candidate_id, notes, score),
        )


def set_shortlist_status(conn: sqlite3.Connection, shortlist_id: str, candidate_id: str, status: str) -> None:
    if status not in SHORTLIST_STATUSES:
        raise ValueError(f"status must be one of {SHORTLIST_STATUSES}")
    with conn:
        cur = conn.execute(
            "UPDATE shortlist_entries SET status = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE shortlist_id = ? AND candidate_id = ?",
            (status, shortlist_id, candidate_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError("candidate is not on that shortlist")


def remove_from_shortlist(conn: sqlite3.Connection, shortlist_id: str, candidate_id: str) -> bool:
    with conn:
        cur = conn.execute(
            "DELETE FROM shortlist_entries WHERE shortlist_id = ? AND candidate_id = ?",
            (shortlist_id, candidate_id),
        )
    return cur.rowcount > 0


def get_shortlist(conn: sqlite3.Connection, shortlist_id: str) -> dict:
    head = conn.execute("SELECT * FROM shortlists WHERE id = ?", (shortlist_id,)).fetchone()
    if head is None:
        raise NotFoundError(f"shortlist not found: {shortlist_id}")
    entries = conn.execute(
        "SELECT e.candidate_id, c.name, e.status, e.notes, e.score_at_add, e.added_at "
        "FROM shortlist_entries e JOIN candidates c ON c.id = e.candidate_id "
        "WHERE e.shortlist_id = ? ORDER BY (e.score_at_add IS NULL), e.score_at_add DESC, c.name",
        (shortlist_id,),
    ).fetchall()
    return {
        "id": head["id"],
        "employer_id": head["employer_id"],
        "job_id": head["job_id"],
        "name": head["name"],
        "entries": [dict(e) for e in entries],
    }
