"""TAP-7729 — the RSVP window, evaluated in the event's own zone.

Two defects are covered here, both invisible to the existing suite because it built
its windows as `now() ± timedelta` and so never sat on a boundary.

1. `phase()` read `datetime.now(UTC)` internally, so the instant either side of an
   open date or a deadline could not be asserted at all. The clock is now a FastAPI
   dependency, which these tests override.

2. `EventCreate` accepted a naive `rsvp_deadline`. Written to a `timestamptz`, a naive
   value is interpreted in the Postgres session's zone — the server's day, not the
   event's — which is the precise failure the issue was filed against.

**The choice, written down as the issue asks.** A bare calendar date is accepted and
converted using `events.timezone`: `"2027-12-15"` as a deadline means the end of
December 15 where the wedding is, stored as midnight at the start of the 16th. That
exclusive upper bound is what `deadline_date()` in `app/templating.py` already assumes
when it renders the date back to a guest. A *naive datetime* is rejected outright,
because unlike a bare date it looks precise while carrying no zone at all.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.factories import add_guest, add_segments, create_event

CHICAGO = ZoneInfo("America/Chicago")
A_SECOND = timedelta(seconds=1)

# The real window from the plan: RSVPs open October 1 2027 and close at the end of
# December 15 2027, both in Port Aransas. The deadline is held as midnight at the
# start of the 16th, Central, because the stored bound is exclusive.
OPENS_LOCAL_DAY = "2027-10-01"
OPENS_INSTANT = datetime(2027, 10, 1, 0, 0, tzinfo=CHICAGO)
DEADLINE_LOCAL_DAY = "2027-12-15"
DEADLINE_INSTANT = datetime(2027, 12, 16, 0, 0, tzinfo=CHICAGO)


class FrozenClock:
    """The instant the routes see, pinned from the first `set()` onwards.

    Deliberately lazy. The same clock dependency also decides whether a host's session
    has expired, so installing the override up front would run this file's setup —
    creating the event and the guest, both host endpoints — at a time years after the
    session cookie was issued, and every test would 401 before it asserted anything.
    Setup therefore runs on the real clock, and time only stops once a test says so.
    The routes under test here are public, so no session is consulted after that.
    """

    def __init__(self, install: Callable[[Callable[[], datetime]], None]) -> None:
        self.moment = datetime(2027, 11, 1, 12, 0, tzinfo=UTC)
        self._install = install

    def set(self, moment: datetime) -> None:
        self.moment = moment
        self._install(lambda: self.moment)


@pytest.fixture
def frozen(client: TestClient) -> Iterator[FrozenClock]:
    # Imported inside the fixture, as `conftest.client` does: importing `app.main` at
    # module scope would build the settings before the session fixture has pointed
    # them at the test database.
    from app import clock
    from app.main import app

    def install(reader: Callable[[], datetime]) -> None:
        app.dependency_overrides[clock.now] = reader

    yield FrozenClock(install)
    app.dependency_overrides.pop(clock.now, None)


def _event_with_window(
    client: TestClient,
    db_session: Session,
    *,
    opens_at: str | None = OPENS_LOCAL_DAY,
    deadline: str | None = DEADLINE_LOCAL_DAY,
) -> tuple[dict[str, str], str]:
    event = create_event(
        client,
        timezone="America/Chicago",
        rsvp_opens_at=opens_at,
        rsvp_deadline=deadline,
    )
    segments = add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "Jordan Lee", party_size=1)
    return segments, str(guest["invite_token"])


def _answer(segments: dict[str, str]) -> dict[str, Any]:
    return {
        "note": None,
        "attendees": [
            {
                "name": "Jordan Lee",
                "attending": True,
                "is_child": False,
                "dietary_tags": [],
                "dietary_notes": None,
                "attendance": [
                    {"segment_id": segment_id, "attending": True}
                    for segment_id in segments.values()
                ],
            }
        ],
    }


# -- A local date becomes an instant in the event's zone -------------------


def test_a_bare_deadline_date_is_stored_as_the_end_of_that_day(client: TestClient) -> None:
    event = create_event(client, timezone="America/Chicago", rsvp_deadline=DEADLINE_LOCAL_DAY)

    stored = datetime.fromisoformat(str(event["rsvp_deadline"]))
    assert stored == DEADLINE_INSTANT
    # Central is UTC-6 in December, so the last moment to reply is 06:00Z on the 16th.
    assert stored.astimezone(UTC) == datetime(2027, 12, 16, 6, 0, tzinfo=UTC)


def test_a_bare_open_date_is_stored_as_the_start_of_that_day(client: TestClient) -> None:
    event = create_event(client, timezone="America/Chicago", rsvp_opens_at=OPENS_LOCAL_DAY)

    assert datetime.fromisoformat(str(event["rsvp_opens_at"])) == OPENS_INSTANT


def test_the_same_date_in_a_different_zone_is_a_different_instant(client: TestClient) -> None:
    """The whole point of reading `events.timezone`: the day is local, not the server's."""
    central = create_event(
        client, slug="central", timezone="America/Chicago", rsvp_deadline=DEADLINE_LOCAL_DAY
    )
    tokyo = create_event(
        client, slug="tokyo", timezone="Asia/Tokyo", rsvp_deadline=DEADLINE_LOCAL_DAY
    )

    assert datetime.fromisoformat(str(central["rsvp_deadline"])) != datetime.fromisoformat(
        str(tokyo["rsvp_deadline"])
    )


