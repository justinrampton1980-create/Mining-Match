# Mining Match: development status

Last updated 20 November 2026. This file describes only what exists in this repository. "Built and tested" means the code is here and automated tests cover it. Nothing has been deployed to a live server yet, and no real candidate data has been used.

## Summary

The platform runs end to end on one computer: candidates and employers can register, build profiles and post jobs. Candidates are matched and scored automatically. Employers can shortlist candidates, unlock contact details through a two-step consent and fee-terms process, and get invoiced for placements. It has not been deployed, has not had an independent security test, and still uses SQLite rather than PostgreSQL.

Test results at this commit: **190 Python tests** (including 2 full browser tests in Chrome and 17 that run the backup script for real) and **20 JavaScript tests**, all passing. Each feature area was also checked by deliberately inserting likely bugs and confirming the tests catch them.

## Your requested features

| Feature | Status | Where |
|---|---|---|
| Candidate registration | Built and tested: sign-up, email confirmation, login, password reset | `python/auth.py`, `web/js/views/auth.js` |
| Employer registration | Built and tested, same flow | same |
| Candidate profiles | Built and tested: licences with expiry dates, qualifications, skills with years, work history, document uploads, availability, location, FIFO/relocation | `python/data_layer.py`, `web/js/views/candidate.js` |
| Job listings | Built and tested: employers create, edit, open and close jobs with required and preferred licences, qualifications and skills | `python/access_control.py`, `web/js/views/employer.js` |
| Automatic matching and scoring | Built and tested in Python and JavaScript, both checked against the same expected results | `python/matching_engine.py`, `js/matchingEngine.js`, `fixtures/match_cases.json` |
| Employer dashboards | Built and tested: jobs list, ranked candidates per job, candidate search, shortlists, billing. There are no charts or analytics. | `web/js/views/employer.js` |
| Shortlisting | Built and tested: shortlists with a status per candidate | same |

## Built and tested

**Matching engine.** Weighted score out of 100: licences 30%, skills 25%, qualifications 20%, experience 20%, location 5%. Required licences must be valid on the job's start date. Candidates are marked eligible or not, with the reasons. Ties are broken by id, never by name.

**Data layer.** SQLite schema (`db/schema.sql`) with migrations 001 to 004, a catalogue of licence, qualification and skill names with aliases (e.g. "SS11" → "Standard 11"), and database triggers that stop fee terms and issued invoices being edited.

**Access control.** Every action is checked against who is asking (candidate, employer or admin). Employers see anonymised candidates until contact is unlocked. Allowed and refused actions are recorded in an audit log, which contains no personal details.

**Authentication.** Described under "Architecture" below.

**Two-step unlock.** 1) The candidate approves an employer. 2) The employer accepts the current placement fee terms and unlocks. The unlock is recorded as an introduction, tied to the exact fee-terms version accepted.

**Invoicing (bank transfer).** The employer records a hire and sees the fee. An Australian tax invoice (ABN checked, 10% GST) is issued on the start date, never backdated, numbered MM-00001 onwards, and emailed with a PDF. Admins mark invoices paid and can void them. Bank details come only from server settings.

**Licence expiry reminders.** Emails 90, 60 and 30 days before a licence expires and once after it lapses, never sending the same reminder twice. Suggested expiry dates come from common renewal cycles. Candidates can turn reminders off from their profile or the link in the email.

**Web app.** Phone-first single-page app served by the API: every screen above, plus admin screens (placement terms, invoices, accounts, document checks, reminders, activity log, inviting admins).

**Security monitoring.** Server errors and rate-limit refusals are emailed to `MM_ALERT_EMAIL`. A 5-minute check (`python -m monitoring`) watches the site, HTTPS certificate, disk space, backups and ClamAV, flags bursts of refused actions and failed logins, and reports every new admin login. Alerts are sent once, repeated every 6 hours, and followed by an all-clear. An outside uptime monitor still has to be set up for the server being down.

