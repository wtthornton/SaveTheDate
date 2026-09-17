# The production image. TAP-7733.
#
# Built from the wheel rather than from a copied source tree, because the wheel is
# exactly what `pyproject.toml` says the application is — templates, the committed
# `app/static/app.css`, the vendored htmx and the photographs all travel inside it.
# A copied tree would also work and would drift: it would carry whatever else
# happened to be in the checkout.
#
# No Node, no Tailwind binary. `app/static/app.css` is committed on purpose so that
# building this image never needs the 110MB standalone binary on the server.

FROM python:3.12-slim AS builder

WORKDIR /build

# Only the inputs the wheel is built from. Dependencies are NOT resolved here —
# `pip wheel --no-deps` builds this project alone, and the runtime stage installs
# the dependency closure from the wheel's own metadata.
COPY pyproject.toml README.md ./
COPY app ./app

RUN pip wheel --no-deps --wheel-dir /wheels .


FROM python:3.12-slim

# Unbuffered so `docker compose logs` shows a traceback at the moment it happens
# rather than when a buffer happens to flush. On a box nobody is logged into, a log
# that arrives late is a log that arrives after the guess has already been made.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# A non-root runtime user. Nothing here writes to disk — Postgres owns the only
# state — so the application never needs to own a path.
RUN useradd --create-home --uid 10001 savethedate

WORKDIR /srv

COPY --from=builder /wheels/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

# Alembic runs from this directory in the one-shot `migrate` service. It is NOT
# part of the wheel: migrations are a release step, not something the application
# imports, and keeping them out of the package makes that structurally true rather
# than a convention someone can forget.
COPY alembic.ini ./
COPY migrations ./migrations

USER savethedate

EXPOSE 8000

# `python -c` rather than curl, so the image carries no HTTP client it does not
# otherwise need. urllib raises on a non-2xx status, so any failure is a non-zero
# exit without a status comparison.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

# One worker, and that is a correctness requirement rather than a sizing choice.
# `app/ratelimit.py` counts in process memory, so N workers would mean N times the
# configured allowance — a limit of 60 silently becoming 240. The guest ceiling is
# under a hundred people across several months; one worker is also plenty.
#
# `--proxy-headers` is required, not decorative: `routers/host.py` builds the invite
# links a host copies out of `request.base_url`, so without X-Forwarded-Proto the
# dashboard would hand somebody an `http://` link for an https-only site.
#
# `--forwarded-allow-ips *` trusts the immediate peer, which can only ever be the
# tunnel: compose publishes this container on 127.0.0.1 alone (see
# docker-compose.prod.yml), so nothing off this box can open the socket at all. The
# alternative — naming the Docker bridge gateway — pins a subnet Docker is free to
# renumber, trading a real risk for a fragile one. Note that this flag is NOT what
# protects the rate limiter: that trusts CF-Connecting-IP via
# TRUSTED_CLIENT_IP_HEADER, and is guarded by the same "only the tunnel can reach
# this socket" fact.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info", "--proxy-headers", "--forwarded-allow-ips", "*"]
