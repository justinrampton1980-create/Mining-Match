# Mining-Match
Mining candidate matching and employer shortlisting platform

**Start here:** [STATUS.md](STATUS.md) says what's built and tested and what isn't. [docs/INSTALL.md](docs/INSTALL.md) sets it up, [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) covers hosting (including Bluehost), [SECURITY.md](SECURITY.md) and [docs/PRIVACY_AND_CONTRACTS.md](docs/PRIVACY_AND_CONTRACTS.md) cover safeguards, and [docs/REVIEW_GUIDE.md](docs/REVIEW_GUIDE.md) is for reviewers.

## What's in this repository

The Mining Match recruitment platform: matching engine, data layer, access control, HTTP API, web app and invoicing.

The matching engine is a pair of Python and JavaScript modules that score and rank candidates against mining jobs. Both read the same plain-dict/JSON shapes and are tested against one shared fixture file, so they produce identical results.

```
mining-match/
  python/matching_engine.py        python/test_matching_engine.py
  js/matchingEngine.js             js/matchingEngine.test.js
  js/format.test.mjs               (date maths used by the web app)
  fixtures/match_cases.json        (shared expected scores)
  db/schema.sql                    (database schema)
  db/migrations/                   (upgrades for existing databases)
  python/data_layer.py             python/test_data_layer.py
  python/access_control.py         python/test_access_control.py
  python/auth.py                   (sign-up, login, sessions, password reset)
  python/storage.py                (uploaded files, file-type checks)
  python/api.py                    python/test_api.py
  python/billing.py                python/test_billing.py   (placement fees, invoices)
  python/reminders.py              python/test_reminders.py (licence expiry reminders)
  python/daily.py                  (the daily job: reminders, then invoices)
  python/test_web.py               (browser tests of the web app)
  python/requirements.txt
  db/catalog_seed.json             (common licence, qualification and skill names)
  web/                             (the web app: index.html, app.css, js/, fonts/)
  deploy/                          (server settings template, systemd, nginx, cron, backups)
  docs/                            (install, deployment, privacy and contracts, review guide)
  STATUS.md  SECURITY.md
```

Run tests: `cd python && pip install -r requirements.txt && python -m unittest -v` and `cd js && node --test` (Node 18+). The engine, data layer and access control need only the standard library; the API needs the packages in `requirements.txt`. The browser tests run only if Playwright and Chrome are installed (see "Web app").

## Scoring rules

| Component | Weight | How it's scored |
|---|---|---|
| licences | 0.30 | Held and valid on the job **start date**. Expiring within 90 days gives a warning but still counts. |
| qualifications | 0.20 | Held or not. |
| skills | 0.25 | `min(1, years / min_years)`. A skill listed with no years gets full credit for presence. |
| experience | 0.20 | `min(1, relevant_years / min_experience_years)`. Only roles in `relevant_roles` count (all roles if none given). |
| location | 0.05 | 1.0 same location, 0.7 if willing to relocate or FIFO, else 0. |

- Required items count 1.0, preferred items 0.5.
- A component with no requirement on the job is skipped and the other weights are renormalised.
- Overall score is 0 to 100, rounded to 1 decimal.
- **Eligible** means no required licence is missing or expired at start, and no required qualification is missing. The engine ranks eligible first, then by score, then by name, then by candidate id. Employers never see that name order: their results are re-sorted so ties go by candidate id (see "No name leaks through ordering" under Access control).

## Result shape

```json
{
  "candidate_id": "cand-b", "job_id": "job-1",
  "score": 51.8, "eligible": false,
  "breakdown": {"licences": 40.0, "qualifications": 100.0, "skills": 33.3, "experience": 40.0, "location": 70.0},
  "blockers": ["Coal Board Medical: expired 2026-10-31"],
  "gaps": ["Dragline operation: 1.5/3 yrs"],
  "warnings": ["relocation or FIFO required"]
}
```

`blockers`, `gaps` and `warnings` are display-ready strings for employer dashboards.

## Input shapes (snake_case in both languages)

- **candidate**: `id, name, location, willing_to_relocate, willing_fifo, licences [str | {type, expiry}], qualifications [str | {name}], skills [str | {name, years}], experience [{role, years}]`
- **job**: `id, title, start_date, location, required_licences, preferred_licences, required_qualifications, preferred_qualifications, required_skills [str | {name, min_years}], preferred_skills, min_experience_years, relevant_roles`
- Dates are `YYYY-MM-DD`.

## Engine functions

