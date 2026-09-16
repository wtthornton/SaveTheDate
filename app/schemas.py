import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class EventCreate(BaseModel):
    slug: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1, max_length=200)
    host_name: str = Field(min_length=1, max_length=200)
    event_date: date | None = None
    location: str | None = Field(default=None, max_length=300)
    details: str | None = None
    rsvp_deadline: date | None = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    title: str
    host_name: str
    event_date: date | None
    location: str | None
    details: str | None
    rsvp_deadline: date | None


class GuestCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr | None = None
    party_size: int = Field(default=1, ge=1, le=20)


class GuestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: EmailStr | None
    party_size: int
    invite_token: str


class RsvpCreate(BaseModel):
    attending: bool
    party_size: int = Field(default=1, ge=0, le=20)
    note: str | None = None


class RsvpOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attending: bool
    party_size: int
    note: str | None


class InviteOut(BaseModel):
    """What a guest sees when they open their invite link."""

    event: EventOut
    guest_name: str
    party_size: int
    rsvp: RsvpOut | None
