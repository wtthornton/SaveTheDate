#!/usr/bin/env bash
#
# One-time setup of the release-tag deploy on it13, as one command. TAP-12474.
#
#   cd ~/code/SaveTheDate && git pull --ff-only && \
#     deploy/setup-deploy.sh ~/.config/websitedup/allowed_signers tapp.thornton@gmail.com
#
# 1. Copies the allowed_signers line(s) for PRINCIPAL, and only those, from
#    SIGNERS_FILE into ~/.config/savethedate/allowed_signers, at mode 600. It never
#    writes a key it did not find, and refuses when the principal has no line.
# 2. Runs deploy/install-timers.sh, which installs the backup, drill and deploy
#    units, copies scripts/deploy_release.py out of the checkout, and enables the
#    deploy timer now that allowed_signers exists.
#
# Idempotent: run it again and an unchanged signers file is left alone.
#
# Only the principal you name is copied. That matters: the source file may also list
# keys whose signatures mean something else, such as WebsiteDup's release bot, and a
# signature from a key in the SaveTheDate file IS a production approval.
#
# The `git pull` is in the command line, not in this script, because on a box that
# has never seen this script it does not exist until the pull has run.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${HOME}/.config/savethedate"
TARGET="${TARGET_DIR}/allowed_signers"

if [ "$#" -ne 2 ]; then
  echo "usage: deploy/setup-deploy.sh SIGNERS_FILE PRINCIPAL" >&2
  echo "  e.g. deploy/setup-deploy.sh ~/.config/websitedup/allowed_signers you@example.com" >&2
  exit 2
fi
source_file="$1"
principal="$2"

if [ ! -r "$source_file" ]; then
  echo "Cannot read $source_file" >&2
  exit 1
fi

# An exact match on the first field, never a substring: `bill@x` must not also pick
# up `bill@x.evil`.
lines="$(awk -v p="$principal" '$1 == p' "$source_file")"
if [ -z "$lines" ]; then
  echo "REFUSED: $source_file has no line for principal '$principal'." >&2
  echo "Nothing was written. The principals it lists:" >&2
  awk 'NF && $1 !~ /^#/ { print "  " $1 }' "$source_file" >&2
  exit 1
fi
if ! printf '%s\n' "$lines" | grep -qE '(^|[[:space:]])(ssh-|ecdsa-|sk-)'; then
  echo "REFUSED: the line for '$principal' in $source_file holds no SSH public key." >&2
  exit 1
fi

umask 077
mkdir -p "$TARGET_DIR"
chmod 700 "$TARGET_DIR"
if [ -f "$TARGET" ] && [ "$(cat "$TARGET")" = "$lines" ]; then
  chmod 600 "$TARGET"
  echo "unchanged: $TARGET already holds exactly the line(s) for $principal"
else
  partial="${TARGET}.partial"
  printf '%s\n' "$lines" > "$partial"
  chmod 600 "$partial"
  mv -f "$partial" "$TARGET"
  echo "wrote $TARGET (mode 600) with $(printf '%s\n' "$lines" | wc -l) line(s) for $principal"
fi

echo
exec "${ROOT}/deploy/install-timers.sh"