| Python | JavaScript |
|---|---|
| `score_candidate(candidate, job, weights=None, as_of=None)` | `scoreCandidate(candidate, job, { weights, asOf })` |
| `rank_candidates(candidates, job, ..., min_score, limit, eligible_only)` | `rankCandidates(candidates, job, { weights, asOf, minScore, limit, eligibleOnly })` |
| `match_jobs_for_candidate(candidate, jobs, ...)` | `matchJobsForCandidate(candidate, jobs, { ... })` |
| `register_aliases({...})` | `registerAliases({...})` |

Licence and qualification names are matched case-insensitively with aliases (e.g. `SS11` is `Standard 11`). Add more with `register_aliases`.

## Data layer

`db/schema.sql` + `python/data_layer.py` (SQLite, standard library only). Tests load the shared fixtures into a database, run them through the engine and check the scores come out exactly as the engine suites expect.

**Tables**

| Group | Tables |
|---|---|
| Accounts | `users` (email, role, password_hash), `candidates`, `employers` |
| Vocabulary | `catalog_items` (every licence, qualification and skill once), `catalog_aliases` |
| Candidate profile | `candidate_licences` (expiry), `candidate_qualifications`, `candidate_skills` (years), `candidate_experience` (dates), `documents` |
| Jobs | `jobs`, `job_requirements` (required or preferred, min years for skills), `job_relevant_roles` |
| Shortlists | `shortlists`, `shortlist_entries` (status, notes, score snapshot) |

**Design notes**

- Licences, qualifications and skills live in one shared catalog. Candidates and jobs link to catalog items, so spelling variants collapse into one item (`add_alias(conn, "SS11", "Standard 11")`). Call `load_aliases(conn)` at start-up so the engine knows the stored aliases. Composite foreign keys stop a licence being filed as a skill.
- Experience years are derived from start/end dates (no end date means still in the role), so they stay correct as time passes.
- Uploaded files are not stored in the database, only their `storage_key`. `delete_candidate` removes a candidate and everything attached, and returns the storage keys so the files can be deleted too.
- Ids are uuid text, dates are ISO text, booleans are 0/1. These work on SQLite and port to PostgreSQL (see below).

**Main functions** (all take the connection first and commit their own work)

| Area | Functions |
|---|---|
| Setup | `connect`, `init_schema`, `load_aliases`, `add_alias` |
| Accounts | `register_candidate`, `register_employer`, `update_candidate`, `delete_candidate` |
| Candidate profile | `set_candidate_licence` (renewals update in place), `set_candidate_qualification`, `set_candidate_skill`, `add_experience`, `add_document`, `verify_document`, `remove_*` |
| Jobs | `create_job`, `update_job`, `set_job_status`, `get_job`, `load_jobs` (take and return engine-shaped dicts) |
| Matching | `load_candidates`, `rank_candidates_for_job`, `rank_jobs_for_candidate`, `search_candidates` (ad-hoc criteria for employer search) |
| Shortlists | `create_shortlist`, `add_to_shortlist` (stores the engine score if the shortlist is linked to a job), `set_shortlist_status`, `remove_from_shortlist`, `get_shortlist`, `list_shortlists` |

```python
import data_layer as dl
conn = dl.connect("mining_match.db"); dl.init_schema(conn); dl.load_aliases(conn)
ranked = dl.rank_candidates_for_job(conn, job_id, eligible_only=True, limit=20)
```

**Not in this layer**

- Access control (see the next section).
- Password hashing and login (the layer only stores `password_hash`).
- Placement fees and employer services billing.
- A JavaScript data-access port.

**Moving to PostgreSQL for production.** SQLite allows one writer at a time, which is fine for development and tests but not for many employers and candidates at once. The schema ports with small changes: `DATE`, `BOOLEAN`, `TIMESTAMPTZ`, and `%s` placeholders instead of `?`. The data layer uses plain SQL through the Python DB-API, so the port is mostly a thin connection adapter.

## Access control

`python/access_control.py` is the layer the API calls. Each function takes the connection and the caller's `Principal`, checks the rules, does the work through the data layer and writes an audit row. Nothing a user can reach should call data-layer write functions directly.

```python
import data_layer as dl, access_control as ac
conn = dl.connect("mining_match.db"); dl.init_schema(conn); dl.load_aliases(conn)
me = ac.principal_for_user(conn, user_id)          # after your login check succeeds
ranked = ac.rank_candidates_for_job(conn, me, job_id, eligible_only=True)
```

**Who can do what**

