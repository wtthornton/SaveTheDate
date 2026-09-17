"""The host's own pages. TAP-7730.

Server-rendered like the guest side, and behind a session cookie. `/auth/*` is the JSON
API for the same thing; these are the HTML forms a person actually uses, because a
dashboard nobody can sign in to from a browser is not a dashboard.

Nothing here re-keys a guests row. An invitation can be renamed, resized or withdrawn
entirely, and withdrawing deletes the row so the token stops working — but no row ever
keeps its identity while getting a new token, because that token is already in an inbox.
"""

from __future__ import annotations

import csv
import io
import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from starlette.datastructures import UploadFile as StarletteUploadFile

from app import auth, dashboard, guest_import
from app.auth import CurrentHost, current_host
from app.config import get_settings
from app.deps import DbSession, Now
from app.models import Event, Guest, Host
from app.templating import templates

router = APIRouter(prefix="/host", tags=["host"])


def _render(request: Request, name: str, context: dict[str, object]) -> Response:
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={"review_instance": get_settings().review_instance, **context},
    )


def _owned_event(event_id: uuid.UUID, db: DbSession, host: Host) -> Event:
    """This host's event, or 404 — the same rule as the JSON API. TAP-7726."""
    event = db.scalar(select(Event).where(Event.id == event_id, Event.host_id == host.id))
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    return event


def _seats(raw: object, *, default: int) -> int:
    """A party size from a form field, clamped, with no exception to swallow.

    `str.isdigit` rather than `try: int(...)`: a browser's `type="number"` will not
    send anything else, and parsing without raising means there is no failure here to
    quietly discard. Anything unparseable leaves the value as it was.
    """
    text = str(raw or "").strip()
    if not text.isdigit():
        return default
    return max(1, min(20, int(text)))


def _owned_guest(event: Event, guest_id: uuid.UUID, db: DbSession) -> Guest:
    guest = db.scalar(select(Guest).where(Guest.id == guest_id, Guest.event_id == event.id))
    if guest is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invitation not found")
    return guest


