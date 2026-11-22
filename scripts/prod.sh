#!/usr/bin/env bash
#
# The production stack on this box. TAP-7733.
#
#   scripts/prod.sh deploy   pull the pinned image, migrate, and bring the app up
#                            (the deploy unit runs it)
#   scripts/prod.sh up       start what is already pinned
#   scripts/prod.sh down     stop the stack, KEEPING the database volume
#   scripts/prod.sh status   what is running, and whether the app answers
#   scripts/prod.sh logs     follow the app's logs
#   scripts/prod.sh psql     a psql shell on the production database
#   scripts/prod.sh compose  raw `docker compose` with the right file and env-file
#   scripts/prod.sh db-exec  run a command inside the running database container
#
# This exists so that `--env-file .env.prod` is never forgotten. Compose does not
# error on an unset variable, it substitutes nothing — so running the compose file
# without it would start Postgres with a blank password rather than fail.
#
# It runs from the DEPLOY DIRECTORY, `~/srv/savethedate` on it13, and reads three
# files beside itself there: `docker-compose.prod.yml`, `.env.prod` (secrets) and
# `image.env` (the image this release runs). It builds nothing and needs no source
# tree; nothing here reads `~/code` (TAP-10322). The one exception is production that
# has not been moved yet, which is still built where it stands: see `_require_env`.
# `scripts/backup.sh` and
# `scripts/restore-drill.sh` reach the database through `prod.sh db-exec`, which
# needs no image pin, so a bad `image.env` cannot stop a backup.
#
# THIS IS THE GUEST LIST. `docker-compose.prod.yml` holds the only copy on this
# machine, in the volume `savethedate-prod_pgdata`. Nothing here removes a volume,
# and `down` deliberately offers no way to.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT}/docker-compose.prod.yml"
ENV_FILE="${ROOT}/.env.prod"
IMAGE_FILE="${ROOT}/image.env"
APP_URL_DEFAULT_PORT=8100

# repository:tag@sha256:<64 hex>. The tag says which release it is, for a person; the
# digest is what Docker actually pulls, so a tag that is moved later changes nothing.
IMAGE_PIN='^SAVETHEDATE_IMAGE=[^[:space:]@]+:[^[:space:]@:/]+@sha256:[0-9a-f]{64}$'

# What production ran before TAP-10322: built in the checkout, under this local name.
LOCAL_BUILD="savethedate-prod:latest"
# 1 once `_require_env` has found production still running that local build.
BUILT_HERE=0
IMAGE_CHECKED=0

_project() {
  # The Compose project's name, from the one place it is written.
  local project
  project="$(sed -n 's/^name:[[:space:]]*//p' "$COMPOSE_FILE")"
  if [ -z "$project" ]; then
    echo "Could not read the project name from $COMPOSE_FILE" >&2
    exit 1
  fi
  echo "$project"
}

_app_container_image() {
  # What the production app container was created from, stopped or running. Asked
  # by project name, with no compose file, so it can be answered with no pin. From
  # /, so Compose does not read the development `docker-compose.yml` beside this.
  local project
  project="$(_project)"
  (cd / && docker compose -p "$project" ps -a --format '{{.Image}}' app 2>/dev/null) || true
}

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
  # The compose file's `:?` catches a missing pin. It cannot tell `savethedate:v3`
  # from a pinned reference, and a bare tag is exactly the unpinned image this file
  # exists to rule out, so the shape is checked here.
  if [ "$IMAGE_CHECKED" = 1 ]; then
    return
  fi
  if [ ! -f "$IMAGE_FILE" ] && [ "$(_app_container_image)" = "$LOCAL_BUILD" ]; then
    # PRODUCTION HAS NOT BEEN MOVED YET. Until Bill moves it, it13 runs the deploy
    # script that was installed before TAP-10322. That copy checks a signed tag out
    # in the old checkout, where there is no `image.env`, and runs this file. If this
    # refused, it would roll the release back and record the tag as failed, for a
    # release with nothing wrong with it. So this does what it did before the move:
    # build here and run that.
    #
    # The test is what the app container was created from, not where this file is.
    # The local build is what production ran before the move and never after it:
    # the move starts the app from a pinned image, and from then on a missing
    # `image.env` is refused below like any other. An `image.env` that exists is
    # always held to the pin, whatever is running.
    BUILT_HERE=1
    IMAGE_CHECKED=1
    echo "NOT PINNED: there is no $IMAGE_FILE, and production is still running the image" >&2
    echo "built in this checkout ($LOCAL_BUILD). Carrying on the old way, building here." >&2
    echo "Move production to its deploy directory: docs/RUNBOOK.md, 'Moving production out of \`~/code\`'." >&2
    return
  fi
  if [ ! -f "$IMAGE_FILE" ]; then
    echo "Missing $IMAGE_FILE" >&2
    echo "It names the image production runs, as one line:" >&2
    echo "  SAVETHEDATE_IMAGE=<repository>:v<N>@sha256:<digest>" >&2
    echo "scripts/build-image.sh prints that line when it pushes a release image, and" >&2
    echo "the deploy unit rewrites it for each release. See docs/RUNBOOK.md." >&2
    exit 1
  fi
  # EXACTLY ONE assignment, and that one is the pin. Compose takes the LAST value
  # it reads for a variable, so a good pin followed by `SAVETHEDATE_IMAGE=repo:v7`
  # runs the bare tag; "some line is a pin" is not the check. Nothing else belongs
  # in this file either: the secrets are in .env.prod, and a second variable here
  # would be one this check never looked at. Comments and blank lines are fine.
  # `scripts/deploy_release.py` reads the file by the same rule.
  local assignments
  assignments="$(grep -vE '^[[:space:]]*(#|$)' "$IMAGE_FILE" || true)"
  if [ "$(grep -c . <<<"$assignments")" != 1 ] || ! grep -qE "$IMAGE_PIN" <<<"$assignments"; then
    echo "$IMAGE_FILE must hold exactly one line, pinned by tag and digest:" >&2
    echo "  SAVETHEDATE_IMAGE=<repository>:v<N>@sha256:<64 hex digits>" >&2
    echo "It holds:" >&2
    sed 's/^/  /' <<<"${assignments:-(nothing)}" >&2
    exit 1
  fi
  IMAGE_CHECKED=1
}

