import logging
import math
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request, Response, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.deps import DbSession
from app.error_collector import init_error_collector
from app.observability import (
    REQUEST_ID_HEADER,
    bind_request_id,
    configure_logging,
    current_request_id,
    redact,
    request_id_from,
)
from app.ratelimit import client_address, get_limiter
from app.routers import auth, events, host, invites, pages, public, webhooks
from app.templating import templates

STATIC_DIR = Path(__file__).resolve().parent / "static"

# An invite URL is a bearer credential. A crawler that reaches one has published
# somebody's RSVP link, so nothing this app serves may be indexed — the meta tag on
# the pages covers HTML, and this covers the JSON API and everything else.
NOINDEX = "noindex, nofollow, noarchive"

configure_logging()
logger = logging.getLogger("app")
request_logger = logging.getLogger("app.request")

settings = get_settings()

# Before the app is built, so the SDK's Starlette patches are in place when the
# middleware stack is assembled. Does nothing at all without a DSN. TAP-12484.
init_error_collector(settings)

app = FastAPI(
    title="SaveTheDate",
    description="Save-the-date and RSVP service for weddings and events.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_index(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = NOINDEX
    return response


# The public guest routes, and only those. `/static` is excluded because one guest
# page pulls a stylesheet, htmx and four photographs, so counting those would throttle
# a single visitor before they finished reading. Host routes are behind a session
# already, and `/health` is hit by the platform every few seconds.
THROTTLED_PREFIXES = ("/invites/", "/api/invites/")


@app.middleware("http")
async def throttle_guest_routes(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Count before looking anything up. TAP-7727.

    Running here rather than in a route dependency is what makes a throttled real
    token indistinguishable from a made-up one: neither reaches the database, so the
    status, the body and the time taken are identical.
    """
    if not request.url.path.startswith(THROTTLED_PREFIXES):
        return await call_next(request)

    address = client_address(
        request.client.host if request.client else None,
        {key.lower(): value for key, value in request.headers.items()},
    )
    wait = get_limiter().retry_after(address)
    if wait is None:
        return await call_next(request)

    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": "too many requests; please wait a moment and try again"},
        # Whole seconds, and never zero: RFC 9110 wants an integer, and a client
        # reading "0" would retry immediately into another 429.
        headers={"Retry-After": str(max(1, math.ceil(wait))), "X-Robots-Tag": NOINDEX},
    )


# Paths a probe hits every few seconds for as long as the stack runs. A line for each
# would bury every real request, so they are logged only when they fail.
QUIET_PATHS = frozenset({"/health", "/health/live"})


@app.middleware("http")
async def log_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """One line per request, carrying an id that every other line it causes shares. TAP-7734.

    Registered last, so it is the OUTERMOST middleware: a request the throttle turns
    away with a 429 is logged too, and so is one that raises. The path is redacted here
    as well as in the formatter, so the `path` field is safe even to a reader who
    pulls it out of the record before formatting.
    """
    request_id = request_id_from(request.headers.get(REQUEST_ID_HEADER))
    bind_request_id(request_id)
    started = time.perf_counter()
    fields = {"method": request.method, "path": redact(request.url.path)}

    try:
        response = await call_next(request)
    except Exception:
        # Logged with the request's id and then RAISED again: Starlette still turns it
        # into the 500, and nothing here decides the request succeeded.
        request_logger.exception(
            "request failed",
            extra={**fields, "duration_ms": _elapsed_ms(started)},
        )
        raise

    response.headers[REQUEST_ID_HEADER] = request_id
    if request.url.path not in QUIET_PATHS or response.status_code >= 400:
        request_logger.info(
            "request",
            extra={
                **fields,
                "status": response.status_code,
                "duration_ms": _elapsed_ms(started),
            },
        )
    return response


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth.router)
app.include_router(events.router)
app.include_router(host.router)
app.include_router(invites.router)
app.include_router(pages.router)
app.include_router(public.router)
app.include_router(webhooks.router)


# Prefixes whose callers are programs or signed-in hosts rather than guests. A 404 from
# any of these stays machine-readable: a caterer's script should not have to parse
# wedding prose, and a host looking at somebody else's event should not be told their
# *invitation* could not be found.
MACHINE_READABLE_PREFIXES = ("/api/", "/events", "/auth/", "/webhooks/", "/host")


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> Response:
    """The 500 a crash produces, carrying the request id like every other response.

    Starlette runs this from its outermost error middleware, after `log_request` has
    logged the traceback and re-raised; Starlette re-raises again once this response is
    sent, so nothing here hides the failure. Without it the one response a host most
    needs to match to a log line was the one that came back without an id. TAP-7734.
    """
    request_id = current_request_id()
    headers = {"X-Robots-Tag": NOINDEX}
    if request_id is not None:
        headers[REQUEST_ID_HEADER] = request_id
    return PlainTextResponse(
        "Internal Server Error",
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        headers=headers,
    )


@app.exception_handler(StarletteHTTPException)
async def guest_facing_not_found(request: Request, exc: StarletteHTTPException) -> Response:
    """Serve the written 404 to people, and JSON to everything else.

    The site has had a designed "We could not find that invitation" page since
    TAP-7728, but it was only reachable through a token that parsed and matched
    nothing. Anyone typing the bare hostname, or pasting a link that lost its whole
    tail rather than one character, got FastAPI's raw `{"detail":"Not Found"}`.

    Only 404 is special-cased; every other status goes to the default handler, so a
    401 or a 422 keeps the body its caller expects.
    """
    if exc.status_code != status.HTTP_404_NOT_FOUND:
        return await http_exception_handler(request, exc)

    path = request.url.path
    wants_html = "text/html" in request.headers.get("accept", "")
    if path.startswith(MACHINE_READABLE_PREFIXES) or not wants_html:
        return await http_exception_handler(request, exc)

    return templates.TemplateResponse(
        request=request,
        name="not_found.html",
        context={"review_instance": get_settings().review_instance},
        status_code=status.HTTP_404_NOT_FOUND,
    )


@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
def robots() -> str:
    """Belt and braces with the header above. Crawlers read this before anything else."""
    return "User-agent: *\nDisallow: /\n"


class HealthTransitions:
    """What /health remembers between probes, so an outage is reported once, not 5,760 times.

    The error collector turns every record at ERROR or above into an event, and
    anything below into, at most, a breadcrumb (`app.error_collector`). Docker probes
    /health every 15 seconds, so an ERROR per failed probe made a day's outage 5,760
    events. TAP-12504. Now only a failure that starts an outage is an ERROR, and only
    if no outage was reported in the last `quiet_for` seconds: a database that drops
    every other probe is otherwise a new outage each time, the same flood by another
    road. Every other failure is a WARNING, which the JSON log keeps, traceback and
    all, and the collector does not. Recovery is an INFO line. The collector itself
    stays stateless: this is the only memory involved.

    `health` is a plain `def`, so FastAPI runs it on a worker thread and two probes can
    overlap. The lock makes each read and write one step, so two overlapping first
    failures cannot both be reported. Each uvicorn worker process has its own.
    """

    def __init__(
        self, *, quiet_for: float = 3600.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._lock = threading.Lock()
        self._quiet_for = quiet_for
        self._clock = clock
        # A process that has just started has no reason to think the database is down,
        # so a database already down at boot is still reported once.
        self._reachable = True
        self._last_reported: float | None = None

    def failed(self) -> bool:
        """Note a failed probe. True if it starts an outage that should be reported."""
        with self._lock:
            starts_outage = self._reachable
            self._reachable = False
            if not starts_outage:
                return False
            now = self._clock()
            if self._last_reported is not None and now - self._last_reported < self._quiet_for:
                return False
            self._last_reported = now
            return True

    def succeeded(self) -> bool:
        """Note a probe that reached the database. True if it ends an outage."""
        with self._lock:
            ends_outage = not self._reachable
            self._reachable = True
            return ends_outage


database_health = HealthTransitions()


@app.get("/health", tags=["ops"])
def health(response: Response, db: DbSession) -> dict[str, str]:
    """Ready: the process answers AND it can reach its database. TAP-7734.

    This used to return ok without checking anything, so with Postgres down every
    guest page failed while the Docker healthcheck, `scripts/prod.sh status` and
    `scripts/prod.sh deploy` all reported a healthy site. Those three still call this
    path, and now they report the outage.
    """
    try:
        db.execute(text("select 1"))
    except SQLAlchemyError:
        # Not swallowed: logged with its traceback, and answered as a failure. Only
        # a failure that starts a reportable outage is an ERROR, and so an event.
        if database_health.failed():
            logger.exception("health check: database unreachable")
        else:
            logger.warning("health check: database unreachable, already reported", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "unreachable"}
    if database_health.succeeded():
        logger.info("health check: database reachable again")
    return {"status": "ok", "database": "ok"}


@app.get("/health/live", tags=["ops"])
def live() -> dict[str, str]:
    """Alive: the process is up and serving, and nothing more.

    For a supervisor deciding whether to RESTART the app. A database outage must not
    answer that question with yes, because restarting the app cannot fix the database
    and a restart loop makes the outage harder to read.
    """
    return {"status": "ok"}
