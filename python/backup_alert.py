"""Emails MM_BACKUP_ALERT_EMAIL when the nightly backup fails. Called by deploy/backup.sh.

    python backup_alert.py "what went wrong"

Uses the same email settings as the site (MM_SMTP_*; without them it prints the email).
"""
import os
import socket
import sys

import auth


def main(argv) -> int:
    to = os.environ.get("MM_BACKUP_ALERT_EMAIL", "").strip()
    if not to:
        return 0
    reason = " ".join(argv[1:]).strip() or "unknown error"
    body = (
        f"Tonight's Mining Match backup on {socket.gethostname()} failed:\n\n"
        f"    {reason}\n\n"
        "No backup was written, and none will be until this is fixed. Backups are never written\n"
        "unencrypted, so a problem with the backup key stops them.\n\n"
        "After fixing it, check the settings with:  /srv/mining-match/backup.sh --check\n"
        "and see /srv/mining-match/backup.log for the full log.\n"
    )
    auth.mailer_from_env().send(to, "Mining Match backup FAILED", body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
