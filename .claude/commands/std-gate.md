---
description: Run the full quality gate and report failures verbatim.
---
Run each of these in order and show the real output. Do not summarise away a failure.
1. `docker compose up -d db` and wait for healthy
2. `.venv/bin/ruff check .`
3. `.venv/bin/ruff format --check .`
4. `.venv/bin/mypy app migrations scripts tests`
5. `.venv/bin/alembic upgrade head && .venv/bin/alembic downgrade base && \
    .venv/bin/alembic upgrade head`
6. `.venv/bin/pytest -q`
If anything fails, stop and report it. Never fix by suppressing.
