"""Mining Match - HTTP API (FastAPI).

Every endpoint goes through access_control (which checks the rules and writes the audit
log) or auth (sign-up, login, sessions). Interactive docs are at /docs while enabled.

Run for development:
    pip install -r requirements.txt
    MM_DB=mining_match.db MM_FILES=uploads MM_BASE_URL=http://localhost:3000 uvicorn api:app --reload

Clients send the session token as:  Authorization: Bearer <token>

Error codes: 400 bad input, 401 not logged in, 403 not allowed, 404 not found,
409 conflict (e.g. fee terms changed), 413 file too large, 429 too many attempts.
"""

import logging
import os
import sqlite3
from pathlib import Path
from typing import Annotated, Any, Callable, Optional, Union

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

import access_control as ac
import auth
import billing
import monitoring
import data_layer as dl
import reminders
from storage import LocalStorage, sniff_type

log = logging.getLogger("mining_match")

# ------------------------------------------------------------------ models
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
OptShort = Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]]
Email = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=254)]
Password = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Token = Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=200)]
DateStr = Optional[Annotated[str, StringConstraints(strip_whitespace=True, max_length=10)]]
Id = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Names = Annotated[list[Short], Field(max_length=50)]


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterCandidateIn(In):
    email: Email
    password: Password
    name: Short
    location: OptShort = None
    phone: OptShort = None


class RegisterEmployerIn(In):
    email: Email
    password: Password
    company_name: Short
    contact_name: OptShort = None
    phone: OptShort = None
    location: OptShort = None


class LoginIn(In):
    email: Email
    password: Password
    use_cookie: bool = False  # the website: session kept in an HttpOnly cookie, token not returned


class TokenIn(In):
    token: Token


class EmailIn(In):
    email: Email


class ResetIn(In):
    token: Token
    new_password: Password


class ChangePasswordIn(In):
    current_password: Password
    new_password: Password


class CandidateUpdateIn(In):
    name: Optional[Short] = None
    phone: OptShort = None
    location: OptShort = None
    willing_to_relocate: Optional[bool] = None
    willing_fifo: Optional[bool] = None
    available_from: DateStr = None
    summary: Optional[Annotated[str, StringConstraints(max_length=2000)]] = None
    is_active: Optional[bool] = None
    expiry_reminders: Optional[bool] = None


class LicenceIn(In):
    name: Short
    expiry: DateStr = None
    document_id: Optional[Id] = None


class QualificationIn(In):
    name: Short
    awarded_date: DateStr = None
    institution: OptShort = None
    document_id: Optional[Id] = None


class SkillIn(In):
    name: Short
    years: Optional[Annotated[float, Field(ge=0, le=60)]] = None


class ExperienceIn(In):
    role: Short
    start_date: Annotated[str, StringConstraints(strip_whitespace=True, max_length=10)]
    end_date: DateStr = None
    employer_name: OptShort = None


class SkillReq(In):
    name: Short
    min_years: Optional[Annotated[float, Field(ge=0, le=60)]] = None


SkillList = Annotated[list[Union[Short, SkillReq]], Field(max_length=50)]


class Criteria(In):
    """Search criteria, in the matching engine's shape."""
    location: OptShort = None
    start_date: DateStr = None
    min_experience_years: Optional[Annotated[float, Field(ge=0, le=60)]] = None
    required_licences: Names = []
    preferred_licences: Names = []
    required_qualifications: Names = []
    preferred_qualifications: Names = []
    required_skills: SkillList = []
    preferred_skills: SkillList = []
    relevant_roles: Names = []

    def engine(self) -> dict:
        return self.model_dump(exclude_none=True)


class JobIn(Criteria):
    title: Short
    description: Optional[Annotated[str, StringConstraints(max_length=10_000)]] = None


class JobCreateIn(JobIn):
    status: Annotated[str, StringConstraints(max_length=10)] = "draft"
    employer_id: Optional[Id] = None  # admins only


class StatusIn(In):
    status: Annotated[str, StringConstraints(max_length=20)]


Weights = Optional[dict[Annotated[str, StringConstraints(max_length=20)], Annotated[float, Field(ge=0, le=100)]]]


class SearchIn(In):
    criteria: Criteria
    weights: Weights = None
    min_score: Annotated[float, Field(ge=0, le=100)] = 0
    eligible_only: bool = False
    limit: Optional[Annotated[int, Field(ge=1, le=1000)]] = None


class ContactRequestIn(In):
    job_id: Optional[Id] = None


class ReleaseIn(In):
    grant: bool


class UnlockIn(In):
    terms_version: Annotated[int, Field(ge=1)]
    job_id: Optional[Id] = None


