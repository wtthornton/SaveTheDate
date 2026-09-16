#!/usr/bin/env bash
#
# The TAP-7738 review instance: the app on this box, published through a Cloudflare
# Quick Tunnel so reviewers can open it on their own phones.
#
#   scripts/review-instance.sh up      start it, seed invented guests, print the links
#   scripts/review-instance.sh status  is it running, and on what URL
#   scripts/review-instance.sh down    stop everything
#
# A Quick Tunnel URL is random and does NOT survive a restart. Every `up` prints a new
# one, so re-send the links after any restart or reboot.
#
# INVENTED GUESTS ONLY. The host endpoints are unauthenticated (TAP-7725), so anyone
# with this URL can read the whole guest list and its tokens. That is acceptable while
# every guest is fictional and is not acceptable one moment after the real list exists.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="${ROOT}/.review"
CLOUDFLARED="${CLOUDFLARED:-$HOME/.local/bin/cloudflared}"
PHASE="${PHASE:-open}"

mkdir -p "$RUN"

_pid_alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

down() {
  local stopped=0
  for name in tunnel app; do
    if _pid_alive "$RUN/$name.pid"; then
      kill "$(cat "$RUN/$name.pid")" 2>/dev/null || true
      stopped=1
    fi
    rm -f "$RUN/$name.pid"
  done
  rm -f "$RUN/url"
  [ "$stopped" = 1 ] && echo "Review instance stopped." || echo "Nothing was running."
}

status() {
  _pid_alive "$RUN/app.pid"    && echo "app:    running (pid $(cat "$RUN/app.pid"))" || echo "app:    stopped"
  _pid_alive "$RUN/tunnel.pid" && echo "tunnel: running (pid $(cat "$RUN/tunnel.pid"))" || echo "tunnel: stopped"
  # `|| true`: under `set -e` a false final test would make `status` exit non-zero,
  # which reads as "the check failed" rather than "there is no URL yet".
  { [ -f "$RUN/url" ] && echo "url:    $(cat "$RUN/url")"; } || true
}

up() {
  down >/dev/null 2>&1 || true

  if [ ! -x "$CLOUDFLARED" ]; then
    echo "cloudflared not found at $CLOUDFLARED" >&2
    echo "Install: curl -sSL -o \"$CLOUDFLARED\" \\" >&2
    echo "  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod +x \"$CLOUDFLARED\"" >&2
    exit 1
  fi

  local port
  port="$("${ROOT}/.venv/bin/python" -c "
import socket
s = socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1]); s.close()")"

  # REVIEW_INSTANCE puts the draft banner on every page.
  REVIEW_INSTANCE=true nohup "${ROOT}/.venv/bin/uvicorn" app.main:app \
    --host 127.0.0.1 --port "$port" --log-level warning \
    >"$RUN/app.log" 2>&1 &
  echo $! >"$RUN/app.pid"

  local i=0
  until curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1 || [ $i -ge 40 ]; do
    i=$((i + 1)); sleep 0.5
  done
  if ! curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    echo "The app did not come up. Last lines of $RUN/app.log:" >&2
    tail -20 "$RUN/app.log" >&2
    down >/dev/null 2>&1 || true
    exit 1
  fi

  nohup "$CLOUDFLARED" tunnel --no-autoupdate --url "http://127.0.0.1:$port" \
    >"$RUN/tunnel.log" 2>&1 &
  echo $! >"$RUN/tunnel.pid"

  i=0
  until grep -qE 'https://[a-z0-9-]+\.trycloudflare\.com' "$RUN/tunnel.log" 2>/dev/null || [ $i -ge 60 ]; do
    i=$((i + 1)); sleep 1
  done

  local url
  url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$RUN/tunnel.log" | head -1 || true)"
  if [ -z "$url" ]; then
    echo "The tunnel did not report a URL. Last lines of $RUN/tunnel.log:" >&2
    tail -20 "$RUN/tunnel.log" >&2
    down >/dev/null 2>&1 || true
    exit 1
  fi
  echo "$url" >"$RUN/url"

  "${ROOT}/.venv/bin/python" -m scripts.seed_review_data --phase "$PHASE" --base-url "$url"

  echo
  echo "Review instance is up at $url"
  echo "Stop it with: scripts/review-instance.sh down"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  status) status ;;
  *)
    echo "usage: $0 {up|status|down}" >&2
    echo "  PHASE=before-open|open|closed|real  which RSVP phase to seed (default: open)" >&2
    exit 2
    ;;
esac