| | Candidate | Employer | Admin |
|---|---|---|---|
| Profile, licences, qualifications, skills, experience, documents | own | no | any |
| See a candidate | own (full) | anonymised; full after the two-step unlock | full |
| Download a candidate's files | own | only after the two-step unlock | any |
| Verify documents | no | no | yes |
| Hide or delete a profile | own | no | any |
| Jobs | see open jobs | own jobs (incl. drafts) | any |
| Search and rank candidates | no | own jobs or ad-hoc search; anonymised; max 100 results | all |
| Jobs ranked for a candidate | own | no | any |
| Shortlists | no | own | any |
| Contact details | approve or refuse employers | request, accept fee terms, unlock | cannot consent or unlock |
| Fee terms | no | view and accept | publish new versions |
| Suspend accounts, read audit log, create admins | no | no | yes |

**How it works**

- **Anonymised until unlocked.** Employers see a candidate's location, availability, licences, qualifications, skills and experience, under a label like `Candidate 3FA9C1D2`. Name, email, phone, summary and documents appear only after the two-step unlock (next point). The visible fields are an allow-list (`ANONYMOUS_FIELDS`), so anything added to profiles later stays hidden by default.
- **Two-step unlock.** Details are released only when both keys have turned:
  1. The candidate approves the employer (`set_contact_release`, after a `request_contact` or unprompted).
  2. The employer accepts the current placement fee terms (`accept_fee_terms`) and unlocks that candidate (`unlock_candidate`), sending back the terms version they were shown.

  The first unlock is recorded as an **introduction** (candidate, employer, job, terms version, who, when): the event that starts the fee window. `list_introductions` shows them for fee tracking. Each employer sees a `contact_status` per candidate: `none`, `requested`, `declined`, `approved` or `unlocked`. A candidate can withdraw approval at any time, which hides their details again (the introduction record stays). Once refused, repeat requests change nothing.
- **Fee terms.** An admin publishes versions with `publish_fee_terms` (fee %, minimum fee, what salary it applies to, window in months, guarantee days, full wording). Published versions can't be edited or deleted; the database refuses. When a new version is published, employers must accept it before unlocking anyone else, and earlier introductions keep the terms they were made under. The wording itself should come from a lawyer.
- **No name leaks through ordering.** For employers, tied scores are ordered by random id, never by name. Ordering by name would let an employer copy a candidate's visible profile into test accounts with chosen names and work out the hidden name from the order.
- **Accounts are re-checked on every call.** A suspended account (`set_user_active`) loses access immediately, and its profile and jobs disappear from searches. A `Principal` that doesn't match the database (wrong role or profile) is refused.
- **Uploads.** `add_document` generates the storage key itself and returns it for the upload code to use, so a client can never point at another file. Filenames are cleaned, and only PDF, Word, JPEG and PNG up to 10 MB are accepted. The declared type is not proof, so also check the file content when saving it.
- **Audit log.** Every refused action and every change, file download, admin read and employer view of a candidate is logged with ids and short reasons, never names or contact details. Entries survive account deletion.
- **Unconfirmed emails.** Accounts that haven't confirmed their email can't log in and are left out of searches, so nobody can fill the platform with fake profiles or sign someone else up.
- **First admin.** Create the first admin from a command line (see "Running the API"). This only works while no admin exists. After that, admins invite admins through the API.

**Upgrading an existing database:** run the files in `db/migrations/` in order, once each (`001_access_control.sql`, `002_auth_and_fees.sql`, `003_billing.sql`, then `004_licence_reminders.sql`). New databases get everything from `schema.sql`. Migration 002 marks existing accounts as confirmed so nobody is locked out.

## HTTP API

`python/api.py` (FastAPI). Interactive documentation of every endpoint, with request and response shapes, is at `/docs` while the server runs. It's the reference for whoever builds the website or app.

**Running the API**

```bash
cd python
pip install -r requirements.txt
# Create the first admin (only works once, while no admin exists):
python -c "import data_layer as dl, access_control as ac, auth; c = dl.connect('mining_match.db'); dl.init_schema(c); ac.create_admin(c, 'you@example.com', auth.hash_password(input('Password: ')))"
MM_DB=mining_match.db MM_FILES=uploads MM_BASE_URL=http://localhost:8000 MM_SECURE_COOKIES=0 uvicorn api:app --reload
```

Then open http://localhost:8000 for the web app. `MM_SECURE_COOKIES=0` is only for trying it on your own computer without HTTPS; leave it out in production.

Without `MM_SMTP_HOST`, emails are printed in the console instead of being sent.

