"""RSVP domain logic, shared by the JSON API and the rendered guest pages.

Both surfaces answer the same questions — which phase is this event in, may this
payload be written, what does an existing answer look like — so the rules live here
rather than in either router. The page routes must not be able to drift from the API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.models import Attendance, Attendee, Event, Guest, Rsvp, Segment
from app.schemas import AttendanceIn, AttendeeIn, DietaryTag, RsvpCreate, RsvpPhase


class RsvpRefused(Exception):
    """A payload the guest can fix, carrying wording meant for the guest to read."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def load_guest(token: str, db: Session) -> Guest:
    guest = db.scalar(select(Guest).where(Guest.invite_token == token))
    if guest is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invite not found")
    return guest


def phase(event: Event, now: datetime | None = None) -> RsvpPhase:
    """Which of the three RSVP phases the event is in at `now`.

    A null `rsvp_opens_at` means open from the start; a null `rsvp_deadline` means
    it never closes.

    Both stored bounds are aware instants — `EventCreate` refuses a naive one — so the
    comparison below is already in the event's own day, whatever zone the server keeps.
    The lower bound is inclusive and the upper bound exclusive: a deadline of "December
    15" is held as midnight at the start of the 16th, which is what `deadline_date()`
    renders back to a guest.

    `now` defaults to the real clock for direct callers; the routes pass the `Now`
    dependency so a test can pin it to a boundary. TAP-7729.
    """
    moment = datetime.now(UTC) if now is None else now
    if event.rsvp_opens_at is not None and moment < event.rsvp_opens_at:
        return "before_open"
    if event.rsvp_deadline is not None and moment >= event.rsvp_deadline:
        return "closed"
    return "open"


def segments_for(event_id: uuid.UUID, db: Session) -> list[Segment]:
    return list(
        db.scalars(select(Segment).where(Segment.event_id == event_id).order_by(Segment.sort_order))
    )


def require_open(event: Event, now: datetime | None = None) -> None:
    """Refuse a write outside the RSVP window, with wording that says which end."""
    current = phase(event, now)
    if current == "before_open":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="RSVPs have not opened yet"
        )
    if current == "closed":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="the RSVP deadline has passed"
        )


def validate(payload: RsvpCreate, guest: Guest, known_segments: set[uuid.UUID]) -> None:
    """Reject a payload before it reaches the database.

    The attendee/attendance consistency rule is also enforced by a deferred constraint
    trigger. It is checked here as well so a guest gets an explanation rather than a
    database error.
    """
    if len(payload.attendees) > guest.party_size:
        raise RsvpRefused(f"invitation covers at most {guest.party_size} guest(s)")

    for attendee in payload.attendees:
        segment_ids = [row.segment_id for row in attendee.attendance]
        if len(segment_ids) != len(set(segment_ids)):
            raise RsvpRefused(f"{attendee.name} answers for the same segment twice")

        unknown = set(segment_ids) - known_segments
        if unknown:
            raise RsvpRefused(f"{attendee.name} answers for a segment that is not on this event")

        coming_to_something = any(row.attending for row in attendee.attendance)
        # Only meaningful once the event has a schedule; before that `attending`
        # stands alone and there is nothing for it to disagree with.
        if known_segments and attendee.attending and not coming_to_something:
            raise RsvpRefused(f"{attendee.name} is marked attending but is not coming to anything")
        if not attendee.attending and coming_to_something:
            raise RsvpRefused(f"{attendee.name} is marked not attending but is coming to something")


def save(payload: RsvpCreate, guest: Guest, db: Session) -> Rsvp:
    """Write an answer, replacing any previous one."""
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
    return rsvp


# -- The rendered form ----------------------------------------------------