# -- Signing in from a browser --------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> Response:
    return _render(request, "host_login.html", {"error": None, "email": ""})


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, db: DbSession, now: Now) -> Response:
    form = await request.form()
    email = str(form.get("email", "")).strip().casefold()
    password = str(form.get("password", ""))

    host = db.scalar(select(Host).where(Host.email == email)) if email else None
    if host is None:
        # Same cost and the same message as a wrong password — see app.auth.
        auth.burn_dummy_verification()
        host = None
    elif not auth.verify_password(host.password_hash, password):
        host = None

    if host is None:
        return templates.TemplateResponse(
            request=request,
            name="host_login.html",
            context={
                "review_instance": get_settings().review_instance,
                "error": "Those details do not match. Please try again.",
                "email": str(form.get("email", "")),
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    settings = get_settings()
    response = RedirectResponse(url="/host", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=settings.session_cookie_name,
        value=auth.start_session(host, db, now),
        max_age=settings.session_lifetime_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return response


@router.post("/logout")
def logout(request: Request, db: DbSession) -> Response:
    settings = get_settings()
    auth.end_session(request.cookies.get(settings.session_cookie_name), db)
    response = RedirectResponse(url="/host/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key=settings.session_cookie_name, path="/")
    return response


# -- The dashboard --------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def index(request: Request, db: DbSession, host: CurrentHost) -> Response:
    """Straight to the event when there is only one, which is the normal case here."""
    events = list(
        db.scalars(select(Event).where(Event.host_id == host.id).order_by(Event.created_at))
    )
    if len(events) == 1:
        return RedirectResponse(
            url=f"/host/events/{events[0].id}", status_code=status.HTTP_303_SEE_OTHER
        )
    return _render(request, "host_events.html", {"events": events, "host": host})


@router.get("/events/{event_id}", response_class=HTMLResponse)
def event_dashboard(
    event_id: uuid.UUID, request: Request, db: DbSession, host: CurrentHost
) -> Response:
    event = _owned_event(event_id, db, host)
    view = dashboard.build(event, db)
    return _render(
        request,
        "host_dashboard.html",
        {
            "host": host,
            "event": event,
            "view": view,
            # From the request, not from `public_base_url`. The host copies these
            # links off this screen, so they have to point at whatever they are
            # actually browsing — the tunnel URL on the review box, the real
            # hostname in production. A configured setting would hand them a
            # localhost link that works for nobody.
            "base_url": str(request.base_url).rstrip("/"),
        },
    )


@router.get("/events/{event_id}/guests.csv")
def guests_csv(event_id: uuid.UUID, db: DbSession, host: CurrentHost) -> Response:
    """The guest list as a file, for caterers and venues.

    Invite tokens are NOT in it. The point of the export is to hand it to a third
    party, and a token is the guest's credential — a spreadsheet that opens somebody
    else's RSVP is the same leak TAP-7725 closed, posted by hand.
    """
    event = _owned_event(event_id, db, host)
    view = dashboard.build(event, db)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["Invitation", "Email", "Seats", "Coming", "Not coming", "Status", "Attendees", "Notes"]
    )
    for row in view.invitations:
        writer.writerow(
            [
                row.guest.name,
                row.guest.email or "",
                row.seats,
                row.accepted,
                row.declined,
                row.status,
                "; ".join(
                    f"{person.name}{' (child)' if person.is_child else ''}"
                    f"{' — ' + ', '.join(person.dietary_tags) if person.dietary_tags else ''}"
                    for person in row.guest.attendees
                    if person.attending
                ),
                row.note or "",
            ]
        )

    buffer.seek(0)
    filename = f"{event.slug}-guests.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# -- Changing the guest list ----------------------------------------------


@router.post("/events/{event_id}/guests")
async def add_invitation(
    event_id: uuid.UUID, request: Request, db: DbSession, host: CurrentHost
) -> Response:
    event = _owned_event(event_id, db, host)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    email = str(form.get("email", "")).strip() or None
    seats = _seats(form.get("party_size"), default=1)

    if name:
        db.add(Guest(event_id=event.id, name=name, email=email, party_size=seats))
        db.commit()

    return RedirectResponse(url=f"/host/events/{event.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/events/{event_id}/guests/{guest_id}")
async def edit_invitation(
    event_id: uuid.UUID,
    guest_id: uuid.UUID,
    request: Request,
    db: DbSession,
    host: CurrentHost,
) -> Response:
    """Rename or resize an invitation. The token is never touched.

    `guests.invite_token` is already in somebody's inbox; changing a name must not
    change the link they were sent. That is the invariant the whole schema hangs off.
    """
    event = _owned_event(event_id, db, host)
    guest = _owned_guest(event, guest_id, db)
    form = await request.form()

    name = str(form.get("name", "")).strip()
    if name:
        guest.name = name
    guest.email = str(form.get("email", "")).strip() or None
    guest.party_size = _seats(form.get("party_size"), default=guest.party_size)

    db.commit()
    return RedirectResponse(url=f"/host/events/{event.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/events/{event_id}/import", response_class=HTMLResponse)
async def import_guests(
    event_id: uuid.UUID, request: Request, db: DbSession, host: CurrentHost
) -> Response:
    """Import a guest list from a spreadsheet. TAP-7732.

    All or nothing. The file is parsed and checked in full before a single row is
    written, and any problem at all refuses the whole thing with every bad line
    numbered. A partial import of a wedding guest list is worse than a rejected one:
    the host cannot tell what landed, and re-running the file would double it.
    """
    event = _owned_event(event_id, db, host)
    form = await request.form()
    upload = form.get("file")

    if not isinstance(upload, StarletteUploadFile):
        return _import_failed(request, host, event, db, ["Choose a CSV file to import."])

    raw = await upload.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # utf-8-sig also eats the BOM Excel writes, which would otherwise turn the
        # first header cell into "﻿name" and silently lose the name column.
        try:
            text = raw.decode("cp1252")
        except UnicodeDecodeError:
            return _import_failed(
                request,
                host,
                event,
                db,
                ["That file is not text this can read. Export it from your spreadsheet as CSV."],
            )

    existing = set(db.scalars(select(Guest.name).where(Guest.event_id == event.id)).all())
    plan = guest_import.parse(text, existing)

    if not plan.ok:
        return _import_failed(request, host, event, db, [str(p) for p in plan.problems])

    for row in plan.rows:
        db.add(Guest(event_id=event.id, name=row.name, email=row.email, party_size=row.party_size))
    db.commit()

    return RedirectResponse(
        url=f"/host/events/{event.id}?imported={len(plan.rows)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _import_failed(
    request: Request, host: Host, event: Event, db: DbSession, problems: list[str]
) -> Response:
    """Re-render the dashboard with the errors, so nothing the host typed is lost."""
    return templates.TemplateResponse(
        request=request,
        name="host_dashboard.html",
        context={
            "review_instance": get_settings().review_instance,
            "host": host,
            "event": event,
            "view": dashboard.build(event, db),
            "base_url": str(request.base_url).rstrip("/"),
            "import_problems": problems,
        },
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
    )


@router.post("/events/{event_id}/guests/{guest_id}/withdraw")
def withdraw_invitation(
    event_id: uuid.UUID, guest_id: uuid.UUID, db: DbSession, host: CurrentHost
) -> Response:
    """Withdraw an invitation, which kills its link.

    Deleting the row is what invalidates the token, and it is the only correct way to
    do it: re-keying the row instead would leave the person holding a link that now
    silently belongs to nobody, and the invariant forbids it. Their RSVP goes with it.
    """
    event = _owned_event(event_id, db, host)
    guest = _owned_guest(event, guest_id, db)
    db.delete(guest)
    db.commit()
    return RedirectResponse(url=f"/host/events/{event.id}", status_code=status.HTTP_303_SEE_OTHER)


__all__ = ["current_host", "router"]
