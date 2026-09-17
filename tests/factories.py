"""Shared setup for the API and guest-page suites."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


def create_event(
    client: TestClient,
    *,
    slug: str = "bill-and-lisa",
    timezone: str = "America/Chicago",
    event_date: str | None = "2028-02-13",
    # A `str` goes to the API verbatim, so a test can post the bare calendar date or
    # the naive value a host might type and see what the API makes of it. TAP-7729.
    rsvp_opens_at: datetime | str | None = None,
    rsvp_deadline: datetime | str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "slug": slug,
        "title": "Lisa & Bill",
        "host_name": "Lisa Gorden and Bill Thornton",
        "event_date": event_date,
        "location": "Port Aransas, TX",
        "timezone": timezone,
    }
    if rsvp_opens_at is not None:
        payload["rsvp_opens_at"] = (
            rsvp_opens_at if isinstance(rsvp_opens_at, str) else rsvp_opens_at.isoformat()
        )
    if rsvp_deadline is not None:
        payload["rsvp_deadline"] = (
            rsvp_deadline if isinstance(rsvp_deadline, str) else rsvp_deadline.isoformat()
        )

    response = client.post("/events", json=payload)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def add_guest(client: TestClient, event_id: str, name: str, party_size: int = 2) -> dict[str, Any]:
    response = client.post(
        f"/events/{event_id}/guests",
        json={"name": name, "email": "guest@example.com", "party_size": party_size},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def add_segments(db_session: Session, event_id: str) -> dict[str, str]:
    """The parts of the weekend a guest answers separately.

    There is no host-facing endpoint for these yet — that belongs to TAP-7730 — so
    the fixture writes them directly.
    """
    from app.models import Segment

    welcome = Segment(
        event_id=uuid.UUID(event_id),
        name="Welcome party on the beach",
        starts_at=datetime(2028, 2, 12, 0, tzinfo=UTC),
        ends_at=datetime(2028, 2, 12, 3, tzinfo=UTC),
        location="Port Aransas beach",
        sort_order=1,
    )
    golf = Segment(
        event_id=uuid.UUID(event_id),
        name="Golf at Palmilla",
        starts_at=datetime(2028, 2, 12, 15, tzinfo=UTC),
        is_optional=True,
        booking_url="https://example.invalid/golf",
        sort_order=2,
    )
    ceremony = Segment(
        event_id=uuid.UUID(event_id),
        name="Ceremony and reception",
        starts_at=datetime(2028, 2, 13, 21, tzinfo=UTC),
        sort_order=3,
    )
    db_session.add_all([welcome, golf, ceremony])
    db_session.commit()
    return {
        "welcome": str(welcome.id),
        "golf": str(golf.id),
        "ceremony": str(ceremony.id),
    }
