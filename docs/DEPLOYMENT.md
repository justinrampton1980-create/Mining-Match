# Deploying Mining Match

> **Status:** these steps and the files in `deploy/` follow standard practice for a Python app behind nginx, but **they have not yet been run on a real server**. Treat the first deployment as a test and work through the checklist at the end before real candidates sign up.

## What the server must provide

| Need | Why |
|---|---|
| A long-running Python 3.11+ process | The app is a FastAPI server (uvicorn) that stays running. |
| Root or sudo access | To install Python packages, nginx, ClamAV and a service. |
| HTTPS certificate | Logins use Secure cookies, which only work over HTTPS. |
| Outbound SMTP or an email service | Confirmation, password reset, invoice and reminder emails. |
| A daily scheduled task (cron) | `python -m daily` for reminders and invoices. |
| Server in Australia | Candidate data is personal information under the Privacy Act. Keeping it onshore avoids cross-border disclosure obligations (APP 8). |
| Disk space for uploads and backups | Uploaded documents up to 10 MB each. |

## Bluehost

**Shared hosting won't work.** Bluehost's own help says shared hosting is built for PHP and MySQL sites, runs Python only as CGI scripts, and doesn't support Python apps that need a persistent server process. Mining Match needs exactly that.

**Use a Bluehost VPS in Sydney.** Bluehost's Self-Managed VPS, Managed VPS and Dedicated plans can be placed in a Sydney data centre. **Choose Sydney at checkout: Bluehost says the location can't be changed after the server is set up.** Check the location list on the order page when you buy, because the available locations differ by plan.

- **Self-Managed VPS:** full root access. You (or ChatGPT and I, through you) run the steps below. This is the best fit.
- **Managed VPS:** check before buying that you get root/SSH access and can run your own service (systemd) and nginx. If the plan only gives cPanel tools for PHP sites, choose Self-Managed instead.
- A small VPS (2 vCPU, 4 GB RAM, 80 GB+ disk) is plenty for a pilot.

Your domain and email can stay on any Bluehost plan; only the app needs the VPS.

## Steps on an Ubuntu 24.04 VPS

```bash
# 1. System packages
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3 python3-venv nginx certbot python3-certbot-nginx clamav-daemon ufw unattended-upgrades sqlite3 age
sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable
# ClamAV: the first signature download can take a few minutes; the scanner starts after it.
sudo systemctl enable --now clamav-freshclam clamav-daemon
sudo systemctl status clamav-daemon --no-pager   # wait until it says "active (running)"

# 2. A user that runs the app, and the code
sudo adduser --system --group --home /srv/mining-match miningmatch
sudo -u miningmatch git clone https://github.com/justinrampton1980-create/Mining-Match.git /srv/mining-match/app
sudo -u miningmatch mkdir -p /srv/mining-match/data/uploads   # database and uploads live here
cd /srv/mining-match/app/python
sudo -u miningmatch python3 -m venv /srv/mining-match/venv
sudo -u miningmatch /srv/mining-match/venv/bin/pip install -r requirements.txt

# 3. Settings (fill in every value; keep the file private)
sudo cp /srv/mining-match/app/deploy/env.example /etc/mining-match.env
sudo nano /etc/mining-match.env
sudo chown root:miningmatch /etc/mining-match.env && sudo chmod 640 /etc/mining-match.env

# Virus scanning must work before the app starts taking uploads (expects "virus scanning OK"):
sudo -u miningmatch bash -c 'set -a; . /etc/mining-match.env; cd /srv/mining-match/app/python && /srv/mining-match/venv/bin/python virus_scan.py --check'

# 4. Database and the first admin (once)
sudo -u miningmatch bash -c 'set -a; . /etc/mining-match.env; cd /srv/mining-match/app/python && /srv/mining-match/venv/bin/python -c "import os, data_layer as dl, access_control as ac, auth; c = dl.connect(os.environ[\"MM_DB\"]); dl.init_schema(c); dl.seed_catalog(c); ac.create_admin(c, input(\"Admin email: \"), auth.hash_password(input(\"Password: \")))"'

# 5. Service, web server, HTTPS
sudo cp /srv/mining-match/app/deploy/mining-match.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mining-match
sudo cp /srv/mining-match/app/deploy/nginx-mining-match.conf /etc/nginx/sites-available/mining-match
sudo sed -i 's/miningmatch.example.com.au/YOUR-DOMAIN/g' /etc/nginx/sites-available/mining-match
sudo ln -s /etc/nginx/sites-available/mining-match /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d YOUR-DOMAIN

# 6. Daily job and backups (make the backup key first; see "Backups" below)
# Owned by root so the app's user can run the script but not change it.
sudo install -o root -g miningmatch -m 750 /srv/mining-match/app/deploy/backup.sh /srv/mining-match/backup.sh
sudo -u miningmatch /srv/mining-match/backup.sh --check   # must say "settings OK"
sudo crontab -u miningmatch /srv/mining-match/app/deploy/crontab
```

