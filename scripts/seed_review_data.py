"""Seed a database with the Port Aransas schedule and INVENTED guests.

    .venv/bin/python -m scripts.seed_review_data

The schedule is real. Every guest below is fictional, and must stay that way until
host authentication lands (TAP-7725): the host endpoints are unauthenticated, so
anyone who can reach this instance can read the whole guest list and its tokens.

Segments are seeded here rather than in the TAP-7739 migration so that one wedding's
details stay out of version-controlled schema history.
"""

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import engine
from app.models import Event, Guest, Host, Segment

CENTRAL = ZoneInfo("America/Chicago")
SLUG = "bill-and-lisa"

# The account the seeded event hangs off (TAP-7726). Not a login: the digest below is
# not parseable as argon2, so verification always fails and nobody can sign in as it.
# This is the throwaway review deployment; a real host is registered deliberately.
REVIEW_HOST_EMAIL = "review@invalid.localhost"
UNUSABLE_PASSWORD = "!no-login-review-instance"


# The wedding day, and the only calendar date written in this file. Every other
# moment in the weekend is expressed as an offset from it, so moving the wedding
# moves the whole schedule with it.
#
# That is not hypothetical tidiness: this date moved once already, from Sunday the
# 13th to Sunday the 20th, and the schedule had nine separate day numbers scattered
# through it. Nine places to edit is how a weekend ends up half-shifted, with the
# welcome party on the wrong Friday and nothing failing.
WEDDING_DAY = date(2028, 2, 20)


def _at(days_from_wedding: int, hour: int, minute: int = 0) -> datetime:
    """A moment during the wedding weekend, in Port Aransas local time.

    `days_from_wedding` is relative to the wedding itself: -2 is the Friday before,
    0 is the wedding day, +1 the morning after.
    """
    day = WEDDING_DAY + timedelta(days=days_from_wedding)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=CENTRAL)


# The dates production will really run on: invitations go out in October 2027 and
# "RSVP by 15 December 2027" means the end of that day, in Texas.
REAL_OPENS_AT = datetime(2027, 10, 1, 0, 0, tzinfo=CENTRAL)
REAL_DEADLINE = datetime(2027, 12, 16, 0, 0, tzinfo=CENTRAL)

PHASES = ("real", "before-open", "open", "closed")


def rsvp_window(phase: str) -> tuple[datetime | None, datetime | None]:
    """The `rsvp_opens_at` / `rsvp_deadline` pair that puts the event in `phase`.

    A review instance defaults to `open`, because TAP-7738 is not done until a reviewer
    has actually completed an RSVP — and on the real dates the form stays shut until
    October 2027. The other phases are seedable too: both are part of what there is to
    review, and neither is reachable otherwise without waiting a year.
    """
    now = datetime.now(UTC)
    if phase == "real":
        return REAL_OPENS_AT, REAL_DEADLINE
    if phase == "before-open":
        return now + timedelta(days=30), REAL_DEADLINE
    if phase == "open":
        return now - timedelta(days=1), REAL_DEADLINE
    if phase == "closed":
        return now - timedelta(days=60), now - timedelta(days=1)
    raise ValueError(f"unknown RSVP phase {phase!r}; expected one of {', '.join(PHASES)}")


@dataclass(frozen=True)
class SegmentSpec:
    name: str
    description: str
    starts_at: datetime
    location: str
    sort_order: int
    ends_at: datetime | None = None
    is_optional: bool = False
    # Prices for golf and the fishing charter are still unconfirmed, so they stay
    # null rather than being invented.
    price: Decimal | None = None
    booking_url: str | None = None