dc() {
  _require_env
  if [ "$BUILT_HERE" = 1 ]; then
    SAVETHEDATE_IMAGE="$LOCAL_BUILD" docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
  else
    docker compose --env-file "$ENV_FILE" --env-file "$IMAGE_FILE" -f "$COMPOSE_FILE" "$@"
  fi
}

_obtain_image() {
  # The image is PULLED, never built here (TAP-10322). `image.env` pins it by digest,
  # so this fetches exactly the bytes that were built on nlt-dev, and is a no-op when
  # they are already on this box. Only `migrate` and `app` are named: a bare `pull`
  # would also move Postgres to whatever `postgres:17-alpine` means today.
  #
  # Production that has not been moved yet is the exception, and it is built from
  # this checkout exactly as `docker compose build` built it before.
  _require_env
  if [ "$BUILT_HERE" = 1 ]; then
    docker build --tag "$LOCAL_BUILD" "$ROOT"
  else
    dc pull migrate app
  fi
}

db_exec() {
  # A command inside the database container that is ALREADY RUNNING, and nothing
  # else: `exec`, on `db`. This is what the backup and the restore drill use.
  #
  # It deliberately does not go through `dc`. `dc` reads the compose file, and the
  # compose file cannot be read without a valid `image.env`. Neither the backup nor
  # the drill starts a container, so neither has any use for the application image,
  # and a missing or half-written pin must not cost a night's backup of the guest
  # list. Compose finds a running project by its name alone, so this gives it no
  # compose file and no env file: there is no pin for it to read.
  #
  # That is also why skipping the pin check here weakens nothing. `exec` cannot
  # create, start or recreate a container, so no image can be run through this.
  if [ ! -f "$ENV_FILE" ]; then
    echo "Missing $ENV_FILE — this is not a configured production deploy directory." >&2
    exit 1
  fi
  local project
  project="$(_project)"
  # From /, so Compose does not pick up the development `docker-compose.yml` that
  # sits in this directory and read it under the production project's name.
  cd /
  docker compose -p "$project" exec -T db "$@"
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
  # The release order the plan asks for: get the image, run the migration to
  # completion, and only then start the app.
  #
  # The migration runs on its own first, and `up` comes only after it has passed.
  # `up` alone looks equivalent because the app waits on
  # `service_completed_successfully`, but it is not. Compose RECREATES every
  # container before it starts any of them, so when the migration fails the old app
  # has already been removed and the new one is never started. That was a failed
  # migration taking production down (TAP-12474, reproduced with Compose v5.8.0).
  # `run --rm migrate` touches no other container, so a failure here leaves the
  # previous app serving. Alembic runs each Postgres migration in a transaction, so
  # the schema is unchanged too. `up` then runs migrate once more, as a no-op.
  #
  # `scripts/deploy_release.py` runs this from a signed tag; nobody should need to.
  _obtain_image
  dc run --rm migrate
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
  db-exec) shift; db_exec "$@" ;;
  *)
    sed -n '3,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
    ;;
esac
