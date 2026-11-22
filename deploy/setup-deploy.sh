#!/usr/bin/env bash
#
# One-time setup of the release-tag deploy on it13, as one command. TAP-12474.
#
#   cd ~/srv/savethedate && git pull --ff-only && \
#     deploy/setup-deploy.sh ~/.config/websitedup/allowed_signers tapp.thornton@gmail.com
#
# 1. Copies the allowed_signers line(s) for PRINCIPAL, and only those, from
#    SIGNERS_FILE into ~/.config/savethedate/allowed_signers, at mode 600. It never
#    writes a key it did not find, and refuses when the principal has no line.
# 2. Runs deploy/install-timers.sh, which installs the backup, drill and deploy
#    units, copies scripts/deploy_release.py out of the checkout, and enables the
#    deploy timer now that allowed_signers exists.
#
# Idempotent: run it again and an unchanged signers file is left alone. It refuses
# rather than drop a different approver already in the file.
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

if [ -z "$principal" ]; then
  echo "REFUSED: the principal is empty." >&2
  exit 1
fi
if [ ! -f "$source_file" ] || [ ! -r "$source_file" ]; then
  echo "REFUSED: $source_file is not a readable file." >&2
  exit 1
fi
if [ -e "$TARGET" ] && [ ! -f "$TARGET" ]; then
  echo "REFUSED: $TARGET exists and is not a file." >&2
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
umask 077
scratch="$(mktemp -d)"
trap 'rm -rf -- "$scratch"' EXIT

# Every copied line must carry a public key that ssh-keygen can read. A line with
# no key, or a truncated one, is refused rather than installed.
while IFS= read -r line; do
  printf '%s\n' "$line" |
    awk '{ for (i = 2; i < NF; i++) if ($i ~ /^(ssh-|ecdsa-|sk-)/) { print $i, $(i + 1); exit } }' \
    > "$scratch/key.pub"
  if ! fingerprint="$(ssh-keygen -l -f "$scratch/key.pub" 2>/dev/null)"; then
    echo "REFUSED: a line for '$principal' in $source_file holds no valid SSH public key:" >&2
    echo "  $line" >&2
    exit 1
  fi
  echo "key for $principal: $fingerprint"
done <<< "$lines"

# The file holds only the approver named here. Another principal already in it was
# put there on purpose by someone, so it is not dropped silently.
if [ -f "$TARGET" ]; then
  others="$(awk -v p="$principal" 'NF && $1 !~ /^#/ && $1 != p { print "  " $1 }' "$TARGET")"
  if [ -n "$others" ]; then
    echo "REFUSED: $TARGET also lists other principals, which this would remove:" >&2
    echo "$others" >&2
    echo "Remove them first if that is intended. Nothing was written." >&2
    exit 1
  fi
fi

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

# `exec` replaces this shell, so the EXIT trap never fires; clean up first.
rm -rf -- "$scratch"
trap - EXIT
echo
exec "${ROOT}/deploy/install-timers.sh"
