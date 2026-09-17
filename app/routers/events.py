"""Host-facing event and guest endpoints.

Every route here that creates an event, adds a guest, or reads a guest list requires a
signed-in host (TAP-7725). `GET /events/{event_id}/guests` is the sharp one: it returns
every `invite_token` on the event, which is enough to RSVP as anybody.

Authorization is a separate matter and is NOT done here. A signed-in host can still
reach another host's event, because `events` has no owner yet — that is TAP-7726, which
adds `events.host_id` and filters every query by it.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth import CurrentHost
from app.deps import DbSession
from app.models import Event, Guest
from app.schemas import EventCreate, EventOut, GuestCreate, GuestOut

router = APIRouter(prefix="/events", tags=["events"])


@router.post("", response_model=EventOut, status_code=status.HTTP_201_CREATED)
def create_event(payload: EventCreate, db: DbSession, host: CurrentHost) -> Event:
    event = Event(**payload.model_dump())
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
def get_event(slug: str, db: DbSession) -> Event:
    event = db.scalar(select(Event).where(Event.slug == slug))
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    return event


@router.post("/{event_id}/guests", response_model=GuestOut, status_code=status.HTTP_201_CREATED)
def add_guest(event_id: uuid.UUID, payload: GuestCreate, db: DbSession, host: CurrentHost) -> Guest:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    guest = Guest(event_id=event.id, **payload.model_dump())
    db.add(guest)
    db.commit()
    return guest


@router.get("/{event_id}/guests", response_model=list[GuestOut])
def list_guests(event_id: uuid.UUID, db: DbSession, host: CurrentHost) -> list[Guest]:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")
    return list(db.scalars(select(Guest).where(Guest.event_id == event_id).order_by(Guest.name)))
