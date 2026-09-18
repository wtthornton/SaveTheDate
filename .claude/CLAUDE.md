# SaveTheDate

FastAPI + PostgreSQL. Server-rendered Jinja2 + htmx 2.x + Tailwind. No Node toolchain.

## Commands
- Postgres: `docker compose up -d db`  (host port 5434, not 5432)
- Test DB, once: `docker compose exec db createdb -U savethedate savethedate_test`
- Gate: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && \
         .venv/bin/mypy app migrations scripts tests && .venv/bin/pytest -q`
- Migrations: `.venv/bin/alembic upgrade head` / `downgrade base`
  **`downgrade base` drops every table in whatever `DATABASE_URL` points at, which
  defaults to the DEV database.** Run the up→down→up round trip against the test DB:
  `DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic …`, or it will wipe local data
  and re-key every invite token behind a running review instance.
- Review instance: `scripts/review-instance.sh up | reload | status | down` (TAP-7738)
  **`reload` after any change under `app/*.py`.** Jinja re-reads templates each
  request but imports Python once, so a long-lived instance keeps serving the old
  module — that shipped a live 500 once while every test was green. `reload`
  restarts on the same port, so the URL and every invite token survive; `up` does
  not.
- Visual gate: `tests/test_visual.py` drives Chromium at 390px and 1440px and writes
  screenshots to `tests/screenshots/`. Look at them. No non-visual test noticed that
  the hero was an empty grey box, that a whole breakpoint was missing, or that every
  save-the-date screenshot was taken with half the card still inside the envelope, or
  that the envelope's doors swung off the side of a phone so the liner was never once
  seen at 390px.
- Seed fake review data: `.venv/bin/python -m scripts.seed_review_data`
- Production: `scripts/prod.sh deploy | up | down | status | logs | psql` (TAP-7733)
  A **separate** Compose project (`savethedate-prod`), its own volume, its own
  database, its own port (127.0.0.1:8100). Always go through this script — it supplies
  `--env-file .env.prod`, and Compose substitutes *nothing* for an unset variable
  rather than failing, so a forgotten flag starts Postgres with a blank password.
  Migrations run as a one-shot `migrate` service the app waits on, never on app boot.
- Backups: `scripts/backup.sh` nightly, `scripts/restore-drill.sh` weekly, both as
  systemd **user** timers (`deploy/install-timers.sh`). The drill restores the newest
  off-machine dump into a scratch database and compares row counts against a manifest
  taken at dump time. **A backup nobody has restored is a hypothesis** — if you change
  the schema, the drill is what tells you the old dumps still load.
- CSS: `~/.local/bin/tailwindcss -i app/static/src/app.css -o app/static/app.css`
  Tailwind v4 standalone binary, no Node, no `package.json`. The built
  `app/static/app.css` is **committed** so a deploy never has to run the build, and
  the 110MB binary never has to exist on the server. Rebuild and commit it whenever
  the source changes — `tests/test_stylesheet.py` fails if you forget.

## Invariants — do not break these
- `guests.invite_token` is in people's inboxes once sent. NEVER re-key or re-issue a
  guests row. All change is absorbed by tables hanging off it.
- Guest routes stay anonymous. The link IS the credential. Never add a guest login.
- Accessibility is a requirement, not a nicety: 18px minimum body text, 44px touch
  targets, real `<input>`/`<label>`, no `role=` on divs. The guest list skews old.
- htmx is pinned to 2.x, vendored at `app/static/vendor/htmx-2.0.10.min.js`. htmx 4
  made attribute inheritance explicit and fails SILENTLY. npm `latest` is still 2.x;
  `next` is 4.0.0. A test asserts the loaded file reports `version:"2.0.10"`.
- `/invites/{token}` serves the guest HTML. The JSON view is `/api/invites/{token}`.
  The token itself never changes — only what the URL renders.
- **Two pages need no token, and both live at `/`.** The hostname decides: the
  save-the-date card on a hostname in `SAVE_THE_DATE_HOSTS`, the wedding welcome on
  every other. An explicit list, never a substring test — this project is itself called
  savethedate. There is **no `/save-the-date` path**; local review uses
  `savethedate.localhost`, which browsers resolve to loopback. Neither page reads a
  guest row, neither takes input, and neither may ever grow a name-lookup box — on an
  anonymous page that is a guest-list oracle.
- **Reference static files with `static_url('app.css')`, never `/static/app.css`.**
  Cloudflare serves `/static/*` with `max-age=14400` and caches it at its edge, so a
  fixed URL keeps handing returning browsers a stale stylesheet for four hours after a
  rebuild. It presents as "the design is broken", not as a cache. A test fails on any
  template that hard-codes the path.
- **Anything that animates to `opacity: 0` and stays in the layout needs
  `pointer-events: none`.** Invisible is not intangible: it still gets hit-tested, and
  it will quietly eat taps meant for whatever is underneath it.
- **`z-index` only means anything inside a stacking context, and `transform`, `opacity`,
  `filter`, `transform-style: preserve-3d` and `isolation` all create one.**
  `preserve-3d` is the dangerous one: it ALSO sorts its children by their position in
  space rather than by `z-index`, so a sibling's `z-index: 2` beat the envelope's whole
  3D subtree and both doors were simply not drawn. When paint order is wrong, find the
  nearest ancestor that creates a context before adjusting numbers.
- **Hit-testing skips `pointer-events: none`, `elementsFromPoint` included.** A test that
  probes a decorative overlay has to lift it for the measurement and put it back, or it
  reports a bare card on a sealed envelope. If a test contradicts the screenshot,
  suspect the instrument.
- No per-plate meal choice. Dietary tags only. Headcounts are per DAY, not per plate.
- Generated Alembic migrations need `ruff check --fix` AND `ruff format`, or CI fails.
  `ruff format` alone does NOT fix the UP007/UP035 errors Alembic generates.
- American English. This is a Texas wedding.

- **Production settings live in `docker-compose.prod.yml`, not in a `.env`.** Every
  non-secret one — `SAVE_THE_DATE_HOSTS`, `TRUSTED_CLIENT_IP_HEADER`,
  `PUBLIC_BASE_URL`, `REVIEW_INSTANCE`, `EMAIL_PROVIDER` — is committed there so it
  can be reviewed and asserted; `tests/test_production_stack.py` fails if any drifts.
  Only secrets go in the gitignored `.env.prod` / `.env.backup`.
- **The production app binds `127.0.0.1` only.** That is what makes trusting
  `CF-Connecting-IP` and `X-Forwarded-Proto` defensible — nothing off this box can
  open the socket to forge either. Binding it wider silently turns the rate limiter
  off, because a header a caller can set is an unlimited supply of fresh buckets.

## Never
- Swallow exceptions, skip tests, or add `# noqa` / `# type: ignore` to get green.
- Put real guest data on an unauthenticated instance.
- Run `docker compose ... down -v` on the production stack. `-v` removes the volume,
  and that volume is the guest list. `scripts/prod.sh down` offers no way to pass it.
- Point `wedding.tapphouse.co` at the review instance to make it look finished. While
  production is down it returns 502 **on purpose**: a guest-facing hostname quietly
  serving the development database is how invented guests start looking real.
