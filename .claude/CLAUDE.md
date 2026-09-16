# SaveTheDate

FastAPI + PostgreSQL. Server-rendered Jinja2 + htmx 2.x + Tailwind. No Node toolchain.

## Commands
- Postgres: `docker compose up -d db`  (host port 5434, not 5432)
- Test DB, once: `docker compose exec db createdb -U savethedate savethedate_test`
- Gate: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && \
         .venv/bin/mypy app migrations scripts tests && .venv/bin/pytest -q`
- Migrations: `.venv/bin/alembic upgrade head` / `downgrade base`
- Seed fake review data: `.venv/bin/python -m scripts.seed_review_data`

## Invariants — do not break these
- `guests.invite_token` is in people's inboxes once sent. NEVER re-key or re-issue a
  guests row. All change is absorbed by tables hanging off it.
- Guest routes stay anonymous. The link IS the credential. Never add a guest login.
- Accessibility is a requirement, not a nicety: 18px minimum body text, 44px touch
  targets, real `<input>`/`<label>`, no `role=` on divs. The guest list skews old.
- htmx is pinned to 2.x. htmx 4 made attribute inheritance explicit and fails SILENTLY.
- No per-plate meal choice. Dietary tags only. Headcounts are per DAY, not per plate.
- Generated Alembic migrations need `ruff check --fix` AND `ruff format`, or CI fails.
  `ruff format` alone does NOT fix the UP007/UP035 errors Alembic generates.
- American English. This is a Texas wedding.

## Never
- Swallow exceptions, skip tests, or add `# noqa` / `# type: ignore` to get green.
- Put real guest data on an unauthenticated instance.
