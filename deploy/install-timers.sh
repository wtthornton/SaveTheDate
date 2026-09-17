#!/usr/bin/env bash
#
# Install the backup and restore-drill timers as systemd USER units. TAP-7733.
#
#   deploy/install-timers.sh
#
# Idempotent: re-run it after editing any unit file in this directory.
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
)

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

systemctl --user daemon-reload
systemctl --user enable --now savethedate-backup.timer savethedate-restore-drill.timer

echo
systemctl --user list-timers --all 'savethedate-*' --no-pager
echo
echo "Run one now, without waiting:"
echo "  systemctl --user start savethedate-backup.service"
echo "  journalctl --user -u savethedate-backup.service -n 50 --no-pager"
