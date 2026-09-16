---
description: Run the full quality gate and report failures verbatim.
---
Run each of these in order and show the real output. Do not summarise away a failure.
1. `docker compose up -d db` and wait for healthy
2. `.venv/bin/ruff check .`
3. `.venv/bin/ruff format --check .`
4. `.venv/bin/mypy app migrations scripts tests`
5. The migration round trip — **against the TEST database, never the dev one.**
   `downgrade base` drops every table, so running this on `DATABASE_URL`'s default
   destroys local data, including a running TAP-7738 review instance and its tokens.
   ```
   DB="${TEST_DATABASE_URL:-postgresql+psycopg://savethedate:savethedate@localhost:5434/savethedate_test}"
   DATABASE_URL="$DB" .venv/bin/alembic upgrade head && \
   DATABASE_URL="$DB" .venv/bin/alembic downgrade base && \
   DATABASE_URL="$DB" .venv/bin/alembic upgrade head
   ```
6. `.venv/bin/pytest -q`
If anything fails, stop and report it. Never fix by suppressing.
