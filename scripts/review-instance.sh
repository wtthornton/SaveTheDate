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
  # SAVE_THE_DATE_HOSTS makes dev-savethedate.tapphouse.co serve the card at its root
  # while dev-wedding.tapphouse.co keeps serving the wedding welcome (TAP-7781). Both
  # hostnames reach this one process through the named tunnel; the app tells them
  # apart by the Host header, so there is no second port and no second instance.
  REVIEW_INSTANCE=true SAVE_THE_DATE_HOSTS=dev-savethedate.tapphouse.co \
    nohup "${ROOT}/.venv/bin/uvicorn" app.main:app \
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

reload() {
  # Jinja reloads templates on its own, but NOT Python. A change to app/*.py leaves the
  # running process serving the old code — which showed up once as a live 500 on the
  # published URL while the whole test suite was green, because the tests start a fresh
  # server and the review instance does not.
  #
  # Restarting only the app, on the same port, keeps the tunnel and therefore the URL,
  # and does not re-seed — so every invite link already sent stays valid.
  if ! _pid_alive "$RUN/app.pid"; then
    echo "Nothing is running. Use 'up'." >&2
    exit 1
  fi
  # /proc/<pid>/cmdline is NUL-separated, so NUL is what has to become a newline.
  local port
  port="$(tr '\0' '\n' <"/proc/$(cat "$RUN/app.pid")/cmdline" 2>/dev/null |
          grep -A1 -x -- '--port' | tail -1)"
  case "$port" in
    ''|*[!0-9]*) port="$(ps -o args= -p "$(cat "$RUN/app.pid")" |
                         grep -oE '\-\-port[ =][0-9]+' | grep -oE '[0-9]+' | head -1)" ;;
  esac
  case "$port" in
    ''|*[!0-9]*) port="" ;;
  esac
  if [ -z "$port" ]; then
    echo "Could not work out which port the app is on; use 'down' then 'up'." >&2
    exit 1
  fi

  kill "$(cat "$RUN/app.pid")" 2>/dev/null || true
  sleep 1
  REVIEW_INSTANCE=true SAVE_THE_DATE_HOSTS=dev-savethedate.tapphouse.co \
    nohup "${ROOT}/.venv/bin/uvicorn" app.main:app \
    --host 127.0.0.1 --port "$port" --log-level warning \
    >"$RUN/app.log" 2>&1 &
  echo $! >"$RUN/app.pid"

  local i=0
  until curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1 || [ $i -ge 40 ]; do
    i=$((i + 1)); sleep 0.5
  done
  if ! curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
    echo "The app did not come back. Last lines of $RUN/app.log:" >&2
    tail -20 "$RUN/app.log" >&2
    exit 1
  fi
  echo "Reloaded on port $port. URL and invite links are unchanged:"
  [ -f "$RUN/url" ] && cat "$RUN/url"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  reload) reload ;;
  status) status ;;
  *)
    echo "usage: $0 {up|reload|status|down}" >&2
    echo "  reload  restart the app after a Python change, keeping the URL and tokens" >&2
    echo "  PHASE=before-open|open|closed|real  which RSVP phase to seed (default: open)" >&2
    exit 2
    ;;
esac
