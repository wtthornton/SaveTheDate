#!/usr/bin/env bash
#
# The production stack on this box. TAP-7733.
#
#   scripts/prod.sh deploy   build, migrate, and bring the app up (the normal path)
#   scripts/prod.sh up       start without rebuilding
#   scripts/prod.sh down     stop the stack, KEEPING the database volume
#   scripts/prod.sh status   what is running, and whether the app answers
#   scripts/prod.sh logs     follow the app's logs
#   scripts/prod.sh psql     a psql shell on the production database
#   scripts/prod.sh compose  raw `docker compose` with the right file and env-file
#
# This exists so that `--env-file .env.prod` is never forgotten. Compose does not
# error on an unset variable, it substitutes nothing — so running the compose file
# without it would start Postgres with a blank password rather than fail.
#
# THIS IS THE GUEST LIST. `docker-compose.prod.yml` holds the only copy on this
# machine, in the volume `savethedate-prod_pgdata`. Nothing here removes a volume,
# and `down` deliberately offers no way to.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT}/docker-compose.prod.yml"
ENV_FILE="${ROOT}/.env.prod"
APP_URL_DEFAULT_PORT=8100

_require_env() {
  if [ ! -f "$ENV_FILE" ]; then
    echo "Missing $ENV_FILE" >&2
    echo "Copy .env.prod.example to .env.prod and fill in POSTGRES_PASSWORD." >&2
    echo "It is gitignored; this repository is public." >&2
    exit 1
  fi
  # Compose's `:?` guards catch an unset variable, but not one set to the empty
  # string, which is exactly what a copied-but-unfilled example file produces.
  if ! grep -qE '^POSTGRES_PASSWORD=.+' "$ENV_FILE"; then
    echo "POSTGRES_PASSWORD is empty in $ENV_FILE" >&2
    echo "Generate one: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"" >&2
    exit 1
  fi
}

dc() {
  _require_env
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

_app_port() {
  # The published port, read back from the running container rather than from the
  # env file, so `status` reports where the app actually is.
  local port
  port="$(dc port app 8000 2>/dev/null | sed 's/.*://' || true)"
  case "$port" in
    ''|*[!0-9]*) grep -E '^APP_PORT=[0-9]+' "$ENV_FILE" 2>/dev/null | cut -d= -f2 ||
                 echo "$APP_URL_DEFAULT_PORT" ;;
    *) echo "$port" ;;
  esac
}

deploy() {
  # The release order the plan asks for: build the image, run the migration to
  # completion, and only then start the app. `up` honours the
  # `service_completed_successfully` dependency, so a migration that fails stops
  # the release rather than leaving an app against a half-migrated schema.
  dc build
  dc up -d
  _wait_healthy
}

up() {
  dc up -d
  _wait_healthy
}

_wait_healthy() {
  local port i=0
  port="$(_app_port)"
  until curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1 || [ "$i" -ge 60 ]; do
    i=$((i + 1)); sleep 1
  done
  if ! curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
    echo "The app did not answer /health on 127.0.0.1:${port}." >&2
    echo "Recent logs:" >&2
    dc logs --tail 40 app migrate >&2 || true
    exit 1
  fi
  echo "Production is up on http://127.0.0.1:${port} (tunnel serves it as https://wedding.tapphouse.co)."
}

down() {
  # No `-v`, no `--volumes`, and no way to pass one through. Removing the volume
  # removes the guest list, and there is no path in this project where that is the
  # thing somebody meant to type.
  dc down
  echo "Stopped. The database volume savethedate-prod_pgdata is untouched."
}

status() {
  dc ps
  local port
  port="$(_app_port)"
  if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
    echo "health: ok on 127.0.0.1:${port}"
  else
    echo "health: NOT answering on 127.0.0.1:${port}"
  fi

  # "Is it up?" and "can we get it back?" are the two questions, and only one of
  # them used to be answerable here. Until TAP-7734 puts alerting on this, a failed
  # drill is silent unless something looks — so this looks.
  echo
  echo "backups:"
  systemctl --user list-timers --all 'savethedate-*' --no-pager 2>/dev/null |
    sed -n '1,4p' || echo "  no timers installed (deploy/install-timers.sh)"
  local failed
  failed="$(systemctl --user --failed --no-legend 'savethedate-*' 2>/dev/null || true)"
  if [ -n "$failed" ]; then
    echo "  FAILED UNITS — the guest list may not be recoverable:"
    echo "$failed" | sed 's/^/    /'
  fi
}

case "${1:-}" in
  deploy) deploy ;;
  up) up ;;
  down) down ;;
  status) status ;;
  logs) shift; dc logs -f --tail 100 "${@:-app}" ;;
  psql) dc exec db psql -U savethedate -d savethedate ;;
  compose) shift; dc "$@" ;;
  *)
    sed -n '3,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
    ;;
esac