class FeeTermsIn(In):
    fee_percent: Annotated[float, Field(gt=0, le=100)]
    fee_basis: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    window_months: Annotated[int, Field(ge=1, le=60)]
    guarantee_days: Annotated[int, Field(ge=0, le=730)]
    terms_text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100_000)]
    min_fee_aud: Optional[Annotated[float, Field(ge=0)]] = None


class ShortlistIn(In):
    name: Short
    job_id: Optional[Id] = None
    employer_id: Optional[Id] = None  # admins only


class EntryIn(In):
    candidate_id: Id
    notes: Optional[Annotated[str, StringConstraints(max_length=2000)]] = None


Money = Union[Annotated[float, Field(gt=0, le=10_000_000)], Annotated[str, StringConstraints(max_length=20)]]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class BillingDetailsIn(In):
    abn: Optional[Annotated[str, StringConstraints(max_length=20)]] = None
    billing_email: Optional[Annotated[str, StringConstraints(max_length=254)]] = None
    billing_address: Optional[Annotated[str, StringConstraints(max_length=300)]] = None
    employer_id: Optional[Id] = None  # admins only


class HireIn(In):
    candidate_id: Id
    start_date: Annotated[str, StringConstraints(strip_whitespace=True, max_length=10)]
    base_salary: Money  # first-year base salary in dollars
    job_id: Optional[Id] = None
    employer_id: Optional[Id] = None  # admins only


class ReasonIn(In):
    reason: Reason


class LineIn(In):
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    amount: Money  # dollars, excluding GST


class ServicesInvoiceIn(In):
    employer_id: Id
    lines: Annotated[list[LineIn], Field(min_length=1, max_length=10)]
    due_days: Optional[Annotated[int, Field(ge=1, le=90)]] = None


class PaidIn(In):
    paid_on: Annotated[str, StringConstraints(strip_whitespace=True, max_length=10)]
    note: Optional[Annotated[str, StringConstraints(max_length=200)]] = None


class UserActiveIn(In):
    active: bool
    reason: Optional[Annotated[str, StringConstraints(max_length=200)]] = None


