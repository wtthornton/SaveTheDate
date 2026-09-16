import secrets
import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, func
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
    rsvp_deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    guests: Mapped[list["Guest"]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class Guest(Base):
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


class Rsvp(Base):
    __tablename__ = "rsvps"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    guest_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("guests.id", ondelete="CASCADE"), unique=True
    )
    attending: Mapped[bool] = mapped_column(Boolean)
    # Seats actually claimed; never more than the guest's party_size.
    party_size: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    responded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    guest: Mapped[Guest] = relationship(back_populates="rsvp")
