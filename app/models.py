import secrets
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _token() -> str:
    """URL-safe invite token. 32 bytes keeps guessing infeasible for public invite links."""
    return secrets.token_urlsafe(32)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    host_name: Mapped[str] = mapped_column(String(200))
    # Null while the date is still "save the date, details to follow".
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    # IANA name, e.g. "America/Chicago". timestamptz stores UTC and discards the zone,
    # so the zone the host means is kept here for rendering and for reasoning locally.
    timezone: Mapped[str] = mapped_column(String(64), server_default="UTC")
    # Before this instant the guest sees the save-the-date and no form. Null means
    # "open from the start".
    rsvp_opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The instant the form closes. Null means "no deadline".
    rsvp_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    guests: Mapped[list["Guest"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    segments: Mapped[list["Segment"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", order_by="Segment.sort_order"
    )


class Segment(Base):
    """One item on the schedule — a dinner, the ceremony, an optional golf round.

    Headcounts are per segment, because a Friday number is not a Sunday number.
    """

    __tablename__ = "segments"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Optional items are the paid, externally-booked ones: golf and the fishing charter.
    is_optional: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Nullable: prices were still unconfirmed at design time.
    price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    booking_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    event: Mapped[Event] = relationship(back_populates="segments")


class Guest(Base):
    """One invitation, not one person. Stable: never re-keyed, never re-issued."""

    __tablename__ = "guests"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Seats this invitation covers, e.g. 2 for "Alex + guest".
    party_size: Mapped[int] = mapped_column(Integer, default=1)
    invite_token: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=_token)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    event: Mapped[Event] = relationship(back_populates="guests")
    rsvp: Mapped["Rsvp | None"] = relationship(
        back_populates="guest", cascade="all, delete-orphan", uselist=False
    )
    attendees: Mapped[list["Attendee"]] = relationship(
        back_populates="guest", cascade="all, delete-orphan"
    )


class Attendee(Base):
    """One real person under an invitation.

    Attendance lives here rather than on the invitation so that one person can come
    while their plus-one declines, and so dietary needs are captured per person.
    """

    __tablename__ = "attendees"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    guest_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("guests.id", ondelete="CASCADE"), index=True
    )
    # Filled in by the guest — a "+1" has no name until they answer.
    name: Mapped[str] = mapped_column(String(200))
    # "Coming to anything at all". Kept in step with `attendance` by a deferred
    # constraint trigger; see the TAP-7739 migration.
    attending: Mapped[bool] = mapped_column(Boolean)
    is_child: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    dietary_tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'::text[]")
    )
    dietary_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    guest: Mapped[Guest] = relationship(back_populates="attendees")
    attendance: Mapped[list["Attendance"]] = relationship(
        back_populates="attendee", cascade="all, delete-orphan"
    )


class Attendance(Base):
    """Whether one person is coming to one segment.

    The row carries an explicit boolean rather than meaning "attending" by its
    presence, so "said no to golf" stays distinct from "never answered about golf".
    """

    __tablename__ = "attendance"

    attendee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attendees.id", ondelete="CASCADE"), primary_key=True
    )
    segment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("segments.id", ondelete="CASCADE"), primary_key=True
    )
    attending: Mapped[bool] = mapped_column(Boolean)

    attendee: Mapped[Attendee] = relationship(back_populates="attendance")
    segment: Mapped[Segment] = relationship()


class Rsvp(Base):
    """That an invitation answered at all.

    Thin by design: its existence is what keeps "declined" distinct from "never
    replied". Who is coming, and to what, lives in `attendees` and `attendance`.
    """

    __tablename__ = "rsvps"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    guest_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("guests.id", ondelete="CASCADE"), unique=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    responded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    guest: Mapped[Guest] = relationship(back_populates="rsvp")


class Host(Base):
    """Someone who runs an event. The only account in this system.

    Guests deliberately have no row here and never will: the invite token is their
    whole credential, and requiring anything more of an older relative is the one
    advantage this has over Joy, Zola and Minted. TAP-7725.
    """

    __tablename__ = "hosts"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Stored casefolded, because people do not type their own address consistently and
    # a second account created by a stray capital is a confusing way to lose an event.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    # An argon2id digest, which carries its own parameters and salt inline.
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sessions: Mapped[list["HostSession"]] = relationship(
        back_populates="host", cascade="all, delete-orphan"
    )


class HostSession(Base):
    """One logged-in browser.

    Server-side rather than a signed cookie, so signing out actually ends the session
    and a stolen cookie can be revoked. The host UI is server-rendered Jinja like the
    guest pages, so a cookie is the natural carrier; a bearer token would drag
    JavaScript into a stack that deliberately has none.
    """

    __tablename__ = "host_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    host_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"))
    # sha256 of the cookie value, hex. The raw token exists only in the cookie, so a
    # dump of this table hands nobody a live session — the same reasoning that would
    # apply to `guests.invite_token` if that token were not, by design, the credential.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    host: Mapped[Host] = relationship(back_populates="sessions")
