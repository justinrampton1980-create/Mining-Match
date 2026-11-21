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
sudo cp /srv/mining-match/app/deploy/backup.sh /srv/mining-match/backup.sh && sudo chmod 750 /srv/mining-match/backup.sh
sudo crontab -u miningmatch /srv/mining-match/app/deploy/crontab
```

Point your domain's DNS (an A record) at the VPS's IP address before running certbot.

## Backups

`deploy/backup.sh` runs nightly. It copies the database and the uploads, encrypts both with
[age](https://age-encryption.org) and keeps 14 days of them in `/srv/mining-match/backups`.

**Make the key on your own computer, not the server** (install age first, e.g. `brew install age`
on a Mac, or from the age website on Windows):

```sh
age-keygen -o mining-match-backup-key.txt
```

It prints a public key starting `age1...`. Put that in `/etc/mining-match.env` as
`MM_BACKUP_RECIPIENT`. Keep `mining-match-backup-key.txt` (the private key) somewhere safe off
the server, such as a password manager. **Without it the backups can't be opened**, and with it
anyone can open them, so don't copy it to the server or the backup storage. The script stops with
an error if `MM_BACKUP_RECIPIENT` isn't set, rather than writing unencrypted backups.

**To restore**, copy the backup files and the key to a safe machine and run:

```sh
age -d -i mining-match-backup-key.txt -o mm.db db-YYYY-MM-DD.sqlite.age
age -d -i mining-match-backup-key.txt uploads-YYYY-MM-DD.tar.gz.age | tar -xzf -
```

Then stop the app, put `mm.db` at `MM_DB` and the uploads folder at `MM_FILES`, and start it again.

## Before real candidates sign up

- [ ] Connect virus scanning of uploads (see SECURITY.md). **Not yet built into the production app.**
- [ ] `MM_SECURE_COOKIES` unset or `1`, `MM_DOCS=0`, `MM_BASE_URL` is the https address.
- [ ] Email sends for real (sign up a test account).
- [ ] Billing settings set and a test invoice checked by your accountant.
- [ ] Placement terms, privacy policy and candidate terms written by a lawyer and published.
- [ ] Backup key made and stored off the server; backups run, are copied off the server, and a restore has been tested.
- [ ] Independent security test.
- [ ] Move to PostgreSQL before growing beyond a pilot (not yet built).

## Updating

```bash
cd /srv/mining-match/app && sudo -u miningmatch git pull
sudo -u miningmatch /srv/mining-match/venv/bin/pip install -r python/requirements.txt
# apply any new files in db/migrations/ to the database, in order, once each (back up first)
sudo systemctl restart mining-match
```