| Setting | Meaning |
|---|---|
| `MM_DB` | database file |
| `MM_FILES` | folder for uploaded files |
| `MM_BASE_URL` | website address used in emailed links (`/verify-email?token=…`, `/reset-password?token=…`) |
| `MM_SMTP_HOST`, `MM_SMTP_PORT`, `MM_SMTP_USER`, `MM_SMTP_PASSWORD`, `MM_MAIL_FROM` | email sending (STARTTLS) |
| `MM_CORS_ORIGINS` | comma-separated website addresses allowed to call the API from a browser |
| `MM_DOCS` | `0` hides `/docs` in production |
| `MM_SECURE_COOKIES` | `1` (default) sends the website's login cookie over HTTPS only. `0` only for local testing over plain http |

**Endpoints**

| Area | Endpoints |
|---|---|
| Sign-up and login | `POST /auth/register/candidate`, `/auth/register/employer`, `/auth/verify-email`, `/auth/verify-email/resend`, `/auth/login`, `/auth/logout`, `/auth/password-reset/request`, `/auth/password-reset/confirm`, `/auth/change-password`; `GET /me` |
| Candidate profile | `GET/PATCH/DELETE /candidates/{id}`; `PUT/DELETE /candidates/{id}/licences`, `/qualifications`, `/skills`; `POST /candidates/{id}/experience`; `DELETE /experience/{id}`; `GET /candidates/{id}/matching-jobs` |
| Documents | `POST/GET /candidates/{id}/documents`; `GET /documents/{id}/file`; `DELETE /documents/{id}`; `POST /documents/{id}/verify` |
| Contact and unlock | `POST /candidates/{id}/contact-request`; `GET /contact-requests`; `PUT /contact-releases/{employer_id}`; `POST /candidates/{id}/unlock` |
| Fees | `GET /fee-terms/current`, `/fee-terms/{version}`; `POST /fee-terms`, `/fee-terms/{version}/accept`; `GET /introductions` |
| Jobs and search | `POST/GET /jobs`; `GET /jobs/open`; `GET/PUT /jobs/{id}`; `PUT /jobs/{id}/status`; `GET /jobs/{id}/ranked-candidates`; `POST /search/candidates` |
| Shortlists | `POST/GET /shortlists`; `GET /shortlists/{id}`; `POST /shortlists/{id}/entries`; `PUT /shortlists/{id}/entries/{candidate_id}/status`; `DELETE /shortlists/{id}/entries/{candidate_id}` |
| Admin | `PUT /admin/users/{id}/active`; `POST /admin/admins`; `GET /admin/audit-log` |

Clients send `Authorization: Bearer <token>` from `/auth/login`. Errors: 400 bad input, 401 not logged in, 403 not allowed, 404 not found, 409 conflict (e.g. fee terms changed), 413 file too large, 422 malformed request, 429 too many attempts (with `Retry-After`).

**Login security**

- Passwords: at least 10 characters, not common, not containing the email; hashed with scrypt and a random salt.
- Sessions: random tokens, stored only as hashes. They end after 7 days unused or 30 days in total, on logout, on password reset (all devices), on password change (other devices) and on suspension.
- Email links (confirmation 48 hours, reset 1 hour) work once and are stored only as hashes.
- Sign-up, resend and password reset give the same answer whether or not the email is registered, and login gives the same error for a wrong password and an unknown email. Nobody can use them to find out who is on the platform.
- Failed logins: 5 for one account from one IP address, or 30 from one IP address, in 15 minutes, then a wait; also 100 for one account in an hour across all addresses. Someone else's wrong guesses don't lock the owner out. Emails sent are limited per address too.
- Validation errors never repeat what was submitted, so passwords can't leak into logs or screens.

**Uploads.** The file type comes from the file's content, not its name or what the browser claims. Only PDF, Word (.doc/.docx), JPEG and PNG up to 10 MB are accepted. A virus-scan hook (`scan_upload`) runs before anything is stored; connect it to ClamAV or a cloud scanner in production. Files are always downloaded as attachments, never shown inside the browser.

**Before going live**

- Run behind HTTPS (e.g. a load balancer or nginx). Cap request size there too (`client_max_body_size 11m`), and pass the real client IP. Then change `ip()` in `api.py` to read the proxy's forwarded header, or rate limits will count every user as one address.
- Move to PostgreSQL, and put uploads in Australian-region object storage (same four methods as `LocalStorage`).
- Connect the virus-scan hook and a real email service, and set `MM_DOCS=0`.
- Keep `MM_SECURE_COOKIES` at its default (`1`) and set `MM_BASE_URL` to the real https address.
- Get an independent security test before real candidate data goes in.

