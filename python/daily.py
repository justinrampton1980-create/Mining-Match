"""Mining Match daily jobs. Run once a day with the same settings as the server, e.g. from cron:

    50 6 * * *  cd /app/python && python -m daily

1. Licence expiry reminders to candidates (reminders.py).
2. Scheduled invoices whose date has come (billing.py), if invoicing is set up.
Each job is safe to run more than once a day.
"""
import os
import sys

import auth
import billing
import data_layer as dl
import reminders


def main() -> int:
    conn = dl.connect(os.environ.get("MM_DB", "mining_match.db"))
    dl.init_schema(conn)
    dl.seed_catalog(conn)
    mailer = auth.mailer_from_env()
    base_url = os.environ.get("MM_BASE_URL", "http://localhost:8000")
    status = 0

    result = reminders.send_expiry_reminders(conn, mailer, base_url)
    print(f"Licence reminders: {result['emails']} email(s) about {result['licences']} licence(s)"
          + (f", {result['failed']} failed (will retry tomorrow)" if result["failed"] else ""))
    if result["failed"]:
        status = 1

    config = billing.BillingConfig.from_env()
    if config is None:
        print("Invoices: invoicing isn't set up, skipped.")
    else:
        issued = billing.issue_due_invoices(conn, config, mailer, base_url)
        print(f"Invoices: issued {', '.join(issued)}" if issued else "Invoices: none due today.")
    return status


if __name__ == "__main__":
    sys.exit(main())
