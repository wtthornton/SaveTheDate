from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.routers import events, invites, pages

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


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(events.router)
app.include_router(invites.router)
app.include_router(pages.router)


@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
def robots() -> str:
    """Belt and braces with the header above. Crawlers read this before anything else."""
    return "User-agent: *\nDisallow: /\n"


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    return {"status": "ok"}