## Web app

`web/` is the website, served by the API itself at `/`: one server to run, nothing to compile or install. It's built for phones first (navigation at the bottom, in reach of a thumb) and switches to a top bar on wider screens.

| Who | Screens |
|---|---|
| Everyone | Log in, sign up (looking for work or hiring), confirm email, forgot and reset password, account (change password, log out) |
| Candidates | **Profile**: licences shown as tickets with expiry status, qualifications, skills, work history, documents, details and visibility. **Jobs**: open jobs ranked for them, with what's missing. **Requests**: approve, decline or withdraw employers. Delete account |
| Employers | **Jobs**: post and edit jobs, set status, ranked candidates, one-tap shortlist. **Candidate profile**: anonymised until unlocked, with the contact request, fee terms and unlock flow. **Search** without a job. **Shortlists** with status and notes. **Fees**: current terms and everyone unlocked |
| Admins | Publish placement terms, find and suspend or reactivate accounts, check uploaded documents, activity log, invite admins |

**Look.** Mine-site signage: slate and concrete grey, with safety orange used only for actions. Type is Barlow, which is modelled on road signage and served from `web/fonts` (licence in `OFL-Barlow.txt`). The one bold element is the licence ticket: a hi-vis stripe and a large expiry date that turns amber within 90 days and is struck through once expired. Licence, qualification and skill boxes suggest common names from `db/catalog_seed.json`, so candidates and jobs use the same wording and match. Location is chosen by state for the same reason.

**Security in the browser**

- The login session is an HttpOnly, SameSite=Strict cookie. Page scripts can't read it, so a script injected into a page couldn't steal it.
- The cookie only counts on requests carrying an `X-Requested-With: mining-match` header, which other websites can't add. This blocks forged requests from other sites.
- A strict Content Security Policy lets the page run only its own script files: no inline scripts, nothing from other sites, and the page can't be framed.
- Screens are built without ever inserting HTML, so anything a user types (names, notes, file names) shows as plain text. The tests check this with a booby-trapped qualification name.
- Emailed links put their token after `#` (`/#/reset-password?token=…`). Browsers never send that part to a server, and the app removes it from the address bar once used.

**Browser tests.** `test_web.py` drives the whole hiring flow in real Chrome:

1. Admin publishes terms.
2. A candidate signs up on a phone-sized screen, confirms their email, adds tickets, a qualification, a skill, work history and a document.
3. An employer posts a job, sees the anonymised ranking, shortlists and requests contact.
4. The candidate approves; the employer accepts the terms, unlocks and downloads the document.
5. Admin checks the document and suspends the employer, who is logged out on their next click.

It also covers a wrong password and a full password reset, and fails on any script error or blocked content. Run it with `pip install playwright`, plus `playwright install chrome` if Chrome isn't installed. Set `MM_SHOTS=folder` to save phone and desktop screenshots.

**Not built yet:** licence expiry reminder emails, checking licences against government registers, and native app-store apps. The API already supports apps through bearer tokens.

## Invoicing (bank transfer)

`python/billing.py`. Placement fees are invoiced and paid by bank transfer: no card fees, no payment processor.

**How it works**

1. **Record a hire.** After unlocking a candidate, the employer records the hire (start date and first-year base salary) from Billing or the candidate's page. They see the fee, GST, invoice date and due date before confirming.
2. **Fee.** The fee follows the fee terms version the employer accepted when they unlocked that candidate: the percentage of salary, at least the minimum fee. No fee is due if the start date is after the fee window (window months from the unlock). The start date can't be before the unlock date.
3. **Issuing.** The invoice is issued on the start date, or straight away if that date has passed, never backdated. It's due after the payment terms (14 days by default). Each issued invoice gets the next number (`MM-00001`, `MM-00002`, ...) and is emailed with a PDF to the employer's billing email, or their account email if none is set.
4. **Payment.** The employer pays by bank transfer using the invoice number as the reference. When the money arrives, an admin marks the invoice paid (Admin > Invoices). Overdue invoices show in red and can get a reminder email (at most one a day).
5. **Corrections.** Issued invoices can't be edited or deleted: the database refuses. To correct one, void it (it keeps its number and record) and issue a new one. Employers can cancel a hire until its invoice is issued; after that only an admin can.
6. **Other services.** Admins can issue invoices for other services, such as a subscription, with up to 10 lines.