# --------------------------------------------------------------- the app
NO_CSP_PATHS = ("/docs", "/redoc", "/openapi.json")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
HSTS = "max-age=31536000"  # browsers use HTTPS only for this site for a year
SESSION_COOKIE = "mm_session"
CSRF_HEADER, CSRF_VALUE = "x-requested-with", "mining-match"
# The website may load only its own files; no inline scripts, no other sites.
WEB_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data: blob:; "
           "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def create_app(db_path: str, *, storage, mailer: auth.Mailer, base_url: str,
               scan_upload: Optional[Callable[[bytes], bool]] = None,
               cors_origins: tuple = (), enable_docs: bool = True,
               web_dir: Optional[Path] = WEB_DIR, secure_cookies: bool = True,
               billing_config: Optional[billing.BillingConfig] = None,
               alert_email: Optional[str] = None) -> FastAPI:
    """Build the API.

    db_path      SQLite file (created and set up if needed)
    storage      object with save/read/delete (e.g. storage.LocalStorage)
    mailer       object with send(to, subject, body) (auth.SMTPMailer in production)
    base_url     the website address used in emailed links, e.g. https://miningmatch.com.au
    scan_upload  optional virus check: return False to reject a file (e.g. ClamAV)
    web_dir      folder with the website (served at /); None to serve the API only
    alert_email  where to email alerts about server errors and rate limits refusing people
                 (monitoring.py); None for no emails
    secure_cookies  send the session cookie over HTTPS only (turn off only for local testing)
    billing_config  invoicing settings (business name, ABN, bank details); None turns invoicing off
    """
    setup = dl.connect(db_path)
    dl.init_schema(setup)
    dl.load_aliases(setup)
    dl.seed_catalog(setup)
    setup.close()
    base_url = base_url.rstrip("/")

    app = FastAPI(
        title="Mining Match API", version="0.1.0",
        docs_url="/docs" if enable_docs else None, redoc_url=None,
        openapi_url="/openapi.json" if enable_docs else None,
    )
    if cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(cors_origins), allow_credentials=False,
                           allow_methods=["*"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.scheme == "https":  # behind nginx, uvicorn's --proxy-headers sets this
            response.headers["Strict-Transport-Security"] = HSTS
        path = request.url.path
        if path == "/" or path.startswith("/assets/"):
            response.headers["Content-Security-Policy"] = WEB_CSP
            response.headers["Cache-Control"] = "no-cache"  # reuse, but check for updates
        else:
            response.headers["Cache-Control"] = "no-store"
            if not path.startswith(NO_CSP_PATHS):
                response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return response

    # ---- errors
    def _error(status: int, message: str, headers: Optional[dict] = None):
        return JSONResponse({"detail": message}, status_code=status, headers=headers)

    def validation_error(request, exc):
        # FastAPI's default reply repeats the submitted values, which could include a password.
        errors = [{"loc": list(err.get("loc", ())), "msg": err.get("msg", "invalid")} for err in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(ac.AccessDenied, lambda r, e: _error(403, "not allowed"))
    app.add_exception_handler(dl.NotFoundError, lambda r, e: _error(404, str(e)))
    app.add_exception_handler(dl.DuplicateError, lambda r, e: _error(409, str(e)))
    app.add_exception_handler(dl.DataError, lambda r, e: _error(409, str(e)))
    app.add_exception_handler(ValueError, lambda r, e: _error(400, str(e)))
    app.add_exception_handler(sqlite3.IntegrityError, lambda r, e: _error(400, "invalid data"))
    app.add_exception_handler(auth.InvalidCredentials, lambda r, e: _error(401, str(e)))
    app.add_exception_handler(auth.EmailNotVerified, lambda r, e: _error(403, str(e)))
    app.add_exception_handler(auth.AccountSuspended, lambda r, e: _error(403, str(e)))
    app.add_exception_handler(auth.InvalidToken, lambda r, e: _error(400, str(e)))
    alerts = monitoring.AppAlerts(db_path, mailer, alert_email)

    def too_many(request: Request, e: auth.TooManyAttempts):
        # A rate limit refusing someone can mean password guessing: tell the admins (at most hourly).
        alerts.send(f"rate-limit:{request.url.path}", "a rate limit is refusing requests",
                    f"{e} (on {request.method} {request.url.path}, from {request.client.host if request.client else '?'}).\n"
                    "One person mistyping a password won't trigger this often; many of these mean someone is guessing.")
        return _error(429, str(e), {"Retry-After": str(e.retry_after)})

    app.add_exception_handler(auth.TooManyAttempts, too_many)

    def server_error(request: Request, e: Exception):
        route = request.scope.get("route")
        where = f"{request.method} {getattr(route, 'path', request.url.path)}"
        log.exception("server error on %s", where)
        alerts.send(f"error:{type(e).__name__}:{where}", f"server error ({type(e).__name__})",
                    f"A {type(e).__name__} happened on {where}. Someone saw \"something went wrong\".\n"
                    "See the full error with: journalctl -u mining-match --since '1 hour ago'")
        return _error(500, "something went wrong on our side; please try again")

    app.add_exception_handler(Exception, server_error)

    # ---- dependencies
    def get_conn():
        conn = dl.connect(db_path, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

    def bearer(request: Request, authorization: Annotated[Optional[str], Header()] = None) -> Optional[str]:
        """Session token from the Authorization header (apps), or the website's cookie.

        The cookie only counts when the request also carries the X-Requested-With header. Other
        sites can't add that header without the browser asking this server first (and it says
        no), so a page elsewhere can't act with a visitor's cookie (cross-site request forgery).
        """
        if authorization and authorization[:7].lower() == "bearer ":
            return authorization[7:].strip() or None
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie and request.headers.get(CSRF_HEADER) == CSRF_VALUE:
            return cookie
        return None

    def current(conn: Conn, token: Annotated[Optional[str], Depends(bearer)]) -> ac.Principal:
        user_id = auth.session_user(conn, token)
        if user_id is None:
            raise _NotLoggedIn()
        try:
            return ac.principal_for_user(conn, user_id)
        except ac.AccessDenied:
            raise _NotLoggedIn() from None

    class _NotLoggedIn(Exception):
        pass

    app.add_exception_handler(_NotLoggedIn, lambda r, e: _error(401, "log in first", {"WWW-Authenticate": "Bearer"}))
    Me = Annotated[ac.Principal, Depends(current)]

    def ip(request: Request) -> str:
        # Behind a proxy or load balancer, take this from its trusted forwarded header instead.
        return request.client.host if request.client else "unknown"

    IP = Annotated[str, Depends(ip)]

    def limit_searches(me: ac.Principal, conn) -> None:
        if me.role == "employer" and me.employer_id:  # admins aren't limited
            auth.limit_employer_search(conn, me.employer_id)
    CHECK_EMAIL = {"detail": "Check your email for the next step."}

    # ================================================================ auth
    @app.get("/health", tags=["system"])
    def health():
        return {"ok": True}

    @app.post("/auth/register/candidate", status_code=202, tags=["auth"])
    def register_candidate(body: RegisterCandidateIn, conn: Conn, client_ip: IP):
        profile = body.model_dump(exclude={"email", "password"}, exclude_none=True)
        auth.register(conn, mailer, base_url, role="candidate", email=body.email, password=body.password,
                      ip=client_ip, profile=profile)
        return CHECK_EMAIL

    @app.post("/auth/register/employer", status_code=202, tags=["auth"])
    def register_employer(body: RegisterEmployerIn, conn: Conn, client_ip: IP):
        profile = body.model_dump(exclude={"email", "password"}, exclude_none=True)
        auth.register(conn, mailer, base_url, role="employer", email=body.email, password=body.password,
                      ip=client_ip, profile=profile)
        return CHECK_EMAIL

    @app.post("/auth/verify-email", tags=["auth"])
    def verify_email(body: TokenIn, conn: Conn):
        auth.verify_email(conn, body.token)
        return {"detail": "Email confirmed. You can log in now."}

    @app.post("/auth/verify-email/resend", status_code=202, tags=["auth"])
    def resend_verification(body: EmailIn, conn: Conn, client_ip: IP):
        auth.resend_verification(conn, mailer, base_url, body.email, client_ip)
        return CHECK_EMAIL

    @app.post("/auth/login", tags=["auth"])
    def login(body: LoginIn, conn: Conn, client_ip: IP):
        session = auth.login(conn, body.email, body.password, client_ip)
        if not body.use_cookie:
            return session
        token = session.pop("token")
        response = JSONResponse(session)
        response.set_cookie(SESSION_COOKIE, token, max_age=auth.SESSION_MAX_SECONDS, path="/",
                            httponly=True, secure=secure_cookies, samesite="strict")
        return response

    @app.post("/auth/logout", status_code=204, tags=["auth"])
    def logout(me: Me, conn: Conn, token: Annotated[Optional[str], Depends(bearer)]):
        auth.logout(conn, token)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=secure_cookies, samesite="strict")
        return response

    @app.post("/auth/password-reset/request", status_code=202, tags=["auth"])
    def request_reset(body: EmailIn, conn: Conn, client_ip: IP):
        auth.request_password_reset(conn, mailer, base_url, body.email, client_ip)
        return CHECK_EMAIL

    @app.post("/auth/password-reset/confirm", tags=["auth"])
    def confirm_reset(body: ResetIn, conn: Conn):
        auth.reset_password(conn, body.token, body.new_password)
        return {"detail": "Password changed. Log in with your new password."}

    @app.post("/auth/change-password", tags=["auth"])
    def change_password(body: ChangePasswordIn, me: Me, conn: Conn,
                        token: Annotated[Optional[str], Depends(bearer)]):
        auth.change_password(conn, token, body.current_password, body.new_password)
        return {"detail": "Password changed. Other devices have been logged out."}

    @app.get("/me", tags=["auth"])
    def whoami(me: Me, conn: Conn):
        email = conn.execute("SELECT email FROM users WHERE id = ?", (me.user_id,)).fetchone()["email"]
        return {"user_id": me.user_id, "role": me.role, "email": email,
                "candidate_id": me.candidate_id, "employer_id": me.employer_id}

    # ========================================================== candidates
    @app.get("/candidates/{candidate_id}", tags=["candidates"])
    def view_candidate(candidate_id: Id, me: Me, conn: Conn):
        return ac.view_candidate(conn, me, candidate_id)

    @app.patch("/candidates/{candidate_id}", tags=["candidates"])
    def update_candidate(candidate_id: Id, body: CandidateUpdateIn, me: Me, conn: Conn):
        fields = body.model_dump(exclude_unset=True)
        for key in ("name", "willing_to_relocate", "willing_fifo", "is_active", "expiry_reminders"):
            if key in fields and fields[key] is None:
                raise ValueError(f"{key} can't be empty")
        ac.update_candidate(conn, me, candidate_id, **fields)
        return ac.view_candidate(conn, me, candidate_id)

    @app.delete("/candidates/{candidate_id}", status_code=204, tags=["candidates"])
    def delete_candidate(candidate_id: Id, me: Me, conn: Conn):
        for key in ac.delete_candidate(conn, me, candidate_id):
            storage.delete(key)
        return Response(status_code=204)

    @app.put("/candidates/{candidate_id}/licences", tags=["candidates"])
    def set_licence(candidate_id: Id, body: LicenceIn, me: Me, conn: Conn):
        ac.set_candidate_licence(conn, me, candidate_id, body.name, body.expiry, body.document_id)
        return {"ok": True}

    @app.delete("/candidates/{candidate_id}/licences", tags=["candidates"])
    def remove_licence(candidate_id: Id, name: Annotated[str, Query(max_length=120)], me: Me, conn: Conn):
        return {"removed": ac.remove_candidate_licence(conn, me, candidate_id, name)}

    @app.put("/candidates/{candidate_id}/qualifications", tags=["candidates"])
    def set_qualification(candidate_id: Id, body: QualificationIn, me: Me, conn: Conn):
        ac.set_candidate_qualification(conn, me, candidate_id, body.name, awarded_date=body.awarded_date,
                                       institution=body.institution, document_id=body.document_id)
        return {"ok": True}

    @app.delete("/candidates/{candidate_id}/qualifications", tags=["candidates"])
    def remove_qualification(candidate_id: Id, name: Annotated[str, Query(max_length=120)], me: Me, conn: Conn):
        return {"removed": ac.remove_candidate_qualification(conn, me, candidate_id, name)}

    @app.put("/candidates/{candidate_id}/skills", tags=["candidates"])
    def set_skill(candidate_id: Id, body: SkillIn, me: Me, conn: Conn):
        ac.set_candidate_skill(conn, me, candidate_id, body.name, body.years)
        return {"ok": True}

    @app.delete("/candidates/{candidate_id}/skills", tags=["candidates"])
    def remove_skill(candidate_id: Id, name: Annotated[str, Query(max_length=120)], me: Me, conn: Conn):
        return {"removed": ac.remove_candidate_skill(conn, me, candidate_id, name)}

    @app.post("/candidates/{candidate_id}/experience", status_code=201, tags=["candidates"])
    def add_experience(candidate_id: Id, body: ExperienceIn, me: Me, conn: Conn):
        exp_id = ac.add_experience(conn, me, candidate_id, body.role, body.start_date, body.end_date,
                                   employer_name=body.employer_name)
        return {"id": exp_id}

    @app.delete("/experience/{experience_id}", tags=["candidates"])
    def remove_experience(experience_id: Id, me: Me, conn: Conn):
        return {"removed": ac.remove_experience(conn, me, experience_id)}

    @app.get("/candidates/{candidate_id}/matching-jobs", tags=["candidates"])
    def matching_jobs(candidate_id: Id, me: Me, conn: Conn,
                      eligible_only: bool = False, limit: Annotated[Optional[int], Query(ge=1, le=500)] = None):
        return ac.rank_jobs_for_candidate(conn, me, candidate_id, eligible_only=eligible_only, limit=limit)

    # =========================================================== documents
    @app.post("/candidates/{candidate_id}/documents", status_code=201, tags=["documents"])
    def upload_document(candidate_id: Id, me: Me, conn: Conn,
                        kind: Annotated[str, Form(max_length=20)], file: Annotated[UploadFile, File()]):
        data = file.file.read(ac.MAX_UPLOAD_BYTES + 1)
        if len(data) > ac.MAX_UPLOAD_BYTES:
            return _error(413, f"file is larger than {ac.MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        mime = sniff_type(data)  # what the file really is, not what the browser says
        if mime is None or mime not in ac.ALLOWED_MIME_TYPES:
            raise ValueError("only PDF, Word, JPEG and PNG files are accepted")
        if scan_upload is not None and not scan_upload(data):
            raise ValueError("this file was rejected by the virus scan")
        doc = ac.add_document(conn, me, candidate_id, kind, file.filename or "upload",
                              mime_type=mime, size_bytes=len(data))
        try:
            storage.save(doc["storage_key"], data)
        except Exception:
            ac.remove_document(conn, me, doc["id"])
            raise
        return {"id": doc["id"], "mime_type": mime, "size_bytes": len(data)}

    @app.get("/candidates/{candidate_id}/documents", tags=["documents"])
    def list_documents(candidate_id: Id, me: Me, conn: Conn):
        return ac.list_documents(conn, me, candidate_id)

    @app.get("/documents/{document_id}/file", tags=["documents"])
    def download_document(document_id: Id, me: Me, conn: Conn):
        info = ac.get_document_file(conn, me, document_id)
        try:
            data = storage.read(info["storage_key"])
        except FileNotFoundError:
            raise dl.NotFoundError("file is missing from storage") from None
        return Response(content=data, media_type=info["mime_type"] or "application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{info["filename"]}"'})

    @app.delete("/documents/{document_id}", status_code=204, tags=["documents"])
    def remove_document(document_id: Id, me: Me, conn: Conn):
        storage.delete(ac.remove_document(conn, me, document_id))
        return Response(status_code=204)

    @app.post("/documents/{document_id}/verify", tags=["documents"])
    def verify_document(document_id: Id, me: Me, conn: Conn):
        ac.verify_document(conn, me, document_id)
        return {"ok": True}

    # ===================================================== contact + unlock
    @app.post("/candidates/{candidate_id}/contact-request", tags=["contact"])
    def request_contact(candidate_id: Id, body: ContactRequestIn, me: Me, conn: Conn):
        return {"status": ac.request_contact(conn, me, candidate_id, body.job_id)}

    @app.get("/contact-requests", tags=["contact"])
    def contact_requests(me: Me, conn: Conn):
        return ac.list_contact_requests(conn, me)

    @app.put("/contact-releases/{employer_id}", tags=["contact"])
    def set_release(employer_id: Id, body: ReleaseIn, me: Me, conn: Conn):
        return {"status": ac.set_contact_release(conn, me, employer_id, body.grant)}

    @app.post("/candidates/{candidate_id}/unlock", tags=["contact"])
    def unlock(candidate_id: Id, body: UnlockIn, me: Me, conn: Conn):
        return ac.unlock_candidate(conn, me, candidate_id, body.terms_version, body.job_id)

    @app.get("/fee-terms/current", tags=["fees"])
    def current_terms(me: Me, conn: Conn):
        return ac.get_fee_terms(conn, me)

    @app.get("/fee-terms/{version}", tags=["fees"])
    def terms_version(version: int, me: Me, conn: Conn):
        return ac.get_fee_terms(conn, me, version)

    @app.post("/fee-terms", status_code=201, tags=["fees"])
    def publish_terms(body: FeeTermsIn, me: Me, conn: Conn):
        return {"version": ac.publish_fee_terms(conn, me, **body.model_dump())}

    @app.post("/fee-terms/{version}/accept", tags=["fees"])
    def accept_terms(version: int, me: Me, conn: Conn):
        ac.accept_fee_terms(conn, me, version)
        return ac.get_fee_terms(conn, me, version)

    @app.get("/introductions", tags=["fees"])
    def introductions(me: Me, conn: Conn, employer_id: Optional[Id] = None):
        return ac.list_introductions(conn, me, employer_id=employer_id)

    # ================================================================ jobs
    @app.post("/jobs", status_code=201, tags=["jobs"])
    def create_job(body: JobCreateIn, me: Me, conn: Conn):
        job = body.model_dump(exclude={"status", "employer_id"}, exclude_none=True)
        return {"id": ac.create_job(conn, me, job, status=body.status, employer_id=body.employer_id)}

    @app.get("/jobs", tags=["jobs"])
    def my_jobs(me: Me, conn: Conn, status: Optional[str] = None, employer_id: Optional[Id] = None):
        return ac.list_my_jobs(conn, me, status=status, employer_id=employer_id)

    @app.get("/jobs/open", tags=["jobs"])
    def open_jobs(me: Me, conn: Conn):
        return ac.list_open_jobs(conn, me)

    @app.get("/jobs/{job_id}", tags=["jobs"])
    def get_job(job_id: Id, me: Me, conn: Conn):
        return ac.get_job(conn, me, job_id)

    @app.put("/jobs/{job_id}", tags=["jobs"])
    def update_job(job_id: Id, body: JobIn, me: Me, conn: Conn):
        ac.update_job(conn, me, job_id, body.model_dump(exclude_none=True))
        return ac.get_job(conn, me, job_id)

    @app.put("/jobs/{job_id}/status", tags=["jobs"])
    def job_status(job_id: Id, body: StatusIn, me: Me, conn: Conn):
        ac.set_job_status(conn, me, job_id, body.status)
        return {"ok": True}

    @app.get("/catalog/licence-rules", tags=["jobs"])
    def licence_rules(me: Me, conn: Conn):
        return reminders.licence_rules(conn, me)

    @app.post("/reminders/unsubscribe", tags=["candidates"])
    def unsubscribe(body: TokenIn, conn: Conn):
        reminders.unsubscribe(conn, body.token)
        return {"detail": "You won't get licence reminders any more."}

    @app.get("/catalog/{kind}", tags=["jobs"])
    def catalog(kind: str, me: Me, conn: Conn):
        return [item["name"] for item in ac.list_catalog(conn, me, kind)]

    @app.get("/jobs/{job_id}/ranked-candidates", tags=["jobs"])
    def ranked_candidates(job_id: Id, me: Me, conn: Conn,
                          min_score: Annotated[float, Query(ge=0, le=100)] = 0, eligible_only: bool = False,
                          limit: Annotated[Optional[int], Query(ge=1, le=1000)] = None):
        limit_searches(me, conn)
        return ac.rank_candidates_for_job(conn, me, job_id, min_score=min_score,
                                          eligible_only=eligible_only, limit=limit)

    @app.post("/search/candidates", tags=["jobs"])
    def search(body: SearchIn, me: Me, conn: Conn):
        limit_searches(me, conn)
        options: dict[str, Any] = {"min_score": body.min_score, "eligible_only": body.eligible_only,
                                   "limit": body.limit}
        if body.weights is not None:
            options["weights"] = body.weights
        return ac.search_candidates(conn, me, body.criteria.engine(), **options)

    # ========================================================== shortlists
    @app.post("/shortlists", status_code=201, tags=["shortlists"])
    def create_shortlist(body: ShortlistIn, me: Me, conn: Conn):
        return {"id": ac.create_shortlist(conn, me, body.name, body.job_id, employer_id=body.employer_id)}

    @app.get("/shortlists", tags=["shortlists"])
    def shortlists(me: Me, conn: Conn, employer_id: Optional[Id] = None):
        return ac.list_shortlists(conn, me, employer_id=employer_id)

    @app.get("/shortlists/{shortlist_id}", tags=["shortlists"])
    def get_shortlist(shortlist_id: Id, me: Me, conn: Conn):
        return ac.get_shortlist(conn, me, shortlist_id)

    @app.post("/shortlists/{shortlist_id}/entries", tags=["shortlists"])
    def add_entry(shortlist_id: Id, body: EntryIn, me: Me, conn: Conn):
        ac.add_to_shortlist(conn, me, shortlist_id, body.candidate_id, notes=body.notes)
        return ac.get_shortlist(conn, me, shortlist_id)

    @app.put("/shortlists/{shortlist_id}/entries/{candidate_id}/status", tags=["shortlists"])
    def entry_status(shortlist_id: Id, candidate_id: Id, body: StatusIn, me: Me, conn: Conn):
        ac.set_shortlist_status(conn, me, shortlist_id, candidate_id, body.status)
        return {"ok": True}

    @app.delete("/shortlists/{shortlist_id}/entries/{candidate_id}", tags=["shortlists"])
    def remove_entry(shortlist_id: Id, candidate_id: Id, me: Me, conn: Conn):
        return {"removed": ac.remove_from_shortlist(conn, me, shortlist_id, candidate_id)}

    # ============================================================= billing
    @app.get("/billing/settings", tags=["billing"])
    def billing_settings(me: Me, conn: Conn):
        return billing.billing_settings(conn, billing_config, me)

    @app.get("/billing/details", tags=["billing"])
    def billing_details(me: Me, conn: Conn, employer_id: Optional[Id] = None):
        return billing.get_billing_details(conn, me, employer_id=employer_id)

    @app.put("/billing/details", tags=["billing"])
    def update_billing_details(body: BillingDetailsIn, me: Me, conn: Conn):
        return billing.update_billing_details(conn, me, abn=body.abn, billing_email=body.billing_email,
                                              billing_address=body.billing_address, employer_id=body.employer_id)

    @app.post("/hires/preview", tags=["billing"])
    def preview_hire(body: HireIn, me: Me, conn: Conn):
        return billing.preview_hire(conn, billing_config, me, body.candidate_id, body.start_date, body.base_salary,
                                    employer_id=body.employer_id)

    @app.post("/hires", status_code=201, tags=["billing"])
    def record_hire(body: HireIn, me: Me, conn: Conn):
        return billing.record_hire(conn, billing_config, mailer, base_url, me, body.candidate_id, body.start_date,
                                   body.base_salary, job_id=body.job_id, employer_id=body.employer_id)

    @app.get("/hires", tags=["billing"])
    def hires(me: Me, conn: Conn, employer_id: Optional[Id] = None):
        return billing.list_placements(conn, me, employer_id=employer_id)

    @app.post("/hires/{placement_id}/cancel", tags=["billing"])
    def cancel_hire(placement_id: Id, body: ReasonIn, me: Me, conn: Conn):
        return billing.cancel_hire(conn, billing_config, me, placement_id, body.reason)

    @app.get("/invoices", tags=["billing"])
    def invoices(me: Me, conn: Conn, status: Optional[str] = None, employer_id: Optional[Id] = None):
        return billing.list_invoices(conn, billing_config, me, status=status, employer_id=employer_id)

    @app.post("/invoices", status_code=201, tags=["billing"])
    def create_invoice(body: ServicesInvoiceIn, me: Me, conn: Conn):
        lines = [line.model_dump() for line in body.lines]
        return billing.create_services_invoice(conn, billing_config, mailer, base_url, me, body.employer_id, lines,
                                               due_days=body.due_days)

    @app.post("/invoices/issue-due", tags=["billing"])
    def issue_due(me: Me, conn: Conn):
        return {"issued": billing.issue_due_invoices(conn, billing_config, mailer, base_url, me)}

    @app.get("/invoices/{invoice_id}", tags=["billing"])
    def invoice(invoice_id: Id, me: Me, conn: Conn):
        return billing.get_invoice(conn, billing_config, me, invoice_id)

    @app.get("/invoices/{invoice_id}/pdf", tags=["billing"])
    def invoice_pdf(invoice_id: Id, me: Me, conn: Conn):
        filename, data = billing.invoice_pdf(conn, billing_config, me, invoice_id)
        return Response(content=data, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.post("/invoices/{invoice_id}/paid", tags=["billing"])
    def mark_paid(invoice_id: Id, body: PaidIn, me: Me, conn: Conn):
        return billing.mark_paid(conn, billing_config, me, invoice_id, body.paid_on, body.note)

    @app.post("/invoices/{invoice_id}/void", tags=["billing"])
    def void_invoice(invoice_id: Id, body: ReasonIn, me: Me, conn: Conn):
        return billing.void_invoice(conn, billing_config, me, invoice_id, body.reason)

    @app.post("/invoices/{invoice_id}/remind", tags=["billing"])
    def remind(invoice_id: Id, me: Me, conn: Conn):
        return billing.send_reminder(conn, billing_config, mailer, base_url, me, invoice_id)

    # =============================================================== admin
    @app.put("/admin/users/{user_id}/active", tags=["admin"])
    def set_active(user_id: Id, body: UserActiveIn, me: Me, conn: Conn):
        ac.set_user_active(conn, me, user_id, body.active, body.reason)
        return {"ok": True}

    @app.post("/admin/admins", status_code=201, tags=["admin"])
    def create_admin(body: EmailIn, me: Me, conn: Conn):
        user_id = ac.create_admin(conn, auth._normalise_email(body.email), by=me)
        auth.send_admin_invite(conn, mailer, base_url, user_id)
        return {"user_id": user_id}

    @app.get("/admin/users", tags=["admin"])
    def users(me: Me, conn: Conn, search: Annotated[Optional[str], Query(max_length=120)] = None,
              role: Optional[str] = None, limit: Annotated[int, Query(ge=1, le=500)] = 100):
        return ac.list_users(conn, me, search=search, role=role, limit=limit)

    @app.get("/admin/documents", tags=["admin"])
    def documents_to_verify(me: Me, conn: Conn, limit: Annotated[int, Query(ge=1, le=500)] = 100):
        return ac.list_documents_to_verify(conn, me, limit=limit)

    @app.post("/admin/reminders/run", tags=["admin"])
    def run_reminders(me: Me, conn: Conn):
        return reminders.send_expiry_reminders(conn, mailer, base_url, me)

    @app.get("/admin/reminders", tags=["admin"])
    def recent_reminders(me: Me, conn: Conn, limit: Annotated[int, Query(ge=1, le=500)] = 100):
        return reminders.list_recent_reminders(conn, me, limit=limit)

    @app.get("/admin/audit-log", tags=["admin"])
    def audit_log(me: Me, conn: Conn, user_id: Optional[Id] = None, resource_type: Optional[str] = None,
                  resource_id: Optional[Id] = None, outcome: Optional[str] = None,
                  limit: Annotated[int, Query(ge=1, le=1000)] = 100):
        return ac.read_audit_log(conn, me, user_id=user_id, resource_type=resource_type,
                                 resource_id=resource_id, outcome=outcome, limit=limit)

    # ============================================================= website
    if web_dir is not None and (Path(web_dir) / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=web_dir), name="assets")

        @app.get("/", include_in_schema=False)
        def website():
            return FileResponse(Path(web_dir) / "index.html", media_type="text/html")

    return app


def app_from_environment() -> FastAPI:
    """Configuration from environment variables (see README)."""
    mailer = auth.mailer_from_env()
    origins = tuple(o.strip() for o in os.environ.get("MM_CORS_ORIGINS", "").split(",") if o.strip())
    return create_app(
        os.environ.get("MM_DB", "mining_match.db"),
        storage=LocalStorage(os.environ.get("MM_FILES", "uploads")),
        mailer=mailer,
        base_url=os.environ.get("MM_BASE_URL", "http://localhost:3000"),
        cors_origins=origins,
        enable_docs=os.environ.get("MM_DOCS", "1") == "1",
        secure_cookies=os.environ.get("MM_SECURE_COOKIES", "1") == "1",
        billing_config=billing.BillingConfig.from_env(),
        alert_email=os.environ.get("MM_ALERT_EMAIL", "").strip() or None,
    )


if os.environ.get("MM_DB"):
    app = app_from_environment()
