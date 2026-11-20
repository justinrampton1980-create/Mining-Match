#!/bin/sh
# Nightly backup: a consistent copy of the database plus the uploads, kept for 14 days.
# Copy /srv/mining-match/backups off the server too (e.g. to Australian-region object storage).
set -eu
. /etc/mining-match.env
DEST=/srv/mining-match/backups
STAMP=$(date +%Y-%m-%d)
mkdir -p "$DEST"
sqlite3 "$MM_DB" ".backup '$DEST/db-$STAMP.sqlite'"
tar -czf "$DEST/uploads-$STAMP.tar.gz" -C "$(dirname "$MM_FILES")" "$(basename "$MM_FILES")"
chmod 600 "$DEST/db-$STAMP.sqlite" "$DEST/uploads-$STAMP.tar.gz"
find "$DEST" -type f -mtime +14 -delete
