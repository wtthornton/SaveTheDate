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
- Review instance: `scripts/review-instance.sh up | status | down` (TAP-7738)
- Seed fake review data: `.venv/bin/python -m scripts.seed_review_data`
- CSS: `~/.local/bin/tailwindcss -i app/static/src/app.css -o app/static/app.css`
  Tailwind v4 standalone binary, no Node, no `package.json`. The built
  `app/static/app.css` is **committed** so a deploy never has to run the build —
  Render does not run yours. Rebuild and commit it whenever the source changes.

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
- No per-plate meal choice. Dietary tags only. Headcounts are per DAY, not per plate.
- Generated Alembic migrations need `ruff check --fix` AND `ruff format`, or CI fails.
  `ruff format` alone does NOT fix the UP007/UP035 errors Alembic generates.
- American English. This is a Texas wedding.

## Never
- Swallow exceptions, skip tests, or add `# noqa` / `# type: ignore` to get green.
- Put real guest data on an unauthenticated instance.
