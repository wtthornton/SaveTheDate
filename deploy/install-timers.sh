#!/usr/bin/env bash
#
# Install the backup, restore-drill and deploy timers as systemd USER units.
# TAP-7733, TAP-12474.
#
#   deploy/install-timers.sh
#
# Idempotent: re-run it after editing any unit file in this directory, AND after any
# change to scripts/deploy_release.py. The deploy unit runs an installed copy of that
# script, outside the checkout it moves, so a merged change to it reaches production
# only through this script.
#
# These are user units rather than system units, to match cloudflared-tapphouse.
# That makes `loginctl enable-linger` load-bearing — without it they stop when the
# last session ends, and on a box nobody logs into they would run once and never
# again. This script checks for it rather than assuming.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
UNITS=(
  savethedate-backup.service
  savethedate-backup.timer
  savethedate-restore-drill.service
  savethedate-restore-drill.timer
  savethedate-deploy.service
  savethedate-deploy.timer
)
LIBEXEC_DIR="${HOME}/.local/libexec/savethedate"
ALLOWED_SIGNERS="${HOME}/.config/savethedate/allowed_signers"

if ! loginctl show-user "$USER" -p Linger --value 2>/dev/null | grep -q '^yes$'; then
  echo "Lingering is OFF for $USER." >&2
  echo "These timers would stop the moment you log out." >&2
  echo "Enable it:  sudo loginctl enable-linger $USER" >&2
  exit 1
fi

mkdir -p "$UNIT_DIR"
for unit in "${UNITS[@]}"; do
  install -m 644 "${ROOT}/deploy/${unit}" "${UNIT_DIR}/${unit}"
  echo "installed ${unit}"
done

mkdir -p "$LIBEXEC_DIR"
install -m 755 "${ROOT}/scripts/deploy_release.py" "${LIBEXEC_DIR}/deploy_release.py"
echo "installed ${LIBEXEC_DIR}/deploy_release.py"

systemctl --user daemon-reload
systemctl --user enable --now savethedate-backup.timer savethedate-restore-drill.timer

# The deploy timer verifies every tag against this file. Without it every tick would
# be refused, so it is not switched on until the file exists. List only the keys
# whose signature is a production approval: Bill's.
if [ -s "$ALLOWED_SIGNERS" ]; then
  systemctl --user enable --now savethedate-deploy.timer
else
  echo
  echo "NOT enabling savethedate-deploy.timer: ${ALLOWED_SIGNERS} is missing or empty." >&2
  echo "Create it with one line per approver, mode 600:" >&2
  echo "  <email> namespaces=\"git\" ssh-ed25519 AAAA..." >&2
  echo "then run this script again." >&2
fi

echo
systemctl --user list-timers --all 'savethedate-*' --no-pager
echo
echo "Run one now, without waiting:"
echo "  systemctl --user start savethedate-backup.service"
echo "  journalctl --user -u savethedate-backup.service -n 50 --no-pager"
echo "  journalctl --user -u savethedate-deploy.service -n 50 --no-pager"
