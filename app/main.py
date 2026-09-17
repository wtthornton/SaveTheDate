from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.routers import events, invites, pages

STATIC_DIR = Path(__file__).resolve().parent / "static"

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

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(events.router)
app.include_router(invites.router)
app.include_router(pages.router)


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    return {"status": "ok"}
