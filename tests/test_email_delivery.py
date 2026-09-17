"""TAP-7731 — emailing the guest list, and knowing what happened to each one.

Nothing in this file opens a socket. The transport is a Protocol and the tests use
`RecordingTransport`, so the send path is exercised end to end with no network and no
provider account — which is the issue's own "done when".

The point of the whole feature is the second half: **a silently bounced invite looks
exactly like a guest who ignored it.** One of those is the host's problem to fix and
the other is not, so the outcome is recorded per guest rather than assumed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.factories import add_guest, add_segments, create_event

if TYPE_CHECKING:
    # `app.models` and `app.mail` both reach `app.db`, which builds the engine at
    # import time — the same ordering trap as everywhere else in this suite.
    from app.mail import RecordingTransport
    from app.models import Delivery

WEBHOOK_SECRET = "a shared secret for the webhook"
BASE = "https://invite.example.com"


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[RecordingTransport]:
    """The app's transport, replaced with one that records instead of sending.

    Patched at `app.mail.build_transport` rather than injected, because the route
    looks the transport up per request — which is what lets a deployment change
    provider without a restart.
    """
    from app import mail

    recorder = mail.RecordingTransport()
    monkeypatch.setattr(mail, "build_transport", lambda: recorder)
    yield recorder


@pytest.fixture
def webhook_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    from app.config import get_settings

    monkeypatch.setenv("EMAIL_WEBHOOK_SECRET", WEBHOOK_SECRET)
    get_settings.cache_clear()
    yield WEBHOOK_SECRET
    get_settings.cache_clear()


def _event_with_guests(client: TestClient, db_session: Session) -> str:
    event = create_event(client)
    add_segments(db_session, event["id"])
    add_guest(client, event["id"], "Dana Whitfield")
    add_guest(client, event["id"], "Marcus Ellery")
    return str(event["id"])


def _deliveries(db_session: Session) -> list[Delivery]:
    from app.models import Delivery as DeliveryModel

    db_session.expire_all()
    return list(db_session.scalars(select(DeliveryModel)))


# -- Composing ------------------------------------------------------------


def test_the_email_carries_the_guests_own_link(client: TestClient, db_session: Session) -> None:
    from app.invitations import INVITE, compose
    from app.models import Event, Guest

    event_id = _event_with_guests(client, db_session)
    event = db_session.get(Event, uuid.UUID(event_id))
    guest = db_session.scalars(select(Guest).where(Guest.event_id == uuid.UUID(event_id))).first()
    assert event is not None and guest is not None

    message = compose(event, guest, BASE, INVITE)

    assert guest.invite_token in message.text
    assert guest.invite_token in message.html
    assert guest.name in message.text


def test_the_link_appears_as_plain_text_as_well_as_a_hyperlink(
    client: TestClient, db_session: Session
) -> None:
    """Mail clients strip and rewrite anchors. A visible URL still gets a guest there."""
    from app.invitations import INVITE, compose
    from app.models import Event, Guest

    event_id = _event_with_guests(client, db_session)
    event = db_session.get(Event, uuid.UUID(event_id))
    guest = db_session.scalars(select(Guest).where(Guest.event_id == uuid.UUID(event_id))).first()
    assert event is not None and guest is not None

    html = compose(event, guest, BASE, INVITE).html
    url = f"{BASE}/invites/{guest.invite_token}"

    assert f'href="{url}"' in html
    assert html.count(url) >= 2, "the URL should also be readable, not only clickable"


# -- Sending --------------------------------------------------------------


def test_sending_reaches_every_guest_with_an_address(
    client: TestClient, db_session: Session, transport: RecordingTransport
) -> None:
    event_id = _event_with_guests(client, db_session)

    response = client.post(f"/host/events/{event_id}/send", follow_redirects=False)

    assert response.status_code == 303
    assert len(transport.sent) == 2
    assert {message.to for message in transport.sent} == {"guest@example.com"}


def test_a_guest_with_no_address_is_skipped_not_failed(
    client: TestClient, db_session: Session, transport: RecordingTransport
) -> None:
    from app.models import Guest

    event = create_event(client)
    db_session.add(
        Guest(event_id=uuid.UUID(str(event["id"])), name="No Email", email=None, party_size=1)
    )
    db_session.commit()

    client.post(f"/host/events/{event['id']}/send", follow_redirects=False)

    assert transport.sent == []
    assert _deliveries(db_session) == [], "a guest with no address gets no delivery row"


def test_sending_twice_does_not_send_twice(
    client: TestClient, db_session: Session, transport: RecordingTransport
) -> None:
    """Pressing the button again is a thing a host will do. It must be safe."""
    event_id = _event_with_guests(client, db_session)

    client.post(f"/host/events/{event_id}/send", follow_redirects=False)
    client.post(f"/host/events/{event_id}/send", follow_redirects=False)

    assert len(transport.sent) == 2, "the second send should have found nobody to send to"


def test_each_send_is_recorded_against_the_guest(
    client: TestClient, db_session: Session, transport: RecordingTransport
) -> None:
    event_id = _event_with_guests(client, db_session)

    client.post(f"/host/events/{event_id}/send", follow_redirects=False)

    rows = _deliveries(db_session)
    assert len(rows) == 2
    assert {row.status for row in rows} == {"sent"}
    assert all(row.provider_message_id for row in rows)


def test_one_refused_address_does_not_stop_the_rest(
    client: TestClient, db_session: Session, transport: RecordingTransport
) -> None:
    """A provider refusing one guest must not cost the other forty-nine their invite."""
    from app.models import Guest

    event = create_event(client)
    db_session.add_all(
        [
            Guest(event_id=uuid.UUID(str(event["id"])), name="Bad", email="bad@example.com"),
            Guest(event_id=uuid.UUID(str(event["id"])), name="Good", email="good@example.com"),
        ]
    )
    db_session.commit()
    transport.fail_for.add("bad@example.com")

    client.post(f"/host/events/{event['id']}/send", follow_redirects=False)

    rows = {row.guest.name: row for row in _deliveries(db_session)}
    assert rows["Good"].status == "sent"
    assert rows["Bad"].status == "failed"
    assert rows["Bad"].error, "a failure must say why"


def test_a_failure_is_shown_on_the_dashboard(
    client: TestClient, db_session: Session, transport: RecordingTransport
) -> None:
    """The whole point: a bounce a host cannot see is the same as no bounce at all."""
    from app.models import Guest

    event = create_event(client)
    db_session.add(Guest(event_id=uuid.UUID(str(event["id"])), name="Bad", email="bad@example.com"))
    db_session.commit()
    transport.fail_for.add("bad@example.com")

    client.post(f"/host/events/{event['id']}/send", follow_redirects=False)
    page = client.get(f"/host/events/{event['id']}")

    assert "could not send" in page.text


def test_the_dashboard_says_when_nothing_has_been_sent(
    client: TestClient, db_session: Session
) -> None:
    event_id = _event_with_guests(client, db_session)

    assert "not sent" in client.get(f"/host/events/{event_id}").text


# -- Reminders ------------------------------------------------------------


def test_a_reminder_goes_only_to_people_who_have_not_replied(
    client: TestClient,
    anonymous_client: TestClient,
    db_session: Session,
    transport: RecordingTransport,
) -> None:
    """Chasing somebody who already declined is the rudest thing this could do."""
    from app.models import Guest

    event = create_event(client)
    segments = add_segments(db_session, event["id"])
    replied = add_guest(client, event["id"], "Has Replied", party_size=1)
    add_guest(client, event["id"], "Has Not Replied", party_size=1)

    anonymous_client.put(
        f"/api/invites/{replied['invite_token']}/rsvp",
        json={
            "note": None,
            "attendees": [
                {
                    "name": "Has Replied",
                    "attending": False,
                    "is_child": False,
                    "dietary_tags": [],
                    "dietary_notes": None,
                    "attendance": [
                        {"segment_id": sid, "attending": False} for sid in segments.values()
                    ],
                }
            ],
        },
    )

    client.post(
        f"/host/events/{event['id']}/send", data={"kind": "reminder"}, follow_redirects=False
    )

    names = {db_session.scalars(select(Guest).where(Guest.name == "Has Not Replied")).one().name}
    assert len(transport.sent) == 1, "only the silent guest should be chased"
    assert names == {"Has Not Replied"}


def test_a_reminder_can_be_sent_after_an_invite(
    client: TestClient, db_session: Session, transport: RecordingTransport
) -> None:
    """Unlike a second invite, a reminder is a different kind and is allowed."""
    event_id = _event_with_guests(client, db_session)

    client.post(f"/host/events/{event_id}/send", follow_redirects=False)
    client.post(f"/host/events/{event_id}/send", data={"kind": "reminder"}, follow_redirects=False)

    assert len(transport.sent) == 4
    kinds = sorted(row.kind for row in _deliveries(db_session))
    assert kinds == ["invite", "invite", "reminder", "reminder"]


# -- Bounces and complaints -----------------------------------------------


def _signed(body: dict[str, object], secret: str) -> tuple[bytes, str]:
    raw = json.dumps(body).encode()
    return raw, hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def test_a_bounce_is_recorded_against_the_guest(
    client: TestClient,
    anonymous_client: TestClient,
    db_session: Session,
    transport: RecordingTransport,
    webhook_secret: str,
) -> None:
    event_id = _event_with_guests(client, db_session)
    client.post(f"/host/events/{event_id}/send", follow_redirects=False)
    message_id = _deliveries(db_session)[0].provider_message_id

    raw, signature = _signed(
        {"type": "email.bounced", "data": {"email_id": message_id}}, webhook_secret
    )
    response = anonymous_client.post(
        "/webhooks/email",
        content=raw,
        headers={"x-savethedate-signature": signature, "content-type": "application/json"},
    )

    assert response.status_code == 204
    bounced = [row for row in _deliveries(db_session) if row.status == "bounced"]
    assert len(bounced) == 1
    assert "bounced" in client.get(f"/host/events/{event_id}").text


def test_a_complaint_is_recorded(
    client: TestClient,
    anonymous_client: TestClient,
    db_session: Session,
    transport: RecordingTransport,
    webhook_secret: str,
) -> None:
    event_id = _event_with_guests(client, db_session)
    client.post(f"/host/events/{event_id}/send", follow_redirects=False)
    message_id = _deliveries(db_session)[0].provider_message_id

    raw, signature = _signed(
        {"type": "email.complained", "data": {"email_id": message_id}}, webhook_secret
    )
    anonymous_client.post(
        "/webhooks/email",
        content=raw,
        headers={"x-savethedate-signature": signature, "content-type": "application/json"},
    )

    assert any(row.status == "complained" for row in _deliveries(db_session))


def test_an_unsigned_callback_is_refused(anonymous_client: TestClient, webhook_secret: str) -> None:
    """The endpoint changes delivery state, so it cannot be open to the internet."""
    raw, _signature = _signed({"type": "email.bounced", "data": {"email_id": "x"}}, "wrong")

    response = anonymous_client.post(
        "/webhooks/email", content=raw, headers={"content-type": "application/json"}
    )

    assert response.status_code == 401


def test_a_wrongly_signed_callback_is_refused(
    anonymous_client: TestClient, webhook_secret: str
) -> None:
    raw, signature = _signed({"type": "email.bounced", "data": {"email_id": "x"}}, "not it")

    response = anonymous_client.post(
        "/webhooks/email",
        content=raw,
        headers={"x-savethedate-signature": signature, "content-type": "application/json"},
    )

    assert response.status_code == 401


def test_the_webhook_refuses_everything_when_no_secret_is_configured(
    anonymous_client: TestClient,
) -> None:
    """Unset must mean closed. An open endpoint that rewrites delivery state is worse
    than no endpoint."""
    from app.config import get_settings

    get_settings.cache_clear()
    raw, signature = _signed({"type": "email.bounced", "data": {"email_id": "x"}}, "")

    response = anonymous_client.post(
        "/webhooks/email",
        content=raw,
        headers={"x-savethedate-signature": signature, "content-type": "application/json"},
    )

    assert response.status_code == 401


def test_an_unknown_message_id_is_accepted_quietly(
    anonymous_client: TestClient, webhook_secret: str
) -> None:
    """A provider that gets an error retries, and there is nothing here to retry."""
    raw, signature = _signed(
        {"type": "email.bounced", "data": {"email_id": "never-heard-of-it"}}, webhook_secret
    )

    response = anonymous_client.post(
        "/webhooks/email",
        content=raw,
        headers={"x-savethedate-signature": signature, "content-type": "application/json"},
    )

    assert response.status_code == 204


# -- Nothing here touches the network -------------------------------------


def test_the_default_transport_does_not_send_anywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfigured deployment must print, not silently succeed and not mail people."""
    from app.config import get_settings
    from app.mail import ConsoleTransport, build_transport

    get_settings.cache_clear()
    assert isinstance(build_transport(), ConsoleTransport)

    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    get_settings.cache_clear()
    assert isinstance(build_transport(), ConsoleTransport), (
        "resend with no API key must not become a live transport"
    )
    get_settings.cache_clear()


def test_sending_needs_a_session(anonymous_client: TestClient, client: TestClient) -> None:
    event = create_event(client)

    assert (
        anonymous_client.post(
            f"/host/events/{event['id']}/send", follow_redirects=False
        ).status_code
        == 401
    )
