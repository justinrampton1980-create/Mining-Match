#!/usr/bin/env bash
# Nightly backup of the database and the uploads, encrypted, kept for 14 days.
#
#   backup.sh           make tonight's backup
#   backup.sh --check   check the settings and tools only; writes nothing
#
# Encryption is not optional. Both backups are streamed straight into age
# (https://age-encryption.org), encrypting to the public key in MM_BACKUP_RECIPIENT, so no
# unencrypted copy is ever written to disk, not even temporarily. The matching private key
# is kept OFF this server, so a break-in here or at the backup storage can't read them.
# If anything is wrong (no key, the private key by mistake, a missing tool, a failed step)
# the script writes nothing, exits with an error and, if MM_BACKUP_ALERT_EMAIL is set,
# emails that address.
#
# Files written, in MM_BACKUP_DIR (default /srv/mining-match/backups):
#   db-YYYY-MM-DD.sql.gz.age        the database (a consistent SQL dump, gzipped)
#   uploads-YYYY-MM-DD.tar.gz.age   the uploads folder
# Copy them off the server too (e.g. to Australian-region object storage).
# Restoring is covered in docs/DEPLOYMENT.md.
set -Eeuo pipefail
umask 077

ENV_FILE=${MM_ENV_FILE:-/etc/mining-match.env}
FAIL_REASON=""
PARTIAL=()  # this run's half-written files, removed if the run fails

log() { printf '%s backup: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }

fail() {
  FAIL_REASON=$*
  exit 1
}

on_error() {  # a command failed without a fail() of its own
  [ -n "$FAIL_REASON" ] || FAIL_REASON="step failed (line $1): $2"
}

on_exit() {
  local status=$?
  local f
  for f in "${PARTIAL[@]+"${PARTIAL[@]}"}"; do rm -f -- "$f"; done
  if [ "$status" -ne 0 ]; then
    [ -n "$FAIL_REASON" ] || FAIL_REASON="stopped with exit status $status"
    log "FAILED: $FAIL_REASON. No backup was written."
    send_alert "$FAIL_REASON" || log "could not send the failure email to ${MM_BACKUP_ALERT_EMAIL:-}"
  fi
  exit "$status"
}

send_alert() {
  [ -n "${MM_BACKUP_ALERT_EMAIL:-}" ] || return 0
  "${MM_PYTHON:-/srv/mining-match/venv/bin/python}" \
    "${MM_APP_DIR:-/srv/mining-match/app}/python/backup_alert.py" "$1"
}

trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR
trap on_exit EXIT

# ------------------------------------------------------------------ settings
[ -r "$ENV_FILE" ] || fail "can't read the settings file $ENV_FILE"
set -a  # export the settings, so the failure email can use the SMTP ones
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

DEST=${MM_BACKUP_DIR:-/srv/mining-match/backups}
LOCK=${MM_BACKUP_LOCK:-$DEST.lock}
KEEP_DAYS=${MM_BACKUP_KEEP_DAYS:-14}
RECIPIENT=${MM_BACKUP_RECIPIENT:-}
MODE=${1:-backup}
[ "$MODE" = backup ] || [ "$MODE" = --check ] || fail "unknown option $MODE (use --check or nothing)"

for tool in sqlite3 age gzip tar flock find; do
  command -v "$tool" >/dev/null 2>&1 || fail "the $tool tool isn't installed (see docs/DEPLOYMENT.md)"
done

case "$RECIPIENT" in
  "") fail "MM_BACKUP_RECIPIENT isn't set in $ENV_FILE; backups are never written unencrypted (see docs/DEPLOYMENT.md)" ;;
  AGE-SECRET-KEY-*) fail "MM_BACKUP_RECIPIENT is a PRIVATE key. Remove it from this server now and use the public key (age1...) instead" ;;
esac
[[ "$RECIPIENT" =~ ^age1[02-9ac-hj-np-z]{58}$ ]] || fail "MM_BACKUP_RECIPIENT isn't an age public key (age1... , 62 characters)"
printf 'test' | age -r "$RECIPIENT" >/dev/null 2>&1 || fail "age won't encrypt to MM_BACKUP_RECIPIENT; check the key"