@dataclass(frozen=True)
class AttendeeRow:
    """One person's slot on the form, whether or not they have been named yet.

    An invitation for four renders four of these. `answered` distinguishes "said no"
    from "has not replied", which matters because the two look identical in a
    checkbox and mean opposite things to a caterer.
    """

    index: int
    name: str
    attending: bool
    answered: bool
    dietary_tags: frozenset[str]
    dietary_notes: str
    coming_to: frozenset[uuid.UUID]

    def is_coming_to(self, segment: Segment) -> bool:
        return segment.id in self.coming_to


def form_rows(guest: Guest, segments: list[Segment]) -> list[AttendeeRow]:
    """One row per seat the invitation covers, filled in from any existing answer.

    An unanswered row arrives with the non-optional segments already ticked: those
    are the parts of the weekend everybody is invited to, so the common case is no
    work. The paid extras — golf, the charter — start unticked, because nobody
    should book one by failing to notice a checkbox.
    """
    default_segments = frozenset(segment.id for segment in segments if not segment.is_optional)
    existing = list(guest.attendees)
    rows: list[AttendeeRow] = []

    for index in range(guest.party_size):
        if index < len(existing):
            attendee = existing[index]
            rows.append(
                AttendeeRow(
                    index=index,
                    name=attendee.name,
                    attending=attendee.attending,
                    answered=True,
                    dietary_tags=frozenset(attendee.dietary_tags),
                    dietary_notes=attendee.dietary_notes or "",
                    coming_to=frozenset(
                        row.segment_id for row in attendee.attendance if row.attending
                    ),
                )
            )
        else:
            # The first seat is the named invitee; the rest are "and guest" until
            # somebody fills them in.
            rows.append(
                AttendeeRow(
                    index=index,
                    name=guest.name if index == 0 and not existing else "",
                    attending=True,
                    answered=False,
                    dietary_tags=frozenset(),
                    dietary_notes="",
                    coming_to=default_segments,
                )
            )

    return rows


def _checked(form: FormData, field: str) -> set[str]:
    return {str(value) for value in form.getlist(field)}


def parse_form(form: FormData, guest: Guest, segments: list[Segment]) -> RsvpCreate:
    """Turn a submitted HTML form into the same payload the JSON API takes.

    A seat is only recorded once it has both a name and an answer: an "and guest"
    nobody filled in is not a person, and inventing a placeholder name would put a
    fictional guest in the caterer's count.
    """
    try:
        declared = int(str(form.get("attendee-count", "0")))
    except ValueError as exc:
        raise RsvpRefused("that form could not be read; please try again") from exc

    attendees: list[AttendeeIn] = []
    for index in range(min(declared, guest.party_size)):
        name = str(form.get(f"attendee-{index}-name", "")).strip()
        answer = str(form.get(f"attendee-{index}-attending", "")).strip().lower()
        if not name or answer not in {"yes", "no"}:
            continue

        attending = answer == "yes"
        ticked = _checked(form, f"attendee-{index}-segments")
        # Someone who is not coming is not coming to anything, whatever the day
        # checkboxes still say — they are meaningless once "sadly cannot" is chosen,
        # and the consistency trigger would reject the pair.
        coming_to = {str(segment.id) for segment in segments} & ticked if attending else set()

        tags: list[DietaryTag] = []
        for raw in sorted(_checked(form, f"attendee-{index}-diet")):
            try:
                tags.append(DietaryTag(raw))
            except ValueError as exc:
                raise RsvpRefused(f"{raw!r} is not a dietary option on this form") from exc

        notes = str(form.get(f"attendee-{index}-notes", "")).strip()
        attendees.append(
            AttendeeIn(
                name=name,
                attending=attending,
                dietary_tags=tags,
                dietary_notes=notes or None,
                # Every segment gets an explicit answer, so "said no to golf" stays
                # distinct from "never answered about golf".
                attendance=[
                    AttendanceIn(segment_id=segment.id, attending=str(segment.id) in coming_to)
                    for segment in segments
                ],
            )
        )

    note = str(form.get("note", "")).strip()
    return RsvpCreate(note=note or None, attendees=attendees)
