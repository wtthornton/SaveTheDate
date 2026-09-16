import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.deps import DbSession
from app.models import Attendance, Attendee, Event, Guest, Rsvp, Segment
from app.schemas import (
    AttendanceOut,
    AttendeeOut,
    EventOut,
    InviteOut,
    RsvpCreate,
    RsvpOut,
    RsvpPhase,
    SegmentOut,
)

router = APIRouter(prefix="/invites", tags=["invites"])


def _load_guest(token: str, db: DbSession) -> Guest:
    guest = db.scalar(select(Guest).where(Guest.invite_token == token))
    if guest is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invite not found")
    return guest


def _phase(event: Event) -> RsvpPhase:
    """Which of the three RSVP phases the event is in right now.

    A null `rsvp_opens_at` means open from the start; a null `rsvp_deadline` means
    it never closes.
    """
    now = datetime.now(UTC)
    if event.rsvp_opens_at is not None and now < event.rsvp_opens_at:
        return "before_open"
    if event.rsvp_deadline is not None and now >= event.rsvp_deadline:
        return "closed"
    return "open"


def _segments(event_id: uuid.UUID, db: DbSession) -> list[Segment]:
    return list(
        db.scalars(select(Segment).where(Segment.event_id == event_id).order_by(Segment.sort_order))
    )


def _attendees_out(guest: Guest) -> list[AttendeeOut]:
    return [
        AttendeeOut(
            id=attendee.id,
            name=attendee.name,
            attending=attendee.attending,
            is_child=attendee.is_child,
            dietary_tags=list(attendee.dietary_tags),
            dietary_notes=attendee.dietary_notes,
            attendance=[
                AttendanceOut(segment_id=row.segment_id, attending=row.attending)
                for row in attendee.attendance
            ],
        )
        for attendee in guest.attendees
    ]


@router.get("/{token}", response_model=InviteOut)
def get_invite(token: str, db: DbSession) -> InviteOut:
    guest = _load_guest(token, db)
    rsvp = guest.rsvp
    return InviteOut(
        event=EventOut.model_validate(guest.event),
        guest_name=guest.name,
        party_size=guest.party_size,
        phase=_phase(guest.event),
        segments=[SegmentOut.model_validate(s) for s in _segments(guest.event_id, db)],
        rsvp=(
            None
            if rsvp is None
            else RsvpOut(
                note=rsvp.note,
                responded_at=rsvp.responded_at,
                attendees=_attendees_out(guest),
            )
        ),
    )


def _reject(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _validate(payload: RsvpCreate, guest: Guest, known_segments: set[uuid.UUID]) -> None:
    """Reject a payload before it reaches the database.

    The attendee/attendance consistency rule is also enforced by a deferred constraint
    trigger. It is checked here as well so a guest gets a 422 explaining the problem
    rather than a database error.
    """
    if len(payload.attendees) > guest.party_size:
        raise _reject(f"invitation covers at most {guest.party_size} guest(s)")

    for attendee in payload.attendees:
        segment_ids = [row.segment_id for row in attendee.attendance]
        if len(segment_ids) != len(set(segment_ids)):
            raise _reject(f"{attendee.name} answers for the same segment twice")

        unknown = set(segment_ids) - known_segments
        if unknown:
            raise _reject(f"{attendee.name} answers for a segment that is not on this event")

        coming_to_something = any(row.attending for row in attendee.attendance)
        # Only meaningful once the event has a schedule; before that `attending`
        # stands alone and there is nothing for it to disagree with.
        if known_segments and attendee.attending and not coming_to_something:
            raise _reject(f"{attendee.name} is marked attending but is not coming to anything")
        if not attendee.attending and coming_to_something:
            raise _reject(f"{attendee.name} is marked not attending but is coming to something")


@router.put("/{token}/rsvp", response_model=RsvpOut)
def submit_rsvp(token: str, payload: RsvpCreate, db: DbSession) -> RsvpOut:
    guest = _load_guest(token, db)

    phase = _phase(guest.event)
    if phase == "before_open":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="RSVPs have not opened yet"
        )
    if phase == "closed":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="the RSVP deadline has passed"
        )

    known_segments = {segment.id for segment in _segments(guest.event_id, db)}
    _validate(payload, guest, known_segments)

    rsvp = guest.rsvp
    if rsvp is None:
        rsvp = Rsvp(guest_id=guest.id, note=payload.note)
        db.add(rsvp)
    else:
        rsvp.note = payload.note

    # A guest may change their mind, so the same token replaces the previous answer.
    # Attendee rows carry no external identity, so replacing them is safe — unlike
    # `guests`, which is never re-keyed because its token is already in inboxes.
    for previous in list(guest.attendees):
        db.delete(previous)
    db.flush()

    for spec in payload.attendees:
        attendee = Attendee(
            guest_id=guest.id,
            name=spec.name,
            attending=spec.attending,
            is_child=spec.is_child,
            dietary_tags=[str(tag) for tag in spec.dietary_tags],
            dietary_notes=spec.dietary_notes,
        )
        db.add(attendee)
        db.flush()
        for row in spec.attendance:
            db.add(
                Attendance(
                    attendee_id=attendee.id,
                    segment_id=row.segment_id,
                    attending=row.attending,
                )
            )

    db.commit()
    db.refresh(guest)
    return RsvpOut(
        note=rsvp.note,
        responded_at=rsvp.responded_at,
        attendees=_attendees_out(guest),
    )
