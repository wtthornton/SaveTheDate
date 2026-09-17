"""The JSON view of an invitation.

This moved under `/api` in TAP-7728. `/invites/{token}` is the link that goes in
somebody's inbox, and that has to be a page — see `app.routers.pages`. The token
itself is unchanged, which is the invariant the whole schema hangs off.
"""

from fastapi import APIRouter, HTTPException, status

from app import rsvp as rsvp_domain
from app.deps import DbSession, Now
from app.models import Guest
from app.schemas import (
    AttendanceOut,
    AttendeeOut,
    EventOut,
    InviteOut,
    RsvpCreate,
    RsvpOut,
    SegmentOut,
)

router = APIRouter(prefix="/api/invites", tags=["invites"])


def attendees_out(guest: Guest) -> list[AttendeeOut]:
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
def get_invite(token: str, db: DbSession, now: Now) -> InviteOut:
    guest = rsvp_domain.load_guest(token, db)
    answer = guest.rsvp
    return InviteOut(
        event=EventOut.model_validate(guest.event),
        guest_name=guest.name,
        party_size=guest.party_size,
        phase=rsvp_domain.phase(guest.event, now),
        segments=[
            SegmentOut.model_validate(segment)
            for segment in rsvp_domain.segments_for(guest.event_id, db)
        ],
        rsvp=(
            None
            if answer is None
            else RsvpOut(
                note=answer.note,
                responded_at=answer.responded_at,
                attendees=attendees_out(guest),
            )
        ),
    )


@router.put("/{token}/rsvp", response_model=RsvpOut)
def submit_rsvp(token: str, payload: RsvpCreate, db: DbSession, now: Now) -> RsvpOut:
    guest = rsvp_domain.load_guest(token, db)
    rsvp_domain.require_open(guest.event, now)

    known = {segment.id for segment in rsvp_domain.segments_for(guest.event_id, db)}
    try:
        rsvp_domain.validate(payload, guest, known)
    except rsvp_domain.RsvpRefused as refused:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=refused.message
        ) from refused

    answer = rsvp_domain.save(payload, guest, db)
    return RsvpOut(
        note=answer.note,
        responded_at=answer.responded_at,
        attendees=attendees_out(guest),
    )
