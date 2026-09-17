from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.conftest import alembic_config
from tests.factories import add_guest, add_segments, create_event

if TYPE_CHECKING:
    # Importing `app.models` for real at module scope would build the engine before
    # the session fixtures have pointed the settings at the test database.
    from app.models import Host

PREVIOUS_REVISION = "34c3f17d487b"


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_duplicate_slug_is_rejected(client: TestClient) -> None:
    create_event(client)
    response = client.post(
        "/events",
        json={"slug": "bill-and-lisa", "title": "Other", "host_name": "Other"},
    )
    assert response.status_code == 409


def test_unknown_invite_token_is_404(client: TestClient) -> None:
    assert client.get("/api/invites/not-a-real-token").status_code == 404


def test_invite_shows_the_schedule_before_anyone_answers(
    client: TestClient, db_session: Session
) -> None:
    event = create_event(client)
    add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "Jordan Lee")

    body = client.get(f"/api/invites/{guest['invite_token']}").json()

    assert body["guest_name"] == "Jordan Lee"
    assert body["rsvp"] is None
    assert body["phase"] == "open"
    assert [s["name"] for s in body["segments"]] == [
        "Welcome party on the beach",
        "Golf at Palmilla",
        "Ceremony and reception",
    ]
    golf = body["segments"][1]
    assert golf["is_optional"] is True
    assert golf["price"] is None
    assert golf["booking_url"] == "https://example.invalid/golf"


def test_one_person_can_attend_while_their_plus_one_declines(
    client: TestClient, db_session: Session
) -> None:
    event = create_event(client)
    segments = add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "Alex Rivera", party_size=2)

    response = client.put(
        f"/api/invites/{guest['invite_token']}/rsvp",
        json={
            "note": "Sam can't get the Friday off.",
            "attendees": [
                {
                    "name": "Alex Rivera",
                    "attending": True,
                    "attendance": [
                        {"segment_id": segments["welcome"], "attending": True},
                        {"segment_id": segments["ceremony"], "attending": True},
                        {"segment_id": segments["golf"], "attending": False},
                    ],
                },
                {"name": "Sam Doyle", "attending": False, "attendance": []},
            ],
        },
    )
    assert response.status_code == 200, response.text

    attendees = {a["name"]: a for a in response.json()["attendees"]}
    assert attendees["Alex Rivera"]["attending"] is True
    assert attendees["Sam Doyle"]["attending"] is False

    # The Friday headcount is one, the Sunday headcount is one, and golf is nil.
    alex_days = {a["segment_id"]: a["attending"] for a in attendees["Alex Rivera"]["attendance"]}
    assert alex_days[segments["welcome"]] is True
    assert alex_days[segments["ceremony"]] is True
    assert alex_days[segments["golf"]] is False


def test_dietary_needs_are_captured_per_person(client: TestClient, db_session: Session) -> None:
    event = create_event(client)
    segments = add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "Robin Fox", party_size=2)

    response = client.put(
        f"/api/invites/{guest['invite_token']}/rsvp",
        json={
            "attendees": [
                {
                    "name": "Robin Fox",
                    "attending": True,
                    "dietary_tags": ["vegetarian", "nut-allergy"],
                    "dietary_notes": "Severe — no shared fryers, please.",
                    "attendance": [{"segment_id": segments["ceremony"], "attending": True}],
                },
                {
                    "name": "Kit Fox",
                    "attending": True,
                    "is_child": True,
                    "dietary_tags": ["dairy-free"],
                    "attendance": [{"segment_id": segments["ceremony"], "attending": True}],
                },
            ]
        },
    )
    assert response.status_code == 200, response.text

    attendees = {a["name"]: a for a in response.json()["attendees"]}
    assert sorted(attendees["Robin Fox"]["dietary_tags"]) == ["nut-allergy", "vegetarian"]
    assert attendees["Robin Fox"]["dietary_notes"] == "Severe — no shared fryers, please."
    assert attendees["Kit Fox"]["dietary_tags"] == ["dairy-free"]
    assert attendees["Kit Fox"]["is_child"] is True
    assert attendees["Robin Fox"]["is_child"] is False


def test_dietary_tag_outside_the_vocabulary_is_rejected(
    client: TestClient, db_session: Session
) -> None:
    event = create_event(client)
    segments = add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "Robin Fox", party_size=1)

    response = client.put(
        f"/api/invites/{guest['invite_token']}/rsvp",
        json={
            "attendees": [
                {
                    "name": "Robin Fox",
                    "attending": True,
                    "dietary_tags": ["pescatarian"],
                    "attendance": [{"segment_id": segments["ceremony"], "attending": True}],
                }
            ]
        },
    )
    assert response.status_code == 422


