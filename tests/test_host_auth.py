"""TAP-7725 — host endpoints require a host; guest routes require nothing.

The defect this closes is not abstract. `GET /events/{event_id}/guests` returned every
`invite_token` on the event to anyone who could reach the URL, and an invite token is
enough to RSVP as that guest. That single response was the whole guest list plus the
credential for each row in it.

The other half matters just as much and is easier to break by accident: adding host
auth must not make a *guest* authenticate. The link is the credential. There is a test
below that fails if a guest route ever starts asking for a session.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from tests.conftest import HOST_EMAIL, HOST_PASSWORD
from tests.factories import add_guest, add_segments, create_event

REGISTRATION_TOKEN = "let-me-in"


@pytest.fixture(autouse=True)
def _settings_are_not_shared_between_tests() -> Iterator[None]:
    """`get_settings` is `lru_cache`d, so a test that changes the environment has to
    drop the cache on the way in *and* on the way out, or it leaks into the next one."""
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def registration_open(monkeypatch: pytest.MonkeyPatch) -> str:
    """Open registration for one test, and hand back the token it expects."""
    from app.config import get_settings

    monkeypatch.setenv("HOST_REGISTRATION_TOKEN", REGISTRATION_TOKEN)
    get_settings.cache_clear()
    return REGISTRATION_TOKEN


# -- The host endpoints refuse an anonymous caller ------------------------


def test_creating_an_event_anonymously_is_401(anonymous_client: TestClient) -> None:
    response = anonymous_client.post(
        "/events",
        json={"slug": "sneaky", "title": "Lisa & Bill", "host_name": "Lisa and Bill"},
    )
    assert response.status_code == 401


def test_adding_a_guest_anonymously_is_401(
    client: TestClient, anonymous_client: TestClient
) -> None:
    event = create_event(client)
    response = anonymous_client.post(
        f"/events/{event['id']}/guests", json={"name": "Gatecrasher", "party_size": 1}
    )
    assert response.status_code == 401


def test_listing_guests_anonymously_is_401(
    client: TestClient, anonymous_client: TestClient
) -> None:
    event = create_event(client)
    add_guest(client, event["id"], "Jordan Lee")

    response = anonymous_client.get(f"/events/{event['id']}/guests")
    assert response.status_code == 401


def test_the_guest_list_does_not_hand_out_invite_tokens_anonymously(
    client: TestClient, anonymous_client: TestClient
) -> None:
    """The reason this issue was Urgent. One unauthenticated GET was every credential."""
    event = create_event(client)
    guest = add_guest(client, event["id"], "Jordan Lee")
    token = str(guest["invite_token"])

    response = anonymous_client.get(f"/events/{event['id']}/guests")

    assert response.status_code == 401
    assert token not in response.text
    assert "invite_token" not in response.text


def test_a_signed_in_host_can_use_each_protected_route(client: TestClient) -> None:
    """The other half of "authenticated and unauthenticated paths for each route"."""
    event = create_event(client)
    guest = add_guest(client, event["id"], "Jordan Lee")

    listed = client.get(f"/events/{event['id']}/guests")
    assert listed.status_code == 200
    assert [row["name"] for row in listed.json()] == ["Jordan Lee"]
    assert listed.json()[0]["invite_token"] == guest["invite_token"]


# -- Guests are not dragged into this -------------------------------------


def test_guest_routes_stay_anonymous(
    client: TestClient, anonymous_client: TestClient, db_session: Session
) -> None:
    """The link IS the credential. If this ever fails, the product got worse."""
    event = create_event(client)
    segments = add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "Jordan Lee", party_size=1)
    token = str(guest["invite_token"])

    assert anonymous_client.get(f"/invites/{token}").status_code == 200
    assert anonymous_client.get(f"/invites/{token}/wedding").status_code == 200
    assert anonymous_client.get(f"/invites/{token}/rsvp").status_code == 200
    assert anonymous_client.get(f"/invites/{token}/print").status_code == 200
    assert anonymous_client.get(f"/api/invites/{token}").status_code == 200

    written = anonymous_client.put(
        f"/api/invites/{token}/rsvp",
        json={
            "note": None,
            "attendees": [
                {
                    "name": "Jordan Lee",
                    "attending": True,
                    "is_child": False,
                    "dietary_tags": [],
                    "dietary_notes": None,
                    "attendance": [
                        {"segment_id": sid, "attending": True} for sid in segments.values()
                    ],
                }
            ],
        },
    )
    assert written.status_code == 200, written.text


# -- Signing in -----------------------------------------------------------


def test_a_wrong_password_and_an_unknown_address_are_indistinguishable(
    anonymous_client: TestClient, host: object
) -> None:
    """Different wording here would turn login into a membership check on the address."""
    wrong_password = anonymous_client.post(
        "/auth/login", json={"email": HOST_EMAIL, "password": "not the password"}
    )
    no_such_host = anonymous_client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "not the password"}
    )

    assert wrong_password.status_code == no_such_host.status_code == 401
    assert wrong_password.json() == no_such_host.json()


def test_a_failed_login_sets_no_cookie(anonymous_client: TestClient, host: object) -> None:
    response = anonymous_client.post("/auth/login", json={"email": HOST_EMAIL, "password": "wrong"})

    assert "set-cookie" not in response.headers
    assert anonymous_client.get("/auth/me").status_code == 401


def test_the_session_cookie_is_httponly(anonymous_client: TestClient, host: object) -> None:
    response = anonymous_client.post(
        "/auth/login", json={"email": HOST_EMAIL, "password": HOST_PASSWORD}
    )

    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.replace("samesite", "SameSite")


def test_the_raw_session_token_is_never_stored(
    anonymous_client: TestClient, host: object, engine: Engine
) -> None:
    """A dump of `host_sessions` must not hand anybody a live session."""
    from app.config import get_settings
    from app.models import HostSession

    anonymous_client.post("/auth/login", json={"email": HOST_EMAIL, "password": HOST_PASSWORD})
    raw = anonymous_client.cookies[get_settings().session_cookie_name]

    with Session(engine) as session:
        stored = [row.token_hash for row in session.scalars(select(HostSession))]

    assert stored, "no session row was written"
    assert raw not in stored
    assert hashlib.sha256(raw.encode()).hexdigest() in stored


def test_signing_out_kills_the_session(client: TestClient) -> None:
    assert client.get("/auth/me").status_code == 200

    assert client.post("/auth/logout").status_code == 204
    assert client.get("/auth/me").status_code == 401


def test_a_copied_cookie_dies_with_the_session(
    client: TestClient, anonymous_client: TestClient
) -> None:
    """Server-side sessions exist so a stolen cookie can be revoked. Prove it."""
    from app.config import get_settings

    name = get_settings().session_cookie_name
    stolen = client.cookies[name]

    client.post("/auth/logout")

    anonymous_client.cookies.set(name, stolen)
    assert anonymous_client.get("/auth/me").status_code == 401


def test_an_expired_session_is_refused_and_cleared(client: TestClient, engine: Engine) -> None:
    from app.models import HostSession

    with Session(engine) as session:
        row = session.scalars(select(HostSession)).one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    assert client.get("/auth/me").status_code == 401

    with Session(engine) as session:
        assert session.scalars(select(HostSession)).all() == [], (
            "the expired session row should have been cleared on sight"
        )


# -- Registration is closed unless it is deliberately opened --------------


def test_registration_is_closed_by_default(anonymous_client: TestClient) -> None:
    """Forgetting to configure the bootstrap token must fail safe, not wide open."""
    response = anonymous_client.post(
        "/auth/register",
        json={
            "email": "stranger@example.com",
            "password": "a long enough password",
            "registration_token": "anything",
        },
    )
    assert response.status_code == 403


def test_registration_works_with_the_configured_token(
    anonymous_client: TestClient, registration_open: str
) -> None:
    response = anonymous_client.post(
        "/auth/register",
        json={
            "email": "NewHost@Example.com",
            "password": "a long enough password",
            "registration_token": registration_open,
        },
    )

    assert response.status_code == 201, response.text
    # Casefolded on the way in, so a stray capital cannot create a second account.
    assert response.json()["email"] == "newhost@example.com"


def test_the_wrong_registration_token_is_refused(
    anonymous_client: TestClient, registration_open: str
) -> None:
    response = anonymous_client.post(
        "/auth/register",
        json={
            "email": "stranger@example.com",
            "password": "a long enough password",
            "registration_token": "guessed",
        },
    )

    assert response.status_code == 403


def test_a_duplicate_address_is_a_conflict(
    anonymous_client: TestClient, host: object, registration_open: str
) -> None:
    response = anonymous_client.post(
        "/auth/register",
        json={
            "email": HOST_EMAIL.upper(),
            "password": "a long enough password",
            "registration_token": registration_open,
        },
    )

    assert response.status_code == 409


def test_a_short_password_is_refused(anonymous_client: TestClient, registration_open: str) -> None:
    response = anonymous_client.post(
        "/auth/register",
        json={
            "email": "stranger@example.com",
            "password": "short",
            "registration_token": registration_open,
        },
    )

    assert response.status_code == 422


def test_the_password_is_not_stored_in_the_clear(host: object, engine: Engine) -> None:
    from app.models import Host

    with Session(engine) as session:
        stored = session.scalars(select(Host)).one().password_hash

    assert HOST_PASSWORD not in stored
    assert stored.startswith("$argon2id$"), f"not an argon2id digest: {stored[:20]!r}"
