from typing import Any

from fastapi.testclient import TestClient


def _create_event(client: TestClient, slug: str = "alex-and-sam") -> dict[str, Any]:
    response = client.post(
        "/events",
        json={
            "slug": slug,
            "title": "Alex & Sam",
            "host_name": "Alex and Sam",
            "event_date": "2027-06-12",
            "location": "Portland, OR",
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _add_guest(client: TestClient, event_id: str, name: str, party_size: int = 2) -> dict[str, Any]:
    response = client.post(
        f"/events/{event_id}/guests",
        json={"name": name, "email": "guest@example.com", "party_size": party_size},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_duplicate_slug_is_rejected(client: TestClient) -> None:
    _create_event(client)
    response = client.post(
        "/events",
        json={"slug": "alex-and-sam", "title": "Other", "host_name": "Other"},
    )
    assert response.status_code == 409


def test_invite_lookup_and_rsvp_round_trip(client: TestClient) -> None:
    event = _create_event(client)
    guest = _add_guest(client, event["id"], "Jordan Lee")

    invite = client.get(f"/invites/{guest['invite_token']}")
    assert invite.status_code == 200
    body = invite.json()
    assert body["guest_name"] == "Jordan Lee"
    assert body["event"]["title"] == "Alex & Sam"
    assert body["rsvp"] is None

    accepted = client.put(
        f"/invites/{guest['invite_token']}/rsvp",
        json={"attending": True, "party_size": 2, "note": "Can't wait"},
    )
    assert accepted.status_code == 200
    assert accepted.json() == {"attending": True, "party_size": 2, "note": "Can't wait"}

    # A guest may change their mind; the same token updates the existing RSVP.
    declined = client.put(
        f"/invites/{guest['invite_token']}/rsvp",
        json={"attending": False, "party_size": 0, "note": None},
    )
    assert declined.status_code == 200
    assert declined.json()["attending"] is False

    assert client.get(f"/invites/{guest['invite_token']}").json()["rsvp"]["attending"] is False


def test_rsvp_cannot_exceed_invited_party_size(client: TestClient) -> None:
    event = _create_event(client)
    guest = _add_guest(client, event["id"], "Robin Fox", party_size=2)

    response = client.put(
        f"/invites/{guest['invite_token']}/rsvp",
        json={"attending": True, "party_size": 5},
    )
    assert response.status_code == 422
    assert "at most 2" in response.json()["detail"]


def test_attending_rsvp_must_claim_a_seat(client: TestClient) -> None:
    event = _create_event(client)
    guest = _add_guest(client, event["id"], "Casey Kim")

    response = client.put(
        f"/invites/{guest['invite_token']}/rsvp",
        json={"attending": True, "party_size": 0},
    )
    assert response.status_code == 422


def test_unknown_invite_token_is_404(client: TestClient) -> None:
    assert client.get("/invites/not-a-real-token").status_code == 404