def test_rsvp_cannot_exceed_invited_party_size(client: TestClient, db_session: Session) -> None:
    event = create_event(client)
    segments = add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "Casey Kim", party_size=2)

    response = client.put(
        f"/api/invites/{guest['invite_token']}/rsvp",
        json={
            "attendees": [
                {
                    "name": f"Person {n}",
                    "attending": True,
                    "attendance": [{"segment_id": segments["ceremony"], "attending": True}],
                }
                for n in range(3)
            ]
        },
    )
    assert response.status_code == 422
    assert "at most 2" in response.json()["detail"]


def test_attending_person_must_be_coming_to_something(
    client: TestClient, db_session: Session
) -> None:
    event = create_event(client)
    segments = add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "Casey Kim", party_size=1)

    response = client.put(
        f"/api/invites/{guest['invite_token']}/rsvp",
        json={
            "attendees": [
                {
                    "name": "Casey Kim",
                    "attending": True,
                    "attendance": [{"segment_id": segments["ceremony"], "attending": False}],
                }
            ]
        },
    )
    assert response.status_code == 422
    assert "not coming to anything" in response.json()["detail"]


def test_declining_person_cannot_also_be_coming_to_something(
    client: TestClient, db_session: Session
) -> None:
    event = create_event(client)
    segments = add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "Casey Kim", party_size=1)

    response = client.put(
        f"/api/invites/{guest['invite_token']}/rsvp",
        json={
            "attendees": [
                {
                    "name": "Casey Kim",
                    "attending": False,
                    "attendance": [{"segment_id": segments["ceremony"], "attending": True}],
                }
            ]
        },
    )
    assert response.status_code == 422


def test_guest_can_change_their_mind(client: TestClient, db_session: Session) -> None:
    event = create_event(client)
    segments = add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "Jordan Lee", party_size=2)
    token = guest["invite_token"]

    first = client.put(
        f"/api/invites/{token}/rsvp",
        json={
            "attendees": [
                {
                    "name": "Jordan Lee",
                    "attending": True,
                    "attendance": [{"segment_id": segments["ceremony"], "attending": True}],
                }
            ]
        },
    )
    assert first.status_code == 200

    second = client.put(
        f"/api/invites/{token}/rsvp",
        json={
            "note": "So sorry — we can't make it after all.",
            "attendees": [{"name": "Jordan Lee", "attending": False, "attendance": []}],
        },
    )
    assert second.status_code == 200

    body = client.get(f"/api/invites/{token}").json()
    assert len(body["rsvp"]["attendees"]) == 1
    assert body["rsvp"]["attendees"][0]["attending"] is False
    assert body["rsvp"]["note"] == "So sorry — we can't make it after all."


def test_form_is_closed_before_the_rsvp_window_opens(
    client: TestClient, db_session: Session
) -> None:
    opens = datetime.now(UTC) + timedelta(days=30)
    event = create_event(client, rsvp_opens_at=opens)
    segments = add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "Jordan Lee", party_size=1)

    assert client.get(f"/api/invites/{guest['invite_token']}").json()["phase"] == "before_open"

    response = client.put(
        f"/api/invites/{guest['invite_token']}/rsvp",
        json={
            "attendees": [
                {
                    "name": "Jordan Lee",
                    "attending": True,
                    "attendance": [{"segment_id": segments["ceremony"], "attending": True}],
                }
            ]
        },
    )
    assert response.status_code == 403
    assert "not opened yet" in response.json()["detail"]


def test_form_is_live_between_the_open_date_and_the_deadline(
    client: TestClient, db_session: Session
) -> None:
    event = create_event(
        client,
        rsvp_opens_at=datetime.now(UTC) - timedelta(days=1),
        rsvp_deadline=datetime.now(UTC) + timedelta(days=30),
    )
    segments = add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "Jordan Lee", party_size=1)

    assert client.get(f"/api/invites/{guest['invite_token']}").json()["phase"] == "open"

    response = client.put(
        f"/api/invites/{guest['invite_token']}/rsvp",
        json={
            "attendees": [
                {
                    "name": "Jordan Lee",
                    "attending": True,
                    "attendance": [{"segment_id": segments["ceremony"], "attending": True}],
                }
            ]
        },
    )
    assert response.status_code == 200


