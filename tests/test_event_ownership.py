"""TAP-7726 — an event belongs to the host who created it.

TAP-7725 proved *who* is calling. It did not limit *what they may touch*: any signed-in
host could read or modify any event, because `events` had no owner. With one host that
is theoretical; the moment there are two it is the guest list of somebody else's
wedding, invite tokens included.

Another host's event answers **404, not 403**. A 403 confirms the event exists, which
turns the id and the slug into something worth enumerating. The two cases — "no such
event" and "not yours" — are deliberately indistinguishable, and there is a test that
compares the two responses byte for byte.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from tests.factories import add_guest, create_event

OTHER_EMAIL = "second.host@example.com"
OTHER_PASSWORD = "an entirely different password"


@pytest.fixture
def other_client(_clean_tables: None, engine: Engine) -> Iterator[TestClient]:
    """A second signed-in host, with their own cookie jar and no events of their own."""
    from app.auth import hash_password
    from app.main import app
    from app.models import Host

    with Session(engine) as session:
        session.add(Host(email=OTHER_EMAIL, password_hash=hash_password(OTHER_PASSWORD)))
        session.commit()

    with TestClient(app) as test_client:
        response = test_client.post(
            "/auth/login", json={"email": OTHER_EMAIL, "password": OTHER_PASSWORD}
        )
        assert response.status_code == 200, response.text
        yield test_client


# -- The event carries its owner ------------------------------------------


def test_creating_an_event_records_who_created_it(client: TestClient, engine: Engine) -> None:
    from app.models import Event, Host

    event = create_event(client)

    with Session(engine) as session:
        row = session.scalars(select(Event)).one()
        owner = session.get(Host, row.host_id)

    assert owner is not None
    assert str(row.id) == event["id"]
    assert owner.email == "host@example.com"


# -- Another host's event does not exist, as far as they are concerned ----


def test_another_host_cannot_read_the_event(client: TestClient, other_client: TestClient) -> None:
    event = create_event(client)

    assert other_client.get(f"/events/{event['slug']}").status_code == 404


def test_another_host_cannot_list_the_guests(client: TestClient, other_client: TestClient) -> None:
    """The guest list is the invite tokens. This is the one that actually matters."""
    event = create_event(client)
    guest = add_guest(client, event["id"], "Jordan Lee")

    response = other_client.get(f"/events/{event['id']}/guests")

    assert response.status_code == 404
    assert str(guest["invite_token"]) not in response.text


def test_another_host_cannot_add_a_guest(client: TestClient, other_client: TestClient) -> None:
    event = create_event(client)

    response = other_client.post(
        f"/events/{event['id']}/guests", json={"name": "Gatecrasher", "party_size": 1}
    )

    assert response.status_code == 404


def test_not_yours_is_indistinguishable_from_does_not_exist(
    client: TestClient, other_client: TestClient
) -> None:
    """A 403 here would confirm the event exists, which is what makes ids worth guessing."""
    event = create_event(client)
    missing = "00000000-0000-0000-0000-000000000000"

    not_yours = other_client.get(f"/events/{event['id']}/guests")
    never_existed = other_client.get(f"/events/{missing}/guests")

    assert not_yours.status_code == never_existed.status_code == 404
    assert not_yours.json() == never_existed.json()


# -- The owner is unaffected ----------------------------------------------


def test_the_owning_host_still_has_full_access(client: TestClient) -> None:
    event = create_event(client)
    guest = add_guest(client, event["id"], "Jordan Lee")

    assert client.get(f"/events/{event['slug']}").status_code == 200

    listed = client.get(f"/events/{event['id']}/guests")
    assert listed.status_code == 200
    assert listed.json()[0]["invite_token"] == guest["invite_token"]


def test_two_hosts_keep_separate_events(client: TestClient, other_client: TestClient) -> None:
    mine = create_event(client, slug="mine")
    theirs = create_event(other_client, slug="theirs")

    add_guest(client, mine["id"], "My Guest")
    add_guest(other_client, theirs["id"], "Their Guest")

    assert [row["name"] for row in client.get(f"/events/{mine['id']}/guests").json()] == [
        "My Guest"
    ]
    assert [row["name"] for row in other_client.get(f"/events/{theirs['id']}/guests").json()] == [
        "Their Guest"
    ]


# -- Guests are still not part of any of this -----------------------------


def test_a_guest_link_works_regardless_of_who_owns_the_event(
    client: TestClient, anonymous_client: TestClient
) -> None:
    event = create_event(client)
    guest = add_guest(client, event["id"], "Jordan Lee")

    assert anonymous_client.get(f"/invites/{guest['invite_token']}").status_code == 200


def test_the_event_lookup_by_slug_is_not_public(
    client: TestClient, anonymous_client: TestClient
) -> None:
    """It is a host route: guests reach their event through their token, never a slug."""
    event = create_event(client)

    assert anonymous_client.get(f"/events/{event['slug']}").status_code == 401
