#!/bin/sh
# Nightly backup: a consistent copy of the database plus the uploads, kept for 14 days.
#
# Both files are encrypted with age (https://age-encryption.org) to the public key in
# MM_BACKUP_RECIPIENT. Keep the matching private key OFF this server (e.g. in a password
# manager), so someone who breaks into the server, or the place backups are copied to,
# can't read them. See docs/DEPLOYMENT.md for making the key and restoring.
#
# Copy /srv/mining-match/backups off the server too (e.g. to Australian-region object storage).
set -eu
. "${MM_ENV_FILE:-/etc/mining-match.env}"
: "${MM_BACKUP_RECIPIENT:?set MM_BACKUP_RECIPIENT (an age public key, age1...) in /etc/mining-match.env}"
DEST=${MM_BACKUP_DIR:-/srv/mining-match/backups}
STAMP=$(date +%Y-%m-%d)
umask 077
mkdir -p "$DEST"
WORK=$(mktemp -d "$DEST/.work.XXXXXX")  # unencrypted copies live here only briefly
trap 'rm -rf "$WORK"' EXIT

sqlite3 "$MM_DB" ".backup '$WORK/db.sqlite'"
tar -czf "$WORK/uploads.tar.gz" -C "$(dirname "$MM_FILES")" "$(basename "$MM_FILES")"

for f in db.sqlite uploads.tar.gz; do
  out="$DEST/${f%%.*}-$STAMP.${f#*.}.age"
  age -r "$MM_BACKUP_RECIPIENT" -o "$out.tmp" "$WORK/$f"
  mv "$out.tmp" "$out"  # a half-written file is never left under the real name
done

find "$DEST" -maxdepth 1 -type f -mtime +14 -delete
