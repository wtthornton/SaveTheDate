#!/usr/bin/env bash
#
# Off-machine backup of the production database. TAP-7733 item 5.
#
#   scripts/backup.sh            dump, verify, upload, prune
#   scripts/backup.sh --local    dump and verify only; do NOT upload (for testing)
#
# Run on a timer by `deploy/savethedate-backup.timer`.
#
# LOSING THE GUEST LIST IS THE ONE FAILURE HERE WITH NO RECOVERY PATH, and the date
# cannot move. Three things follow, and each is enforced below rather than hoped for:
#
#   1. The dump is VERIFIED before it is uploaded. `pg_dump` exiting 0 is not proof
#      the file is readable; `pg_restore --list` reading the archive's table of
#      contents is. An unverified dump is a hypothesis.
#   2. It goes OFF THIS MACHINE. A backup on the same disk as the database is not a
#      backup. If the upload cannot happen, this script FAILS — it does not quietly
#      leave a local copy and exit 0, because a timer reporting success while
#      nothing leaves the box is worse than no timer.
#   3. Every dump carries a MANIFEST of row counts, taken in the same transaction.
#      `scripts/restore-drill.sh` restores the dump and compares against it, so the
#      drill checks the data arrived rather than merely that a file existed.
#
# Not encrypted client-side, and that is a decision rather than an oversight. R2
# encrypts at rest and the bucket is private, so the realistic threat this would
# address is Cloudflare-account compromise — while the cost is a key that can be
# lost, which would turn every backup into an unreadable file. For a guest list of a
# hundred names the key-loss risk is the larger one. If that trade is ever revisited,
# `age` is the tool, and the key belongs somewhere that is NOT this machine.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT}/docker-compose.prod.yml"
ENV_FILE="${ROOT}/.env.prod"
BACKUP_ENV="${ROOT}/.env.backup"
STAGING="${BACKUP_STAGING:-${ROOT}/.backups}"

# Keep a year of daily dumps. The realistic disaster is a bad migration noticed a
# week later, not a disk dying, so depth matters more than frequency — and at a few
# hundred kilobytes a dump, a year costs a rounding error against R2's 10GB free
# tier. This also spans the whole RSVP window, which closes in February 2028.
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-365}"

LOCAL_ONLY=0
[ "${1:-}" = "--local" ] && LOCAL_ONLY=1

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP="${STAGING}/savethedate-${STAMP}.dump"
MANIFEST="${STAGING}/savethedate-${STAMP}.manifest.json"

log() { echo "[backup $(date -u +%H:%M:%SZ)] $*"; }
die() { echo "[backup] FAILED: $*" >&2; exit 1; }

mkdir -p "$STAGING"
chmod 700 "$STAGING"

[ -f "$ENV_FILE" ] || die "missing $ENV_FILE — the production stack is not configured"

dc() { docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }

# -- 1. The manifest, BEFORE the dump ---------------------------------------
#
# Row counts come from the same database a moment before `pg_dump` opens its
# snapshot. They are a sanity check for the restore drill, not an exact ledger: a
# write landing between the two would show up as a one-row difference, which is why
# the drill treats a SHORTFALL as failure and a surplus as information.
log "counting rows in the production database"
COUNTS="$(dc exec -T db psql -U savethedate -d savethedate -At -F$'\t' -c "
  select 'events', count(*) from events
  union all select 'guests', count(*) from guests
  union all select 'rsvps', count(*) from rsvps
  union all select 'attendees', count(*) from attendees
  union all select 'hosts', count(*) from hosts
  order by 1;
")" || die "could not read the production database"

# The counts arrive as an argument, not on stdin: stdin is the program itself.
python3 - "$MANIFEST" "$STAMP" "$COUNTS" <<'PY' || die "could not write the manifest"
import json, sys
manifest_path, stamp, raw = sys.argv[1], sys.argv[2], sys.argv[3]
counts = {}
for line in raw.splitlines():
    if not line.strip():
        continue
    table, n = line.split("\t")
    counts[table] = int(n)
with open(manifest_path, "w") as fh:
    json.dump({"taken_at": stamp, "row_counts": counts}, fh, indent=2, sort_keys=True)
    fh.write("\n")