**Tax invoice rules followed** (from the ATO's tax invoice page):

- The invoice says it's a tax invoice and shows the seller's name and ABN, the issue date, a description with the price, the GST amount, and that the whole sale is taxable.
- For $1,000 or more it also shows the buyer's identity (company name, and ABN if they've entered one).
- GST is 10%, worked out on the total of the lines and rounded to the nearest cent, half a cent up.
- If you're not registered for GST, invoices are titled "Invoice", charge no GST and say so.

**Protection against invoice fraud.** Bank details come only from the server settings below, never from anything that can be edited in the website. Someone who gets into an admin or employer account can't change where employers pay. Every invoice and email tells employers that your bank details never change by email.

**Settings** (environment variables; invoicing is off until `MM_ABN` is set)

| Setting | Meaning |
|---|---|
| `MM_BUSINESS_NAME` | Legal name on invoices |
| `MM_ABN` | Your ABN (check digits are verified) |
| `MM_BUSINESS_ADDRESS`, `MM_BILLING_EMAIL`, `MM_BUSINESS_PHONE` | Shown on invoices; phone is optional |
| `MM_GST_REGISTERED` | `1` (default) adds 10% GST; `0` for not registered |
| `MM_BANK_ACCOUNT_NAME`, `MM_BANK_BSB`, `MM_BANK_ACCOUNT` | Where employers pay |
| `MM_PAYID` | Optional PayID shown as another way to pay |
| `MM_PAYMENT_TERMS_DAYS` | Days to pay (default 14) |
| `MM_INVOICE_PREFIX` | Invoice number prefix (default `MM`) |
| `MM_TIMEZONE` | For invoice dates and reminder days (default `Australia/Brisbane`) |

**Daily job.** Scheduled invoices are issued by the daily job, `python -m daily` (see "Daily job" below). Admins can also issue them from Admin > Invoices.

**Check with your accountant:**

- **Partial payments** aren't supported. An invoice is marked paid in full; use the note for anything unusual.
- **Adjustment notes:** voiding an invoice after you've reported its GST may need an adjustment note, which isn't built.
- **Matching payments:** payments are matched to invoices by hand from your bank statement, using the reference.
- **Subscriptions** aren't recurring automatically. Issue them as services invoices.
- **GST registration:** whether and when to register. Your board notes estimate about six placements passes the $75,000 threshold.

## Licence expiry reminders

`python/reminders.py`. Candidates get an email before a licence on their profile expires, so they renew in time and stay matchable.

- **When:** 90, 60 and 30 days before the expiry date, and once just after it expires (within 14 days). One email per person per day lists every licence that reached a new reminder.
- **Never twice:** each reminder sent is logged against the licence's expiry date. Running the job twice in a day sends nothing new. Renewing a licence, or correcting its date, starts its reminders afresh.
- **Catching up:** if the job missed days, or someone adds a licence with 45 days left, they get only the latest reminder reached (the 60-day one), not every one since.
- **Who:** active accounts with a confirmed email that haven't turned reminders off. Hiding a profile from employers doesn't stop reminders; suspending the account does.
- **Turning them off:** a checkbox under Your details in the profile, or the link in every email (no login needed). Each candidate's link carries their own random key, and it can only turn reminders off.
- **Failed emails** aren't logged, so they're tried again on the next run, and the job exits with status 1 so cron can alert you.
- **Admin > Reminders** shows what was sent, has a "Send today's reminders now" button for when the daily job hasn't run, and lists the renewal cycles.

**Suggested expiry dates.** `db/catalog_seed.json` records the usual renewal cycle and a short note for common licences (Standard 11, S123, G2 and Coal Board Medical every 5 years; high risk work licences 5 years; first aid 3 years; notes only for licences with no set cycle, such as the White Card). When a candidate adds one of these, the form shows the note and, for a fixed cycle, works out the expiry date from the date they got or last renewed it. The person is told to check it against their card, and a date they type themselves always wins. These cycles are general guidance: confirm them with the regulators before launch, and edit the seed file (it's reloaded at startup) as rules change.

## Daily job

`python -m daily` runs, in order: licence expiry reminders, then scheduled invoices whose date has come (if invoicing is set up). Run it once a day with the same settings as the server (`MM_DB`, `MM_BASE_URL`, mail and billing settings), for example from cron at 6:50am Brisbane time:

```
50 6 * * *  cd /app/python && python -m daily
```

Both parts are safe to run more than once a day. `python -m billing issue-due` still works on its own.
