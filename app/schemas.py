import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class DietaryTag(StrEnum):
    """Fixed vocabulary so the caterer's counts stay clean.

    Anything outside this list belongs in `dietary_notes`.
    """

    VEGETARIAN = "vegetarian"
    VEGAN = "vegan"
    GLUTEN_FREE = "gluten-free"
    NUT_ALLERGY = "nut-allergy"
    SHELLFISH_ALLERGY = "shellfish-allergy"
    KOSHER = "kosher"
    HALAL = "halal"
    DAIRY_FREE = "dairy-free"


# How each tag is written on the guest form. The stored value stays machine-readable
# so the caterer's counts group cleanly; only the wording here is for people.
DIETARY_LABELS: dict[DietaryTag, str] = {
    DietaryTag.VEGETARIAN: "Vegetarian",
    DietaryTag.VEGAN: "Vegan",
    DietaryTag.GLUTEN_FREE: "Gluten-free",
    DietaryTag.NUT_ALLERGY: "Nut allergy",
    DietaryTag.SHELLFISH_ALLERGY: "Shellfish allergy",
    DietaryTag.DAIRY_FREE: "Dairy-free",
    DietaryTag.KOSHER: "Kosher",
    DietaryTag.HALAL: "Halal",
}


RsvpPhase = Literal["before_open", "open", "closed"]


class EventCreate(BaseModel):
    slug: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1, max_length=200)
    host_name: str = Field(min_length=1, max_length=200)
    event_date: date | None = None
    location: str | None = Field(default=None, max_length=300)
    details: str | None = None
    timezone: str = Field(default="UTC", max_length=64)
    rsvp_opens_at: datetime | None = None
    rsvp_deadline: datetime | None = None

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA time zone: {value!r}") from exc
        return value


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    title: str
    host_name: str
    event_date: date | None
    location: str | None
    details: str | None
    timezone: str
    rsvp_opens_at: datetime | None
    rsvp_deadline: datetime | None


class SegmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    starts_at: datetime
    ends_at: datetime | None
    location: str | None
    is_optional: bool
    price: Decimal | None
    booking_url: str | None
    sort_order: int


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


class AttendanceIn(BaseModel):
    segment_id: uuid.UUID
    attending: bool


class AttendanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    segment_id: uuid.UUID
    attending: bool


class AttendeeIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    attending: bool
    is_child: bool = False
    dietary_tags: list[DietaryTag] = Field(default_factory=list)
    dietary_notes: str | None = None
    attendance: list[AttendanceIn] = Field(default_factory=list)


class AttendeeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    attending: bool
    is_child: bool
    dietary_tags: list[str]
    dietary_notes: str | None
    attendance: list[AttendanceOut]


class RsvpCreate(BaseModel):
    note: str | None = None
    attendees: list[AttendeeIn] = Field(default_factory=list)


class RsvpOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    note: str | None
    responded_at: datetime
    attendees: list[AttendeeOut]


class InviteOut(BaseModel):
    """What a guest sees when they open their invite link."""

    event: EventOut
    guest_name: str
    party_size: int
    phase: RsvpPhase
    segments: list[SegmentOut]
    rsvp: RsvpOut | None