Point your domain's DNS (an A record) at the VPS's IP address before running certbot.

## Virus scanning

Every upload is checked by ClamAV before it's saved. The app talks to the ClamAV daemon on
`/var/run/clamav/clamd.ctl` (change it with `MM_CLAMD_SOCKET`, or use `MM_CLAMD_HOST` and
`MM_CLAMD_PORT` for a scanner on another machine). `clamav-freshclam` keeps the virus signatures
up to date by itself.

**Scanning fails closed.** If the scanner is stopped, too slow (30 seconds, `MM_CLAMD_TIMEOUT`)
or gives any unclear answer, uploads are refused with "uploads are paused because virus scanning
is unavailable" and nothing is saved. The rest of the site keeps working. Infected files are
refused, and both kinds of refusal are written to the activity log.

- ClamAV needs about 1.5 GB of memory; the 4 GB VPS suggested above has room for it.
- If uploads say scanning is unavailable: `sudo systemctl restart clamav-daemon`, wait a minute,
  then run the `virus_scan.py --check` command from step 3.
- `MM_VIRUS_SCAN=0` turns scanning off, but only for local development: the app refuses to start
  with it on a live site (`MM_SECURE_COOKIES` unset or `1`).

## Backups

`deploy/backup.sh` runs nightly at 2:15am. It streams the database and the uploads straight into
[age](https://age-encryption.org) encryption, so **no unencrypted copy is ever written to disk**,
and keeps 14 days of them in `/srv/mining-match/backups`:

- `db-YYYY-MM-DD.sql.gz.age`: the database, as a consistent SQL dump
- `uploads-YYYY-MM-DD.tar.gz.age`: the uploads folder

**Encryption can't be turned off.** If the key is missing or wrong, a tool is missing, the database
fails its integrity check or any step fails, the script writes nothing, logs why in
`/srv/mining-match/backup.log` and emails `MM_BACKUP_ALERT_EMAIL`. It also removes any unencrypted
backups left by older versions of the script, and stops with an error if anything other than
encrypted backups turns up in the backup folder.

**Make the key on your own computer, not the server** (install age first, e.g. `brew install age`
on a Mac, or from the age website on Windows):

```sh
age-keygen -o mining-match-backup-key.txt
```

It prints a public key starting `age1...`. Put that in `/etc/mining-match.env` as
`MM_BACKUP_RECIPIENT`, and your email as `MM_BACKUP_ALERT_EMAIL`. Keep
`mining-match-backup-key.txt` (the private key, starting `AGE-SECRET-KEY-`) somewhere safe off
the server, such as a password manager, ideally with a second copy somewhere else.
**Without it the backups can't be opened**, and with it anyone can open them, so never put it on
the server or the backup storage. The script refuses to run if you paste the private key into
the settings by mistake.

Check the settings at any time (writes nothing):

```sh
sudo -u miningmatch /srv/mining-match/backup.sh --check
```

**To restore**, copy the backup files and the key to a safe machine and run:

```sh
age -d -i mining-match-backup-key.txt db-YYYY-MM-DD.sql.gz.age | gunzip | sqlite3 mm.db
age -d -i mining-match-backup-key.txt uploads-YYYY-MM-DD.tar.gz.age | tar -xzf -
```

`mm.db` must not exist before the first command. Then stop the app, put `mm.db` at `MM_DB` and
the uploads folder at `MM_FILES`, and start it again. Test a restore like this before launch, and
every few months after.

## Before real candidates sign up

- [ ] `virus_scan.py --check` says "virus scanning OK", and uploading a file as a test candidate works.
- [ ] `MM_SECURE_COOKIES` unset or `1`, `MM_DOCS=0`, `MM_BASE_URL` is the https address.
- [ ] Email sends for real (sign up a test account).
- [ ] Billing settings set and a test invoice checked by your accountant.
- [ ] Placement terms, privacy policy and candidate terms written by a lawyer and published.
- [ ] Backup key made and stored off the server; `backup.sh --check` says settings OK; a failure email arrives (e.g. run it once with a wrong key); backups are copied off the server; a restore has been tested.
- [ ] Independent security test.
- [ ] Move to PostgreSQL before growing beyond a pilot (not yet built).

## Updating

```bash
cd /srv/mining-match/app && sudo -u miningmatch git pull
sudo -u miningmatch /srv/mining-match/venv/bin/pip install -r python/requirements.txt
# apply any new files in db/migrations/ to the database, in order, once each (back up first)
sudo systemctl restart mining-match
```