print("  " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
PY

# -- 2. The dump -------------------------------------------------------------
#
# Custom format: compressed in place, and readable by `pg_restore --list`, which is
# what makes step 3 possible. A plain-SQL dump can only be verified by restoring it.
log "dumping"
dc exec -T db pg_dump -U savethedate -d savethedate --format=custom --no-owner --no-acl \
  >"$DUMP" || die "pg_dump failed"

[ -s "$DUMP" ] || die "the dump is empty"

# -- 3. Verify the archive is readable ---------------------------------------
#
# pg_dump can exit 0 and still leave a truncated file if the pipe breaks or the disk
# fills. Reading the table of contents back proves the archive parses and that the
# tables are in it.
log "verifying the archive"
TOC="$(dc exec -T db pg_restore --list <"$DUMP" 2>/dev/null)" || die "the dump is not a readable archive"
# A data SECTION for each table, which is not the same as rows — pg_dump emits one
# for an empty table too. Proving rows arrived is the restore drill's job, via the
# manifest; claiming it here would be the kind of overstatement that gets believed.
for table in events guests rsvps hosts; do
  grep -q "TABLE DATA public ${table}" <<<"$TOC" || die "the dump has no data section for '${table}'"
done
log "archive OK ($(du -h "$DUMP" | cut -f1)), $(grep -c 'TABLE DATA' <<<"$TOC") tables with data"

if [ "$LOCAL_ONLY" = 1 ]; then
  log "--local: stopping before upload. $DUMP"
  log "NOTE: a dump that has not left this machine is not a backup."
  exit 0
fi

# -- 4. Off the machine ------------------------------------------------------
[ -f "$BACKUP_ENV" ] || die "missing $BACKUP_ENV — see .env.backup.example. A dump that stays on this disk is not a backup."

# shellcheck disable=SC1090
set -a; . "$BACKUP_ENV"; set +a

RCLONE="${RCLONE:-$HOME/.local/bin/rclone}"
[ -x "$RCLONE" ] || die "rclone not found at $RCLONE"
REMOTE="${BACKUP_REMOTE:?BACKUP_REMOTE is not set in $BACKUP_ENV}"

# The whole remote is configured by RCLONE_CONFIG_* variables out of .env.backup, so
# there is no rclone.conf and rclone says so on every call. Silenced rather than
# tolerated: a timer whose journal is mostly noise is a journal nobody reads. An
# explicit RCLONE_CONFIG still wins, for anyone who would rather use a real file.
export RCLONE_CONFIG="${RCLONE_CONFIG:-/dev/null}"

log "uploading to ${REMOTE}/daily/"
"$RCLONE" copy "$DUMP" "${REMOTE}/daily/" || die "upload failed"
"$RCLONE" copy "$MANIFEST" "${REMOTE}/daily/" || die "manifest upload failed"

# Prove it arrived, rather than trusting the exit status. rclone copy is quiet about
# a destination that silently accepted nothing.
REMOTE_SIZE="$("$RCLONE" size --json "${REMOTE}/daily/$(basename "$DUMP")" 2>/dev/null |
               python3 -c 'import json,sys; print(json.load(sys.stdin)["bytes"])' 2>/dev/null || echo 0)"
LOCAL_SIZE="$(stat -c%s "$DUMP")"
[ "$REMOTE_SIZE" = "$LOCAL_SIZE" ] ||
  die "uploaded size ${REMOTE_SIZE} does not match local ${LOCAL_SIZE}"
log "confirmed ${REMOTE_SIZE} bytes at the remote"

# -- 5. Prune -----------------------------------------------------------------
log "pruning dumps older than ${RETENTION_DAYS}d"
"$RCLONE" delete --min-age "${RETENTION_DAYS}d" "${REMOTE}/daily/" || die "prune failed"

# The local staging copy is a convenience for the restore drill, not the backup.
# Keep a week so a drill does not have to download, and no more.
find "$STAGING" -name 'savethedate-*' -mtime +7 -delete

log "done. Remote now holds $("$RCLONE" lsf "${REMOTE}/daily/" | grep -c '\.dump$') dumps."