[[ "$KEEP_DAYS" =~ ^[1-9][0-9]*$ ]] || fail "MM_BACKUP_KEEP_DAYS must be a whole number of days"
[ -n "${MM_DB:-}" ] && [ -f "$MM_DB" ] || fail "the database MM_DB (${MM_DB:-not set}) doesn't exist"
[ -n "${MM_FILES:-}" ] && [ -d "$MM_FILES" ] || fail "the uploads folder MM_FILES (${MM_FILES:-not set}) doesn't exist"
FILES_ABS=$(cd "$MM_FILES" && pwd -P)
mkdir -p "$DEST"
chmod 700 "$DEST"
DEST_ABS=$(cd "$DEST" && pwd -P)
case "$DEST_ABS/" in
  "$FILES_ABS"/*) fail "MM_BACKUP_DIR is inside the uploads folder; put backups somewhere else" ;;
esac

if [ "$MODE" = --check ]; then
  log "settings OK: backups go to $DEST_ABS, encrypted to ${RECIPIENT:0:12}..., kept $KEEP_DAYS days"
  exit 0
fi

exec 9>"$LOCK"
flock -n 9 || fail "another backup is still running"

# ------------------------------------------- leftovers from older script versions
# Before encryption, backups were plain db-*.sqlite / uploads-*.tar.gz files, and one version
# used a .work.* folder. Remove any, so no unencrypted copies are left lying around.
while IFS= read -r -d '' old; do
  log "removing old unencrypted backup ${old##*/}"
  rm -rf -- "$old"
done < <(find "$DEST" -mindepth 1 -maxdepth 1 \( -name 'db-*.sqlite' -o -name 'uploads-*.tar.gz' \
  -o -name '.work.*' -o -name '*.age.tmp' \) -print0)

# ------------------------------------------------------------------ backup
check=$(sqlite3 "$MM_DB" "PRAGMA quick_check;" 2>&1) || fail "can't read the database: $check"
[ "$check" = ok ] || fail "the database failed its integrity check: ${check:0:200}"

STAMP=$(date +%Y-%m-%d)
DB_OUT="$DEST/db-$STAMP.sql.gz.age"
UP_OUT="$DEST/uploads-$STAMP.tar.gz.age"
PARTIAL=("$DB_OUT.tmp" "$UP_OUT.tmp")

# .dump reads inside one transaction, so it's a consistent copy even while the site is running.
sqlite3 -bail "$MM_DB" .dump | gzip -c | age -r "$RECIPIENT" -o "$DB_OUT.tmp" \
  || fail "the database backup failed"
tar -czf - -C "$(dirname "$FILES_ABS")" "$(basename "$FILES_ABS")" | age -r "$RECIPIENT" -o "$UP_OUT.tmp" \
  || fail "the uploads backup failed"

for f in "$DB_OUT.tmp" "$UP_OUT.tmp"; do  # both must be real age files before either is kept
  [ "$(head -c 21 "$f")" = "age-encryption.org/v1" ] || fail "${f##*/} isn't an encrypted age file"
  [ "$(wc -c <"$f")" -gt 200 ] || fail "${f##*/} is suspiciously small"
done
mv -f "$DB_OUT.tmp" "$DB_OUT"
mv -f "$UP_OUT.tmp" "$UP_OUT"
PARTIAL=()

find "$DEST" -mindepth 1 -maxdepth 1 -type f -name '*.age' -mtime +"$KEEP_DAYS" -delete

# Only encrypted backups may be in the backup folder.
unexpected=$(find "$DEST" -mindepth 1 -maxdepth 1 ! -name '*.age' -printf '%f ')
[ -z "$unexpected" ] || fail "unexpected files in $DEST_ABS that aren't encrypted backups: $unexpected"

log "done: ${DB_OUT##*/} ($(wc -c <"$DB_OUT") bytes), ${UP_OUT##*/} ($(wc -c <"$UP_OUT") bytes)"
