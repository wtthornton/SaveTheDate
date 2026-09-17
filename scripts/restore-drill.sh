#!/usr/bin/env bash
#
# Restore the latest off-machine backup into a scratch database and count the
# guests. TAP-7733 item 5, and the part that is actually the deliverable.
#
#   scripts/restore-drill.sh                 pull the newest dump from the remote
#   scripts/restore-drill.sh --file FILE     drill a specific local dump
#
# A backup nobody has restored is a hypothesis. This is what converts it into a
# fact, so it is meant to be run periodically and not only once — a schema change,
# an expired R2 token or a silently empty bucket all present as a green backup timer
# and a drill that fails.
#
# By default it downloads from the REMOTE rather than reading `.backups/`. Drilling
# the local staging copy would prove the dump is readable and prove nothing about
# whether anything ever left this machine, which is the failure that matters.
#
# SAFETY: this only ever creates and drops a database named `restore_drill_*`. It
# refuses to touch `savethedate`. The production database is read by `pg_dump` in
# `scripts/backup.sh` and is never written by this script.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT}/docker-compose.prod.yml"
ENV_FILE="${ROOT}/.env.prod"
BACKUP_ENV="${ROOT}/.env.backup"
WORK="$(mktemp -d)"
SCRATCH="restore_drill_$(date -u +%Y%m%d%H%M%S)"

log()  { echo "[drill $(date -u +%H:%M:%SZ)] $*"; }
die()  { echo "[drill] FAILED: $*" >&2; exit 1; }

case "$SCRATCH" in
  savethedate|postgres|template*) die "refusing to use '$SCRATCH' as a scratch name" ;;
esac

cleanup() {
  rm -rf "$WORK"
  # Dropping the scratch database is cleanup, not part of the result — if the drill
  # already failed, its exit status must survive this.
  dc exec -T db psql -U savethedate -d postgres \
    -c "drop database if exists ${SCRATCH};" >/dev/null 2>&1 || true
}
trap cleanup EXIT

[ -f "$ENV_FILE" ] || die "missing $ENV_FILE"
dc() { docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"; }

# -- 1. Get a dump and its manifest ------------------------------------------
DUMP=""
MANIFEST=""

if [ "${1:-}" = "--file" ]; then
  DUMP="${2:?--file needs a path}"
  [ -f "$DUMP" ] || die "no such file: $DUMP"
  MANIFEST="${DUMP%.dump}.manifest.json"
  log "drilling the LOCAL file $DUMP (this proves nothing about the remote)"
else
  [ -f "$BACKUP_ENV" ] || die "missing $BACKUP_ENV — cannot reach the remote"
  # shellcheck disable=SC1090
  set -a; . "$BACKUP_ENV"; set +a
  RCLONE="${RCLONE:-$HOME/.local/bin/rclone}"
  [ -x "$RCLONE" ] || die "rclone not found at $RCLONE"
  REMOTE="${BACKUP_REMOTE:?BACKUP_REMOTE is not set in $BACKUP_ENV}"
  # See the same line in backup.sh: the remote comes entirely from RCLONE_CONFIG_*
  # environment variables, so there is no config file to warn about.
  export RCLONE_CONFIG="${RCLONE_CONFIG:-/dev/null}"

  log "finding the newest dump in ${REMOTE}/daily/"
  NEWEST="$("$RCLONE" lsf "${REMOTE}/daily/" --include '*.dump' | sort | tail -1)"
  [ -n "$NEWEST" ] || die "the remote holds no dumps at all — the backup has never run, or is writing somewhere else"

  log "downloading $NEWEST"
  "$RCLONE" copy "${REMOTE}/daily/${NEWEST}" "$WORK/" || die "download failed"
  "$RCLONE" copy "${REMOTE}/daily/${NEWEST%.dump}.manifest.json" "$WORK/" ||
    die "the dump has no manifest beside it"
  DUMP="${WORK}/${NEWEST}"
  MANIFEST="${WORK}/${NEWEST%.dump}.manifest.json"

  # An old dump restoring perfectly is still a broken backup. Fail loudly rather
  # than reporting a green drill on a timer that stopped a month ago.
  AGE_DAYS="$(( ( $(date -u +%s) - $(date -u -d "$(sed -E 's/savethedate-([0-9]{8})T([0-9]{2})([0-9]{2})([0-9]{2})Z\.dump/\1 \2:\3:\4/' <<<"$NEWEST")" +%s) ) / 86400 ))"
  log "newest dump is ${AGE_DAYS} day(s) old"
  [ "$AGE_DAYS" -le 2 ] || die "the newest backup is ${AGE_DAYS} days old — the timer is not running"
fi

[ -f "$MANIFEST" ] || die "no manifest for this dump; cannot check the data arrived"

# -- 2. Restore into a scratch database ---------------------------------------
log "creating scratch database ${SCRATCH}"
dc exec -T db psql -U savethedate -d postgres -c "create database ${SCRATCH};" >/dev/null ||
  die "could not create the scratch database"

log "restoring"
# --exit-on-error: a restore that logs errors and carries on is how a partial
# restore gets mistaken for a successful one.
dc exec -T db pg_restore -U savethedate -d "${SCRATCH}" --no-owner --no-acl --exit-on-error \
  <"$DUMP" || die "pg_restore refused the archive"

# -- 3. Count what came back, and compare -------------------------------------
log "counting rows in the restored database"
RESTORED="$(dc exec -T db psql -U savethedate -d "${SCRATCH}" -At -F$'\t' -c "
  select 'events', count(*) from events
  union all select 'guests', count(*) from guests
  union all select 'rsvps', count(*) from rsvps
  union all select 'attendees', count(*) from attendees
  union all select 'hosts', count(*) from hosts
  order by 1;
")" || die "the restored database does not answer a query"

python3 - "$MANIFEST" "$RESTORED" <<'PY' || exit 1
import json, sys

manifest = json.load(open(sys.argv[1]))
expected = manifest["row_counts"]
restored = {}
for line in sys.argv[2].splitlines():
    if line.strip():
        table, n = line.split("\t")
        restored[table] = int(n)

print()
print(f"  Backup taken at {manifest['taken_at']}")
print(f"  {'table':<12} {'in backup':>10} {'restored':>10}")
failures = []
for table in sorted(expected):
    want, got = expected[table], restored.get(table, 0)
    mark = "ok" if got >= want else "SHORT"
    if got < want:
        failures.append(f"{table}: expected {want}, restored {got}")
    print(f"  {table:<12} {want:>10} {got:>10}  {mark}")
print()

# The headline number. "Count the guests" is the check the plan actually asks for.
guests = restored.get("guests", 0)
print(f"  GUESTS RESTORED: {guests}")

# A restore that produces an empty guest list from a backup that HELD guests is the
# failure this whole exercise exists to catch, and the shortfall check above catches
# it: the manifest is read from the live database moments before the dump, so a
# backup of 40 guests restoring 0 is a SHORT and a failure.
#
# An empty database that backs up empty is a different thing entirely. Before the
# real guest list is loaded that is simply the truth, and failing on it every week
# would teach whoever reads these to ignore them — which is how a real failure gets
# missed later. So it warns loudly and passes, and becomes meaningful by itself the
# day guests exist.
if guests == 0 and expected.get("guests", 0) == 0:
    print("  NOTE: production holds no guests yet, so this drill proved the backup")
    print("        round-trips but proved nothing about guest data. It becomes a")
    print("        real check the moment the guest list is loaded.")

if failures:
    print("\n[drill] FAILED:", file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    sys.exit(1)
PY

log "the backup restores, and the data is in it."
