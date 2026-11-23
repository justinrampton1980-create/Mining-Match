# Security

## Reporting a problem

Don't open a public GitHub issue for a security problem. Email the owner of this repository privately, and include steps to reproduce it.

## Safeguards in the code (all covered by automated tests)

**Accounts and sessions**
- Passwords are hashed with scrypt and a random salt. They must be at least 10 characters, not a common password, and not contain the email.
- Session tokens, email-confirmation tokens and reset tokens are random and stored only as SHA-256 hashes. A database leak doesn't reveal working sessions.
- Sessions expire after 7 days unused or 30 days in total. Password reset ends every session; password change ends the others; suspension ends all of them straight away.
- Login, sign-up and reset never reveal whether an email is registered.
- Rate limits: 5 failed logins per email or 30 per IP address in 15 minutes, plus limits on emails sent.

**Two-factor login** (`python/two_factor.py`, `python/test_two_factor.py`)
- Anyone can turn it on from their Account page; **admins must** before the admin pages work (`MM_REQUIRE_ADMIN_2FA`). Codes come from any standard authenticator app (TOTP, RFC 6238; checked against the RFC's test values).
- With it on, the right password only earns a 5-minute login challenge, never a session or cookie. The challenge needs a code: 5 wrong codes end it, and 10 wrong codes per account in 15 minutes lock code entry for that account.
- A code works once: one somebody watched being typed is useless. Codes are accepted 30 seconds either side of now, for phone clock drift.
- Ten one-time backup codes for a lost phone, shown once and stored only as hashes. Using one emails the owner.
- Turning it off, or making new backup codes, needs the password and a current code. Turning it on logs out other devices. Turning it on or off emails the owner. A password reset does **not** turn it off, so access to someone's email isn't enough to get into their account.
- Secrets are encrypted in the database with `MM_SECRET_KEY` (tamper-checked), so a copy of the database alone doesn't reveal them.
- An admin can reset it for someone who lost their phone and codes. A reason is required, it is audit-logged, it logs the person out everywhere and emails them, and admins can't reset their own.

**The website**
- **Login cookie:** HttpOnly (page scripts can't read it), Secure (HTTPS only) and SameSite=Strict. The server accepts it only with the header `X-Requested-With: mining-match`, which another site can't add, so the cookie can't be used for cross-site request forgery.
- **Content Security Policy:** scripts load only from the site itself, with no inline scripts, so injected script can't run. The API also sends `X-Content-Type-Options`, `Referrer-Policy` and frame-blocking headers.
- **No HTML injection:** all text people type is inserted as text, never parsed as HTML. A browser test tries an injection and checks it doesn't run.
- **Tokens stay out of logs:** emailed links carry tokens after `#`, so they never reach the server or its logs, and the app removes them from the address bar after use.

**Access control**
- Every request is checked against the caller's role and ownership on every call. Nothing is trusted from the browser.
- A permission grid test calls every protected function as every kind of user, and fails if a new function is added without being in the grid.
- Employers see an allow-listed anonymous view of candidates until the candidate approves them *and* they accept the fee terms.
- Allowed and refused actions are written to an audit log that holds no personal details.

**Uploads**
- The file type is decided from the file's content, not its name. Only PDF, Word, JPEG and PNG up to 10 MB are accepted.
- Filenames are cleaned, and storage paths can't escape the upload folder.
- Files are always sent as downloads, never displayed inside the site.

**Money**
- Bank details come only from server settings, never from the website, so nobody who breaks into an account can redirect payments.
- Issued invoices and published fee terms can't be changed: database triggers refuse it.
- ABNs are checked with their check digits.

**Errors** never echo submitted values (so passwords can't leak into logs), and the interactive API docs can be switched off in production (`MM_DOCS=0`).

## Known gaps (to fix before real data)

| Gap | What to do |
|---|---|
| Uploads aren't virus-scanned in production | Connect ClamAV (installed by the deployment steps) through the `scan_upload` hook in `api.app_from_environment()`. |
| Two-factor reset is a manual identity check | Before resetting someone's two-factor login, confirm who they are another way (e.g. call the number on file). Account thieves ask for this too. |
| Single-server SQLite | Move to PostgreSQL; encrypt backups and copy them off the server. |
| Deployment files not run on a real server | Test them on the VPS; run `nginx -t`. |
| No independent security test | Commission one before launch. |
| Admin accounts can see everything | Keep the number of admins small and review the activity log. |