SEGMENTS: list[SegmentSpec] = [
    SegmentSpec(
        name="Welcome party on the beach",
        description="Drinks and a catered supper on the sand. Come as you are.",
        starts_at=_at(-2, 18),
        ends_at=_at(-2, 21),
        location="Port Aransas beach",
        sort_order=1,
    ),
    SegmentSpec(
        name="Golf at Palmilla Beach",
        description="Optional, paid, and booked directly with the course.",
        starts_at=_at(-1, 8),
        location="Palmilla Beach Golf Course",
        is_optional=True,
        booking_url="https://palmillabeachgolf.com/",
        sort_order=2,
    ),
    SegmentSpec(
        name="Dinner in town and a bar crawl",
        description="Dinner on the strip, then whoever is still standing.",
        starts_at=_at(-1, 19),
        location="Downtown Port Aransas",
        sort_order=3,
    ),
    SegmentSpec(
        name="Bay fishing",
        description="Optional, paid, and booked directly with the charter.",
        starts_at=_at(0, 6, 30),
        location="Fisherman's Wharf",
        is_optional=True,
        booking_url="https://www.fishermanswharfportaransas.com/",
        sort_order=4,
    ),
    SegmentSpec(
        name="Ceremony and reception",
        description="The main event. Catered dinner, cash bar.",
        starts_at=_at(0, 15),
        location="Port Aransas",
        sort_order=5,
    ),
    SegmentSpec(
        name="Departure breakfast",
        description="Catered breakfast before everyone scatters.",
        starts_at=_at(1, 8),
        ends_at=_at(1, 12),
        location="Port Aransas",
        sort_order=6,
    ),
]

# Fictional. See the module docstring.
FAKE_GUESTS: list[tuple[str, int]] = [
    ("Dana Whitfield", 2),
    ("Marcus Ellery", 1),
    ("Priya and Tom Raghunathan", 4),
    ("Eleanor Boyd", 2),
    ("The Calloway family", 5),
]


def review_host(session: Session) -> Host:
    """The account that owns the seeded event. TAP-7726 gave events an owner.

    Reused if it is already there, so re-seeding does not pile up host rows. It has an
    unusable password hash: this is the throwaway review deployment and nobody should
    be able to sign in to it. Register a real host to get a working login.
    """
    existing = session.scalar(select(Host).where(Host.email == REVIEW_HOST_EMAIL))
    if existing is not None:
        return existing
    host = Host(email=REVIEW_HOST_EMAIL, password_hash=UNUSABLE_PASSWORD)
    session.add(host)
    session.flush()
    return host


def seed(session: Session, phase: str = "open") -> Event:
    existing = session.scalar(select(Event).where(Event.slug == SLUG))
    if existing is not None:
        # Cascades to guests, segments, attendees and attendance.
        session.delete(existing)
        session.flush()

    opens_at, deadline = rsvp_window(phase)
    event = Event(
        host_id=review_host(session).id,
        slug=SLUG,
        title="Lisa & Bill",
        host_name="Lisa Gorden and Bill Thornton",
        event_date=WEDDING_DAY,
        location="Port Aransas, Texas",
        details="Four days on Mustang Island. Come for the weekend, or come for the day.",
        timezone="America/Chicago",
        rsvp_opens_at=opens_at,
        rsvp_deadline=deadline,
    )
    session.add(event)
    session.flush()

    for spec in SEGMENTS:
        session.add(
            Segment(
                event_id=event.id,
                name=spec.name,
                description=spec.description,
                starts_at=spec.starts_at,
                ends_at=spec.ends_at,
                location=spec.location,
                is_optional=spec.is_optional,
                price=spec.price,
                booking_url=spec.booking_url,
                sort_order=spec.sort_order,
            )
        )

    for name, party_size in FAKE_GUESTS:
        session.add(Guest(event_id=event.id, name=name, party_size=party_size))

    session.commit()
    return event


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=PHASES,
        default="open",
        help=(
            "which RSVP phase to seed the event into. Defaults to 'open' so a reviewer "
            "can actually complete a reply; 'real' uses the true October 2027 dates."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="prefix for the printed invite links, e.g. a Quick Tunnel URL.",
    )
    args = parser.parse_args()

    base_url = (args.base_url or get_settings().public_base_url).rstrip("/")
    with Session(engine) as session:
        event = seed(session, phase=args.phase)
        guests = list(session.scalars(select(Guest).where(Guest.event_id == event.id)))
        segments = list(session.scalars(select(Segment).where(Segment.event_id == event.id)))

        print(f"Seeded {event.title} ({event.slug}) with {len(segments)} segments.")
        print(
            f"RSVP phase: {args.phase}  (opens {event.rsvp_opens_at}, closes {event.rsvp_deadline})"
        )
        print("Invite links — every one of these guests is invented:")
        for guest in sorted(guests, key=lambda g: g.name):
            print(f"  {guest.name:<32} {base_url}/invites/{guest.invite_token}")


if __name__ == "__main__":
    main()
