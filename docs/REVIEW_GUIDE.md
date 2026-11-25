# Review guide

For anyone reviewing this code, including the ChatGPT agent working on Mining Match with Claude. Start with [STATUS.md](../STATUS.md), which separates what's built and tested from what isn't.

## Map of the code

| Layer | File | What to look at |
|---|---|---|
| Matching | `python/matching_engine.py`, `js/matchingEngine.js` | Scoring weights, licence validity on the job's start date, tie-breaking. Shared expected results: `fixtures/match_cases.json`. |
| Database | `db/schema.sql`, `db/migrations/` | Triggers that lock fee terms and issued invoices; which tables cascade on candidate deletion and which deliberately don't. |
| Data access | `python/data_layer.py` | Parameterised SQL throughout; `LIKE ... ESCAPE` in searches. |
| Permissions | `python/access_control.py` | `authorize()`, the anonymised employer view (`ANONYMOUS_FIELDS`), the two-step unlock, audit logging. |
| Login | `python/auth.py` | scrypt hashing, hashed session and email tokens, expiry, rate limits, no account enumeration. |
| HTTP | `python/api.py` | Cookie plus `X-Requested-With` check, security headers and CSP, upload handling, error handlers. |
| Billing | `python/billing.py` | GST rounding, fee window, issue dates, invoice numbering, ABN check, bank details from settings only. |
| Reminders | `python/reminders.py`, `python/daily.py` | Once-per-stage logic, opt-out token. |
| Web app | `web/js/` | `dom.js` builds every element with text nodes only; routes in `app.js`. |
| Deployment | `deploy/`, `docs/DEPLOYMENT.md` | systemd hardening, nginx proxy headers, encrypted backups (`deploy/backup.sh`, tested by `python/test_backup.py`). **Untested on a real server.** |

## Running the checks

```bash
cd python && pip install -r requirements.txt && python -m unittest    # 190 tests
cd ../js && node --test                                                # 20 tests
```

`test_access_control.py`, `test_billing.py` and `test_reminders.py` each contain a permission grid. It calls every protected function as every kind of user, and fails if a new function isn't listed.

## Known gaps worth a second opinion

1. Virus scanning (`python/virus_scan.py`) has been tested against a stand-in ClamAV daemon speaking its protocol, not yet a real one. Run the `virus_scan.py --check` command in step 3 of `docs/DEPLOYMENT.md` on the server; it loads the app's own settings and runs as the service user.
2. Rate limits rely on uvicorn's `--proxy-headers` behind nginx for the real client IP.
3. SQLite with a single worker; the PostgreSQL move isn't started.
4. Admins can see every record; there's no two-factor login.
5. The unsubscribe link page posts automatically when opened, the same as email confirmation. A mail scanner that runs page scripts could turn a candidate's reminders off.
6. Deployment files have only been partly checked: the uvicorn command was run, but nginx and systemd were not.

## Ways of working

- Changes go through pull requests to `main`, never pushed straight to it.
- Every change comes with tests. Don't mark a feature done in STATUS.md unless its tests pass.
- Keep secrets out of the repository; settings go in `/etc/mining-match.env` on the server (see `deploy/env.example`).
