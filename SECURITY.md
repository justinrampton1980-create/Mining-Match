# Security

## Reporting a problem

Don't open a public GitHub issue for a security problem. Email the owner of this repository privately, and include steps to reproduce it.

## Safeguards in the code (all covered by automated tests)

**Accounts and sessions**
- Passwords are hashed with scrypt and a random salt. They must be at least 10 characters, not a common password, and not contain the email.
- Session tokens, email-confirmation tokens and reset tokens are random and stored only as SHA-256 hashes. A database leak doesn't reveal working sessions.
- Sessions expire after 7 days unused or 30 days in total. Password reset ends every session; password change ends the others; suspension ends all of them straight away.
- Login, sign-up and reset never reveal whether an email is registered, by their answer or by how long they take.
- Employer candidate searches and job rankings are limited to 100 an hour and 500 a day per employer, so nobody can sweep the whole candidate pool.
- Rate limits on failed logins: 5 for one account from one IP address, or 30 from one IP address, in 15 minutes; and 100 for one account in an hour from all addresses together. Someone else guessing at an account can't lock its owner out. Emails sent are limited too.

**Email**
- The connection to the mail server is encrypted with STARTTLS, and the server's certificate and name are checked first. If the check fails nothing is sent: not the mail account's password, and not the email (which may hold a password-reset link or an invoice). The mail provider must therefore present a valid certificate for the name in `MM_SMTP_HOST`.

**Monitoring**
- Server errors and rate limits refusing people are emailed to `MM_ALERT_EMAIL` (at most hourly per kind); visitors only ever see "something went wrong", never internal details.
- Every 5 minutes `python -m monitoring` checks the site, HTTPS certificate, disk, last night's backup and ClamAV, and looks for bursts of refused actions by one account and of failed logins site-wide. Every new admin login is reported.
- An outside uptime monitor is needed for the server being down altogether (DEPLOYMENT.md).

**Two-factor login (admins)**
- Admins log in with their password and a 6-digit code from an authenticator app (standard TOTP). A correct password alone starts no session; the first login sets up the app before anything else.
- An admin session only works while two-factor is on, so even a session made some other way can't be used without it.
- Each code works once; a login allows 5 wrong codes, and an account 10 in 15 minutes.
- Ten one-time recovery codes, stored hashed, cover a lost phone. Another admin can reset two-factor (logged), or the server's command line can for the only admin (`python two_factor.py reset EMAIL`).

**The website**
- **Login cookie:** HttpOnly (page scripts can't read it), Secure (HTTPS only) and SameSite=Strict. The server accepts it only with the header `X-Requested-With: mining-match`, which another site can't add, so the cookie can't be used for cross-site request forgery.
- **Content Security Policy:** scripts load only from the site itself, with no inline scripts, so injected script can't run. The API also sends `X-Content-Type-Options`, `Referrer-Policy` and frame-blocking headers, and over HTTPS a `Strict-Transport-Security` header so browsers never fall back to plain http.
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

**Virus scanning**
- Every upload is streamed to ClamAV before it's saved; infected files are refused.
- It fails closed: if ClamAV is down, slow or unclear, uploads are refused, never saved unscanned.
- A live site can't run with scanning off: `MM_VIRUS_SCAN=0` is refused unless the site is in local-development mode.
- The whole scan has one 30-second limit, and an answer that is cut short counts as "couldn't scan", never as clean.
- ClamAV runs on the same server and the app talks to it over a local socket. Scanning over the network isn't supported (the app won't start if it is configured), because ClamAV's network protocol is neither encrypted nor authenticated.
- ClamAV is set to flag, not skip, files or archives over its size limits (`AlertExceedsMax yes`, set by the deployment steps).
- Refused uploads are written to the activity log.

**Backups**
- Nightly backups are streamed straight into encryption to a public key whose private key is kept off the server, so no unencrypted copy is ever written to disk and a break-in at the server or the backup storage doesn't expose them.
- There is no unencrypted fallback: a missing or wrong key (including the private key pasted by mistake), a missing tool, a database that fails its integrity check or any failed step means nothing is written and an alert email is sent. Unencrypted backups from older versions are removed.

**Money**
- Bank details come only from server settings, never from the website, so nobody who breaks into an account can redirect payments.
- Issued invoices and published fee terms can't be changed: database triggers refuse it.
- ABNs are checked with their check digits.

**Errors** never echo submitted values (so passwords can't leak into logs), and the interactive API docs can be switched off in production (`MM_DOCS=0`).

## Known gaps (to fix before real data)

| Gap | What to do |
|---|---|
| Single-server SQLite | Move to PostgreSQL; copy the (encrypted) backups off the server. |
| Deployment files not run on a real server | Test them on the VPS; run `nginx -t`. |
| No independent security test | Commission one before launch. |
| Admin accounts can see everything | Keep the number of admins small and review the activity log. |