def test_a_naive_deadline_is_refused_rather_than_guessed_at(client: TestClient) -> None:
    """`2027-12-15T23:59:59` looks precise and carries no zone. That is the trap."""
    response = client.post(
        "/events",
        json={
            "slug": "naive-deadline",
            "title": "Lisa & Bill",
            "host_name": "Lisa Gorden and Bill Thornton",
            "timezone": "America/Chicago",
            "rsvp_deadline": "2027-12-15T23:59:59",
        },
    )

    assert response.status_code == 422
    assert "zone" in response.text.lower()


def test_a_naive_open_date_is_refused_too(client: TestClient) -> None:
    response = client.post(
        "/events",
        json={
            "slug": "naive-open",
            "title": "Lisa & Bill",
            "host_name": "Lisa Gorden and Bill Thornton",
            "timezone": "America/Chicago",
            "rsvp_opens_at": "2027-10-01T09:00:00",
        },
    )

    assert response.status_code == 422
    assert "zone" in response.text.lower()


def test_an_aware_deadline_is_kept_exactly_as_given(client: TestClient) -> None:
    given = "2027-12-15T23:59:59-06:00"
    event = create_event(client, timezone="America/Chicago", rsvp_deadline=given)

    assert datetime.fromisoformat(str(event["rsvp_deadline"])) == datetime.fromisoformat(given)


# -- The boundaries, to the second ----------------------------------------


def test_the_instant_before_the_deadline_still_accepts_a_reply(
    client: TestClient, db_session: Session, frozen: FrozenClock
) -> None:
    segments, token = _event_with_window(client, db_session)
    frozen.set(DEADLINE_INSTANT.astimezone(UTC) - A_SECOND)

    assert client.get(f"/api/invites/{token}").json()["phase"] == "open"
    assert client.put(f"/api/invites/{token}/rsvp", json=_answer(segments)).status_code == 200


def test_the_deadline_instant_itself_is_closed(
    client: TestClient, db_session: Session, frozen: FrozenClock
) -> None:
    """The stored bound is exclusive: midnight on the 16th is already too late."""
    segments, token = _event_with_window(client, db_session)
    frozen.set(DEADLINE_INSTANT.astimezone(UTC))

    assert client.get(f"/api/invites/{token}").json()["phase"] == "closed"
    refused = client.put(f"/api/invites/{token}/rsvp", json=_answer(segments))
    assert refused.status_code == 403
    assert refused.json()["detail"] == "the RSVP deadline has passed"


def test_the_instant_before_the_open_date_is_refused(
    client: TestClient, db_session: Session, frozen: FrozenClock
) -> None:
    segments, token = _event_with_window(client, db_session)
    frozen.set(OPENS_INSTANT.astimezone(UTC) - A_SECOND)

    assert client.get(f"/api/invites/{token}").json()["phase"] == "before_open"
    refused = client.put(f"/api/invites/{token}/rsvp", json=_answer(segments))
    assert refused.status_code == 403
    assert refused.json()["detail"] == "RSVPs have not opened yet"


def test_the_open_instant_itself_accepts_a_reply(
    client: TestClient, db_session: Session, frozen: FrozenClock
) -> None:
    """The lower bound is inclusive, so the first second of the day works."""
    segments, token = _event_with_window(client, db_session)
    frozen.set(OPENS_INSTANT.astimezone(UTC))

    assert client.get(f"/api/invites/{token}").json()["phase"] == "open"
    assert client.put(f"/api/invites/{token}/rsvp", json=_answer(segments)).status_code == 200


def test_a_deadline_is_judged_on_the_events_day_not_the_servers(
    client: TestClient, db_session: Session, frozen: FrozenClock
) -> None:
    """05:00Z on December 16 is still December 15 in Port Aransas, so replies stand.

    Read against `datetime.now(UTC)` and the raw column — the behavior before this
    issue — the UTC date has already rolled over and this guest would be turned away
    with six hours of their deadline left.
    """
    segments, token = _event_with_window(client, db_session)
    frozen.set(datetime(2027, 12, 16, 5, 0, tzinfo=UTC))

    assert datetime(2027, 12, 16, 5, 0, tzinfo=UTC).astimezone(CHICAGO).day == 15
    assert client.get(f"/api/invites/{token}").json()["phase"] == "open"
    assert client.put(f"/api/invites/{token}/rsvp", json=_answer(segments)).status_code == 200


# -- The two open-ended cases, each named ---------------------------------


def test_an_event_with_no_deadline_accepts_replies_indefinitely(
    client: TestClient, db_session: Session, frozen: FrozenClock
) -> None:
    segments, token = _event_with_window(client, db_session, deadline=None)
    frozen.set(datetime(2099, 1, 1, tzinfo=UTC))

    assert client.get(f"/api/invites/{token}").json()["phase"] == "open"
    assert client.put(f"/api/invites/{token}/rsvp", json=_answer(segments)).status_code == 200


def test_an_event_with_no_open_date_accepts_replies_immediately(
    client: TestClient, db_session: Session, frozen: FrozenClock
) -> None:
    segments, token = _event_with_window(client, db_session, opens_at=None)
    frozen.set(datetime(2026, 1, 1, tzinfo=UTC))

    assert client.get(f"/api/invites/{token}").json()["phase"] == "open"
    assert client.put(f"/api/invites/{token}/rsvp", json=_answer(segments)).status_code == 200