def test_after_the_deadline_the_answer_is_read_only(
    client: TestClient, db_session: Session
) -> None:
    from app.models import Event

    event = create_event(client, rsvp_deadline=datetime.now(UTC) + timedelta(days=1))
    segments = add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "Jordan Lee", party_size=1)
    token = guest["invite_token"]

    accepted = client.put(
        f"/api/invites/{token}/rsvp",
        json={
            "attendees": [
                {
                    "name": "Jordan Lee",
                    "attending": True,
                    "attendance": [{"segment_id": segments["ceremony"], "attending": True}],
                }
            ]
        },
    )
    assert accepted.status_code == 200

    # The deadline passes.
    stored = db_session.get(Event, uuid.UUID(event["id"]))
    assert stored is not None
    stored.rsvp_deadline = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()

    body = client.get(f"/api/invites/{token}").json()
    assert body["phase"] == "closed"
    # The answer they already gave is still there to read.
    assert body["rsvp"]["attendees"][0]["name"] == "Jordan Lee"

    late = client.put(
        f"/api/invites/{token}/rsvp",
        json={"attendees": [{"name": "Jordan Lee", "attending": False, "attendance": []}]},
    )
    assert late.status_code == 403
    assert "deadline has passed" in late.json()["detail"]


def test_database_rejects_an_attendee_who_attends_nothing(db_session: Session, host: Host) -> None:
    """The consistency rule is enforced by the database, not only by the API."""
    from app.models import Attendee, Event, Guest, Segment

    event = Event(host_id=host.id, slug="trigger-check", title="T", host_name="H", timezone="UTC")
    db_session.add(event)
    db_session.flush()
    db_session.add(
        Segment(
            event_id=event.id,
            name="Ceremony",
            starts_at=datetime(2028, 2, 20, 21, tzinfo=UTC),
        )
    )
    guest = Guest(event_id=event.id, name="Jordan Lee", party_size=1)
    db_session.add(guest)
    db_session.flush()
    db_session.add(Attendee(guest_id=guest.id, name="Jordan Lee", attending=True))

    with pytest.raises(IntegrityError):
        db_session.commit()


def test_database_accepts_a_consistent_attendee(db_session: Session, host: Host) -> None:
    """Negative control for the test above — the trigger is not simply always failing."""
    from app.models import Attendance, Attendee, Event, Guest, Segment

    event = Event(host_id=host.id, slug="trigger-control", title="T", host_name="H", timezone="UTC")
    db_session.add(event)
    db_session.flush()
    segment = Segment(
        event_id=event.id,
        name="Ceremony",
        starts_at=datetime(2028, 2, 20, 21, tzinfo=UTC),
    )
    guest = Guest(event_id=event.id, name="Jordan Lee", party_size=1)
    db_session.add_all([segment, guest])
    db_session.flush()
    attendee = Attendee(guest_id=guest.id, name="Jordan Lee", attending=True)
    db_session.add(attendee)
    db_session.flush()
    db_session.add(Attendance(attendee_id=attendee.id, segment_id=segment.id, attending=True))

    db_session.commit()
    assert attendee.id is not None


def test_migration_never_changes_an_invite_token(engine: Engine) -> None:
    """The constraint the whole design hangs off: sent links keep working."""
    token = "a-token-that-is-already-in-someones-inbox"
    config = alembic_config()

    command.downgrade(config, PREVIOUS_REVISION)
    with engine.begin() as connection:
        event_id = connection.execute(
            text(
                """
                INSERT INTO events (id, slug, title, host_name, rsvp_deadline, created_at)
                VALUES (gen_random_uuid(), 'legacy', 'Legacy', 'Host', DATE '2027-12-15', now())
                RETURNING id
                """
            )
        ).scalar_one()
        guest_id = connection.execute(
            text(
                """
                INSERT INTO guests (id, event_id, name, party_size, invite_token, created_at)
                VALUES (gen_random_uuid(), :event_id, 'Jordan Lee', 2, :token, now())
                RETURNING id
                """
            ),
            {"event_id": event_id, "token": token},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO rsvps (id, guest_id, attending, party_size, responded_at)
                VALUES (gen_random_uuid(), :guest_id, true, 2, now())
                """
            ),
            {"guest_id": guest_id},
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        guest = connection.execute(
            text("SELECT id, invite_token FROM guests WHERE id = :id"), {"id": guest_id}
        ).one()
        attendees = connection.execute(
            text("SELECT name, attending FROM attendees WHERE guest_id = :id ORDER BY name"),
            {"id": guest_id},
        ).all()
        deadline = connection.execute(
            text("SELECT rsvp_deadline FROM events WHERE id = :id"), {"id": event_id}
        ).scalar_one()

    assert guest.invite_token == token
    assert guest.id == guest_id

    # The two seats the old invitation-level RSVP claimed become two people.
    assert [row.name for row in attendees] == ["Jordan Lee", "Jordan Lee (guest 2)"]
    assert all(row.attending for row in attendees)

    # The date deadline becomes the instant the following day starts, in the
    # event's own zone — which backfills as UTC.
    assert deadline == datetime(2027, 12, 16, tzinfo=UTC)