**Virus scanning.** Every upload is streamed to ClamAV before it's saved. Infected files are refused; if ClamAV is down or unsure, uploads are refused rather than saved unscanned. The live app can't run with scanning off.

**Data export (privacy access requests).** Any user can download a ZIP of everything held about them from their account page (limited to 5 an hour); admins can export any user's data from Accounts. It holds the profile, uploaded files, contact requests, introductions, shortlist entries, hires, reminders and the user's own activity, but no passwords, tokens or employer fees. Employer exports hold their company, jobs and accepted terms, never candidates' details. Every export is logged.

**Daily job.** `python -m daily` sends reminders, then issues invoices that are due.

## Not built yet

| Item | Notes |
|---|---|
| PostgreSQL | Only SQLite works. Fine for a pilot on one server; move before scaling. |
| Licence register checks | Admins can mark an uploaded document as checked by hand. Nothing checks government registers automatically. |
| Uploads in cloud storage | Files are saved on the server's disk (`MM_FILES`). |
| Live deployment | The deployment files in `deploy/` have **not been run on a real server**. |
| Independent security test | Not done. Needed before real candidate data. |
| Legal documents | Privacy policy, candidate terms and employer terms aren't written. The fee-terms text in the app is a placeholder; a lawyer must write it. |
| Two-factor login | Not built. Recommended for admin accounts. |
| Employer account deletion | Candidates can delete their accounts; employers and admins can't (admins can suspend). |
| Partial payments, adjustment notes, recurring subscriptions | Not built; see the README's invoicing section. |
| Native phone apps | It's a phone-friendly website, not an App Store or Google Play app. |
| Charts or analytics on dashboards | Not built. |

## Known limitations to fix before going live

1. **Virus scanning** is built but has only been tested against a stand-in ClamAV daemon. Before taking uploads, run the `virus_scan.py --check` command in step 3 of `docs/DEPLOYMENT.md` on the server; it loads the app's own settings and runs as the service user.
2. **Real client IP behind a proxy:** rate limits use the connecting address. Behind nginx, uvicorn must be started with `--proxy-headers --forwarded-allow-ips=127.0.0.1` (as in `deploy/mining-match.service`), or every user will share one address and hit the limits together.
3. **One server only:** SQLite and local uploads mean the app can't yet run across several servers.
4. **Licence renewal cycles** in `db/catalog_seed.json` are general guidance and need checking against the regulators.

## Architecture

| Part | Technology |
|---|---|
| Language | Python 3.11 or newer (all tests pass on 3.11, 3.12 and 3.13) |
| Web framework | FastAPI with pydantic v2, served by uvicorn |
| Database | SQLite (`db/schema.sql`); PostgreSQL planned |
| Web app | Plain JavaScript modules with no framework and no build step, served from `web/` by the API |
| Matching engine | `python/matching_engine.py` (used by the server) and `js/matchingEngine.js` (identical results, for other clients) |
| PDF invoices | reportlab |
| Email | SMTP with STARTTLS (the mail server's certificate and name are checked), or printed to the console when not configured |
| Tests | Python unittest, Node's test runner, Playwright with Chrome for browser tests |

**Authentication.**
- **Passwords:** hashed with scrypt and a random salt. At least 10 characters, not common, and can't contain the email.
- **Sessions:** random tokens stored only as SHA-256 hashes. A session ends after 7 days unused, 30 days in total, or on logout, password change or reset, or suspension.
- **How the session travels:** the web app uses an HttpOnly, Secure, SameSite=Strict cookie, honoured only with the header `X-Requested-With: mining-match`. That guards against cross-site request forgery. Other clients send `Authorization: Bearer`.
- **Email links** (confirmation and reset) are single-use, time-limited and stored hashed. Tokens sit after `#` in links, so they never reach server logs.
- **No account enumeration:** sign-up, reset and login reveal nothing about which emails are registered.
- **Rate limits:** on logins, sign-ups and emails, stored in the database.
- **Roles:** candidate, employer and admin. The first admin is created from the server's command line, and later admins by invitation.
