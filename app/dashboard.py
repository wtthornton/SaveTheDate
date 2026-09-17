"""What the host actually needs to know, computed from the rows. TAP-7730.

Everything here is **seat-based**, never invitation-based. An invitation for four where
two people are coming is two, not one and not four, and no count in this module is read
from a stored integer — they are all derived from `attendees` and `attendance`, so they
cannot drift from what guests actually said.

Headcounts are **per day**, not per plate. TAP-7739 cut meal options deliberately and
the invariants say so: dietary tags only. The caterer gets a count for each segment,
children separately, and the dietary tags rolled up with the free-text notes attributed
by name underneath — an allergy nobody can trace to a person is not usable.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Attendee, Event, Guest, Segment
from app.schemas import DIETARY_LABELS, DietaryTag


@dataclass(frozen=True)
class Headline:
    """The one line a host wants: how many people are coming.

    `awaiting` is seats, not invitations. An invitation for four that has named two
    people has answered for two and still owes two, which is the number the caterer
    is exposed to.
    """

    invited_seats: int
    accepted: int
    declined: int
    awaiting: int

    @property
    def replied_invitations(self) -> int:
        return self.accepted + self.declined


@dataclass(frozen=True)
class SegmentCount:
    """One scheduled item, and who is coming to it."""

    segment: Segment
    adults: int
    children: int

    @property
    def total(self) -> int:
        return self.adults + self.children


@dataclass(frozen=True)
class DietaryNote:
    name: str
    note: str


@dataclass(frozen=True)
class Dietary:
    """Counts per tag, plus the free text, both only for people actually coming.

    Someone who declined does not eat, so counting their tag would inflate the number
    the caterer cooks to.
    """

    counts: list[tuple[str, int]]
    notes: list[DietaryNote]

    @property
    def is_empty(self) -> bool:
        return not self.counts and not self.notes


@dataclass(frozen=True)
class InvitationRow:
    """One invitation on the guest list, as a host reads it."""

    guest: Guest
    accepted: int
    declined: int

    @property
    def seats(self) -> int:
        return self.guest.party_size

    @property
    def answered(self) -> int:
        return self.accepted + self.declined

    @property
    def status(self) -> str:
        if self.answered == 0:
            return "awaiting reply"
        if self.accepted == 0:
            return "not coming"
        if self.answered < self.seats:
            return "partly answered"
        if self.declined:
            return "some coming"
        return "all coming"

    @property
    def note(self) -> str | None:
        return self.guest.rsvp.note if self.guest.rsvp else None


@dataclass(frozen=True)
class Dashboard:
    event: Event
    headline: Headline
    segments: list[SegmentCount]
    dietary: Dietary
    invitations: list[InvitationRow]


def _attending(guest: Guest) -> list[Attendee]:
    return [person for person in guest.attendees if person.attending]


def build(event: Event, db: Session) -> Dashboard:
    """Everything the dashboard renders, in one pass over the event's rows."""
    guests = list(
        db.scalars(
            select(Guest)
            .where(Guest.event_id == event.id)
            .order_by(Guest.name)
            .options(
                selectinload(Guest.attendees).selectinload(Attendee.attendance),
                selectinload(Guest.rsvp),
            )
        )
    )
    segments = list(
        db.scalars(select(Segment).where(Segment.event_id == event.id).order_by(Segment.sort_order))
    )

    invitations = [
        InvitationRow(
            guest=guest,
            accepted=sum(1 for person in guest.attendees if person.attending),
            declined=sum(1 for person in guest.attendees if not person.attending),
        )
        for guest in guests
    ]

    invited_seats = sum(row.seats for row in invitations)
    accepted = sum(row.accepted for row in invitations)
    declined = sum(row.declined for row in invitations)

    return Dashboard(
        event=event,
        headline=Headline(
            invited_seats=invited_seats,
            accepted=accepted,
            declined=declined,
            # Never negative: the API caps an RSVP at the invitation's party size, but
            # a host shrinking `party_size` after people replied would otherwise show
            # a negative number of outstanding seats.
            awaiting=max(invited_seats - accepted - declined, 0),
        ),
        segments=_segment_counts(guests, segments),
        dietary=_dietary(guests),
        invitations=invitations,
    )


def _segment_counts(guests: list[Guest], segments: list[Segment]) -> list[SegmentCount]:
    """Per-day headcount, adults and children apart.

    Read from `attendance`, so "said no to golf" stays distinct from "never answered
    about golf" — only an explicit yes is counted.
    """
    adults: Counter[uuid.UUID] = Counter()
    children: Counter[uuid.UUID] = Counter()

    for guest in guests:
        for person in _attending(guest):
            bucket = children if person.is_child else adults
            for row in person.attendance:
                if row.attending:
                    bucket[row.segment_id] += 1

    return [
        SegmentCount(segment=segment, adults=adults[segment.id], children=children[segment.id])
        for segment in segments
    ]


def _dietary(guests: list[Guest]) -> Dietary:
    tally: Counter[str] = Counter()
    notes: list[DietaryNote] = []

    for guest in guests:
        for person in _attending(guest):
            for tag in person.dietary_tags:
                tally[tag] += 1
            if person.dietary_notes:
                notes.append(DietaryNote(name=person.name, note=person.dietary_notes))

    # Ordered by the fixed vocabulary rather than by count, so the list does not
    # reshuffle under the host every time somebody replies.
    counts = [(DIETARY_LABELS[tag], tally[str(tag)]) for tag in DietaryTag if tally[str(tag)]]
    return Dietary(counts=counts, notes=sorted(notes, key=lambda entry: entry.name))
