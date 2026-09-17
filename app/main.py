import math
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request, Response, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.ratelimit import client_address, get_limiter
from app.routers import auth, events, host, invites, pages, public, webhooks
from app.templating import templates

STATIC_DIR = Path(__file__).resolve().parent / "static"

# An invite URL is a bearer credential. A crawler that reaches one has published
# somebody's RSVP link, so nothing this app serves may be indexed — the meta tag on
# the pages covers HTML, and this covers the JSON API and everything else.
NOINDEX = "noindex, nofollow, noarchive"

settings = get_settings()

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


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    return {"status": "ok"}
