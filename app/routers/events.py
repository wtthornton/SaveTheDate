"""Host-facing event and guest endpoints.

Every route here requires a signed-in host (TAP-7725) *and* only ever reaches that
host's own events (TAP-7726). `GET /events/{event_id}/guests` is the sharp one: it
returns every `invite_token` on the event, which is enough to RSVP as anybody.

Somebody else's event answers **404, not 403**. A 403 would confirm the event exists,
which is what makes an id or a slug worth guessing at. "No such event" and "not yours"
are the same answer here, on purpose.

Guests never come through this module. They arrive at `/invites/{token}` with no
account and no session, and that must stay true.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth import CurrentHost
from app.deps import DbSession
from app.models import Event, Guest, Host
from app.schemas import EventCreate, EventOut, GuestCreate, GuestOut

router = APIRouter(prefix="/events", tags=["events"])


def _not_found() -> HTTPException:
    """One wording for every miss, so the two cases cannot be told apart."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")


def _owned(event_id: uuid.UUID, db: DbSession, host: Host) -> Event:
    """This host's event, or 404.

    The ownership filter is in the query rather than in an `if` after the fetch, so
    there is no path that loads somebody else's row and then decides what to do with it.
    """
    event = db.scalar(select(Event).where(Event.id == event_id, Event.host_id == host.id))
    if event is None:
        raise _not_found()
    return event


@router.post("", response_model=EventOut, status_code=status.HTTP_201_CREATED)
def create_event(payload: EventCreate, db: DbSession, host: CurrentHost) -> Event:
    event = Event(host_id=host.id, **payload.model_dump())
    db.add(event)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="slug already in use"
        ) from exc
    return event


@router.get("/{slug}", response_model=EventOut)
def get_event(slug: str, db: DbSession, host: CurrentHost) -> Event:
    """A host route, despite looking public: guests reach their event by token, never
    by slug, so nothing guest-facing regresses by closing this."""
    event = db.scalar(select(Event).where(Event.slug == slug, Event.host_id == host.id))
    if event is None:
        raise _not_found()
    return event


@router.post("/{event_id}/guests", response_model=GuestOut, status_code=status.HTTP_201_CREATED)
def add_guest(event_id: uuid.UUID, payload: GuestCreate, db: DbSession, host: CurrentHost) -> Guest:
    event = _owned(event_id, db, host)
    guest = Guest(event_id=event.id, **payload.model_dump())
    db.add(guest)
    db.commit()
    return guest


@router.get("/{event_id}/guests", response_model=list[GuestOut])
def list_guests(event_id: uuid.UUID, db: DbSession, host: CurrentHost) -> list[Guest]:
    event = _owned(event_id, db, host)
    return list(db.scalars(select(Guest).where(Guest.event_id == event.id).order_by(Guest.name)))
