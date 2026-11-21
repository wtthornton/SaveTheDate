#!/usr/bin/env bash
#
# Install the backup, restore-drill and deploy timers as systemd USER units.
# TAP-7733, TAP-12474, TAP-12499.
#
#   deploy/install-timers.sh
#
# Idempotent. Bill runs it once, through deploy/setup-deploy.sh. After that the deploy
# runs it itself: scripts/deploy_release.py runs the checkout's copy of this script
# after every settled deploy or rollback, so the installed copy of
# scripts/deploy_release.py and these units always come from the commit that is live.
#
# That means this script runs INSIDE savethedate-deploy.service, and replaces the
# file that service is running. So:
# - Every file is copied to a temporary name in its destination directory and
#   renamed over the old one. A rename is atomic: nothing ever sees a missing or
#   half-written script or unit. The running process keeps the old file it opened.
#   A temporary file that never made it into place is removed on exit.
# - A deploy_release.py that does not compile is refused, and the installed copy,
#   the verifier every later tick runs, is kept. The script goes in before the units,
#   so a refusal leaves both as they were.
# - `daemon-reload` and `enable --now` from inside the running unit do not stop it.
#   systemd.unit(5): on a reload "all configuration settings are flushed out and
#   replaced with the new configuration (which however might not be in effect
#   immediately), however all runtime state is saved/restored". Starting a timer that
#   is already active is a no-op. Checked on systemd 259 with a oneshot service, run by
#   its own timer, that rewrote its unit, reloaded and ran `enable --now` on its timer:
#   it ran to completion, once, and the timer's next run stayed due relative to the
#   end of that run (TAP-12499).
# - `$USER` and the user bus are there: systemd sets $USER for every service, and
#   $XDG_RUNTIME_DIR for every service of the user manager (systemd.exec(5)).
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

# The temporary file in flight. Any exit that did not rename it into place, a failed
# `cp`, check or `mv` under `set -e` included, removes it rather than leave a dot-file.
tmp=""
trap 'if [ -n "$tmp" ]; then rm -f -- "$tmp"; fi' EXIT

# install_atomically MODE SRC DEST [CHECK...]: put SRC at DEST by renaming a finished
# copy over it. `install` and `cp` would instead unlink or truncate DEST and then
# write it, and a deploy or a daemon-reload in that window would read a missing or
# partial file. CHECK, when given, runs on the finished copy first, with its path as
# the last argument; if it fails, DEST is left exactly as it was.
install_atomically() {
  local mode="$1" src="$2" dest="$3"
  shift 3
  tmp="$(mktemp "$(dirname "$dest")/.$(basename "$dest").XXXXXX")"
  cp -- "$src" "$tmp"
  chmod "$mode" "$tmp"
  if [ "$#" -gt 0 ] && ! "$@" "$tmp"; then
    return 1
  fi
  mv -f -- "$tmp" "$dest"
  tmp=""
}

# The installed deploy script is the verifier every later tick runs. One that does not
# compile would fail every tick from then on, and only a login could replace it, so
# it is refused here and the working copy stays. Compiled by the interpreter the unit
# runs it with, in memory: `python3 -m py_compile` would leave a __pycache__ behind.
compiles() {
  if ! /usr/bin/python3 -c \
    'import sys; compile(open(sys.argv[1], "rb").read(), sys.argv[1], "exec")' "$1"; then
    echo "REFUSED: scripts/deploy_release.py does not compile." >&2
    echo "The installed ${LIBEXEC_DIR}/deploy_release.py is kept as it was." >&2
    return 1
  fi
}

# The script first: if it is refused, the units are not moved ahead of it either.
mkdir -p "$LIBEXEC_DIR"
install_atomically 755 "${ROOT}/scripts/deploy_release.py" "${LIBEXEC_DIR}/deploy_release.py" \
  compiles
echo "installed ${LIBEXEC_DIR}/deploy_release.py"

mkdir -p "$UNIT_DIR"
for unit in "${UNITS[@]}"; do
  install_atomically 644 "${ROOT}/deploy/${unit}" "${UNIT_DIR}/${unit}"
  echo "installed ${unit}"
done

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
  echo "Create it from an existing signers file, copying only the approver's line:" >&2
  echo "  deploy/setup-deploy.sh ~/.config/websitedup/allowed_signers <principal>" >&2
fi

echo
systemctl --user list-timers --all 'savethedate-*' --no-pager
echo
echo "Run one now, without waiting:"
echo "  systemctl --user start savethedate-backup.service"
echo "  journalctl --user -u savethedate-backup.service -n 50 --no-pager"
echo "  journalctl --user -u savethedate-deploy.service -n 50 --no-pager"
