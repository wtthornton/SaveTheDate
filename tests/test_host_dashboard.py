"""TAP-7730 — the host dashboard, and the numbers on it.

The issue's "done when" is about arithmetic, so most of this file is arithmetic. Two
rules drive all of it:

**Counts are seat-based, not invitation-based.** An invitation for four where two people
are coming is two. Nothing is read from a stored integer; every number is derived from
`attendees` and `attendance`, so it cannot drift from what guests actually said.

**Headcounts are per day, not per plate.** TAP-7739 cut meal options deliberately and
the invariants say so. The issue's original wording asked for "counts per meal option",
which would have meant inventing a column that was removed on purpose; it was corrected
on 2026-09-17 before this was built.

The mixed party — one person coming, one declining, under one invitation — is the case
the issue calls out by name, and the one a naive `COUNT(guests)` gets wrong.
"""

from __future__ import annotations

import csv
import io
import uuid
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from tests.conftest import HOST_EMAIL, HOST_PASSWORD
from tests.factories import add_guest, add_segments, create_event
from tests.html_assertions import Document

if TYPE_CHECKING:
    # `app.models` pulls in `app.db`, which builds the engine at import time.
    from app.dashboard import Dashboard


def _view(event_id: str, db_session: Session) -> Dashboard:
    """The dashboard a host would be looking at, built from the rows as they stand."""
    from app.dashboard import build
    from app.models import Event

    event = db_session.get(Event, uuid.UUID(event_id))
    assert event is not None
    return build(event, db_session)


def _only_guest(event_id: str, db_session: Session) -> uuid.UUID:
    from app.models import Guest

    guest = db_session.scalars(select(Guest).where(Guest.event_id == uuid.UUID(event_id))).one()
    return guest.id


def _rsvp(
    client: TestClient, token: str, attendees: list[dict[str, object]], note: str | None = None
) -> None:
    response = client.put(f"/api/invites/{token}/rsvp", json={"note": note, "attendees": attendees})
    assert response.status_code == 200, response.text


def _person(
    name: str,
    segments: dict[str, str],
    *,
    coming: tuple[str, ...] = ("welcome", "ceremony"),
    child: bool = False,
    tags: list[str] | None = None,
    notes: str | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "attending": bool(coming),
        "is_child": child,
        "dietary_tags": tags or [],
        "dietary_notes": notes,
        "attendance": [
            {"segment_id": sid, "attending": key in coming} for key, sid in segments.items()
        ],
    }


def _event_with_party(client: TestClient, db_session: Session) -> tuple[dict[str, str], str, str]:
    event = create_event(client)
    segments = add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "The Calloway family", party_size=4)
    return segments, str(event["id"]), str(guest["invite_token"])


# -- The headline ---------------------------------------------------------


def test_a_mixed_party_is_counted_by_person(client: TestClient, db_session: Session) -> None:
    """The case the issue names. One invitation for four: TWO coming, one not, one silent.

    Two coming rather than one, deliberately. With a single attendee, counting people
    and counting invitations both give 1, and a mutation that replaced the per-person
    sum with `1 if anyone is coming` passed this test untouched. Two is the smallest
    number that tells the two readings apart.
    """
    segments, event_id, token = _event_with_party(client, db_session)
    _rsvp(
        client,
        token,
        [
            _person("Ada Calloway", segments),
            _person("Ben Calloway", segments),
            _person("Cleo Calloway", segments, coming=()),
        ],
    )

    headline = _view(event_id, db_session).headline

    assert headline.accepted == 2, "one invitation is not one guest"
    assert headline.declined == 1
    assert headline.invited_seats == 4
    assert headline.awaiting == 1


def test_an_invitation_nobody_answered_is_all_awaiting(
    client: TestClient, db_session: Session
) -> None:
    _segments, event_id, _token = _event_with_party(client, db_session)

    headline = _view(event_id, db_session).headline

    assert headline.accepted == 0
    assert headline.declined == 0
    assert headline.awaiting == 4


def test_seats_never_go_negative_if_a_host_shrinks_an_invitation(
    client: TestClient, db_session: Session
) -> None:
    """A host cutting the party size after people replied must not show -2 outstanding."""
    from app.models import Guest

    segments, event_id, token = _event_with_party(client, db_session)
    _rsvp(client, token, [_person(f"Person {n}", segments) for n in range(4)])

    guest = db_session.get(Guest, _only_guest(event_id, db_session))
    assert guest is not None
    guest.party_size = 2
    db_session.commit()

    assert _view(event_id, db_session).headline.awaiting == 0


# -- The caterer's numbers ------------------------------------------------


def test_the_headcount_is_per_day_and_children_are_separate(
    client: TestClient, db_session: Session
) -> None:
    segments, event_id, token = _event_with_party(client, db_session)
    _rsvp(
        client,
        token,
        [
            # Two adults at everything, golf included.
            _person("Ada Calloway", segments, coming=("welcome", "ceremony", "golf")),
            _person("Ben Calloway", segments, coming=("welcome", "ceremony", "golf")),
            # A child at the ceremony only.
            _person("Cleo Calloway", segments, coming=("ceremony",), child=True),
        ],
    )

    counts = {row.segment.name: row for row in _view(event_id, db_session).segments}

    assert counts["Welcome party on the beach"].adults == 2
    assert counts["Welcome party on the beach"].children == 0
    assert counts["Ceremony and reception"].adults == 2
    assert counts["Ceremony and reception"].children == 1
    assert counts["Ceremony and reception"].total == 3
    assert counts["Golf at Palmilla"].adults == 2
    assert counts["Golf at Palmilla"].children == 0


def test_saying_no_to_an_optional_extra_is_not_counted(
    client: TestClient, db_session: Session
) -> None:
    """Only an explicit yes counts, so a paid extra is never over-booked."""
    segments, event_id, token = _event_with_party(client, db_session)
    _rsvp(client, token, [_person("Ada Calloway", segments, coming=("ceremony",))])

    counts = {row.segment.name: row for row in _view(event_id, db_session).segments}

    assert counts["Golf at Palmilla"].total == 0


def test_a_declined_guests_dietary_tag_is_not_counted(
    client: TestClient, db_session: Session
) -> None:
    """They are not eating, so counting their tag inflates what the caterer cooks."""
    segments, event_id, token = _event_with_party(client, db_session)
    _rsvp(
        client,
        token,
        [
            _person("Ada Calloway", segments, tags=["vegetarian"]),
            _person("Ben Calloway", segments, coming=(), tags=["vegan"]),
        ],
    )

    assert dict(_view(event_id, db_session).dietary.counts) == {"Vegetarian": 1}


def test_dietary_notes_are_attributed_to_a_person(client: TestClient, db_session: Session) -> None:
    """An allergy a caterer cannot trace to a person is not something they can act on."""
    segments, event_id, token = _event_with_party(client, db_session)
    _rsvp(
        client,
        token,
        [
            _person(
                "Ada Calloway", segments, tags=["nut-allergy"], notes="Severe — no shared fryers."
            )
        ],
    )

    notes = _view(event_id, db_session).dietary.notes

    assert [(entry.name, entry.note) for entry in notes] == [
        ("Ada Calloway", "Severe — no shared fryers.")
    ]


# -- The page itself ------------------------------------------------------


def test_the_dashboard_answers_the_question_in_one_screen(
    client: TestClient, db_session: Session
) -> None:
    segments, event_id, token = _event_with_party(client, db_session)
    _rsvp(
        client,
        token,
        [_person("Ada Calloway", segments), _person("Ben Calloway", segments, coming=())],
        note="Ben is so sorry to miss it.",
    )

    page = client.get(f"/host/events/{event_id}")

    assert page.status_code == 200
    assert "How many people are coming?" in page.text
    assert "Headcount for each day" in page.text
    assert "The Calloway family" in page.text
    assert "Ben is so sorry to miss it." in page.text


def test_the_dashboard_shows_the_invite_link(client: TestClient, db_session: Session) -> None:
    _segments, event_id, token = _event_with_party(client, db_session)

    assert f"/invites/{token}" in client.get(f"/host/events/{event_id}").text


def test_every_control_on_the_dashboard_has_a_label(
    client: TestClient, db_session: Session
) -> None:
    """Same floor as the guest pages: real controls, real labels, no `role=` on a div."""
    _segments, event_id, _token = _event_with_party(client, db_session)

    page = Document(client.get(f"/host/events/{event_id}").text)

    labelled = {label.attrs.get("for") for label in page.find_all("label")}
    unlabelled = [
        control.attrs.get("name")
        for control in page.form_controls()
        if control.attrs.get("id") not in labelled
    ]
    assert not unlabelled, f"controls with no label: {unlabelled}"


def test_the_dashboard_needs_a_session(anonymous_client: TestClient, client: TestClient) -> None:
    event = create_event(client)

    assert anonymous_client.get(f"/host/events/{event['id']}").status_code == 401


def test_another_host_cannot_open_the_dashboard(client: TestClient, engine: Engine) -> None:
    """Same 404 rule as the JSON API — TAP-7726."""
    from app.auth import hash_password
    from app.main import app
    from app.models import Host

    event = create_event(client)

    with Session(engine) as session:
        session.add(Host(email="other@example.com", password_hash=hash_password("another pass!!")))
        session.commit()

    with TestClient(app) as other:
        other.post("/host/login", data={"email": "other@example.com", "password": "another pass!!"})
        assert other.get(f"/host/events/{event['id']}").status_code == 404


# -- The CSV --------------------------------------------------------------


def test_the_csv_lists_the_guests(client: TestClient, db_session: Session) -> None:
    segments, event_id, token = _event_with_party(client, db_session)
    _rsvp(
        client,
        token,
        [_person("Ada Calloway", segments), _person("Ben Calloway", segments, coming=())],
    )

    response = client.get(f"/host/events/{event_id}/guests.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0][0] == "Invitation"
    body = rows[1]
    assert body[0] == "The Calloway family"
    assert body[2] == "4"
    assert body[3] == "1"
    assert body[4] == "1"
    assert "Ada Calloway" in body[6]
    assert "Ben Calloway" not in body[6], "someone who declined is not on the caterer's list"


def test_the_csv_does_not_contain_invite_tokens(client: TestClient, db_session: Session) -> None:
    """It exists to be handed to a caterer. A token in it is a credential handed over."""
    _segments, event_id, token = _event_with_party(client, db_session)

    assert token not in client.get(f"/host/events/{event_id}/guests.csv").text


# -- Changing the guest list ----------------------------------------------


def test_renaming_an_invitation_keeps_its_token(client: TestClient, db_session: Session) -> None:
    """The invariant the whole schema hangs off: a token already in an inbox never moves."""
    from app.models import Guest

    _segments, event_id, token = _event_with_party(client, db_session)
    guest_id = _only_guest(event_id, db_session)

    posted = client.post(
        f"/host/events/{event_id}/guests/{guest_id}",
        data={"name": "The Calloways", "email": "calloway@example.com", "party_size": "3"},
        follow_redirects=False,
    )
    assert posted.status_code == 303

    db_session.expire_all()
    guest = db_session.get(Guest, guest_id)
    assert guest is not None
    assert guest.name == "The Calloways"
    assert guest.party_size == 3
    assert guest.invite_token == token, "renaming an invitation must not re-key it"


def test_withdrawing_an_invitation_kills_its_link(
    client: TestClient, anonymous_client: TestClient, db_session: Session
) -> None:
    _segments, event_id, token = _event_with_party(client, db_session)
    guest_id = _only_guest(event_id, db_session)

    assert anonymous_client.get(f"/invites/{token}").status_code == 200

    withdrawn = client.post(
        f"/host/events/{event_id}/guests/{guest_id}/withdraw", follow_redirects=False
    )
    assert withdrawn.status_code == 303

    assert anonymous_client.get(f"/invites/{token}").status_code == 404


def test_adding_an_invitation_mints_a_working_link(
    client: TestClient, anonymous_client: TestClient, db_session: Session
) -> None:
    from app.models import Guest

    event = create_event(client)
    add_segments(db_session, event["id"])

    client.post(
        f"/host/events/{event['id']}/guests",
        data={"name": "Marcus Ellery", "email": "", "party_size": "1"},
        follow_redirects=False,
    )

    guest = db_session.get(Guest, _only_guest(str(event["id"]), db_session))
    assert guest is not None
    assert guest.name == "Marcus Ellery"
    assert anonymous_client.get(f"/invites/{guest.invite_token}").status_code == 200


# -- Signing in through a browser -----------------------------------------


def test_the_login_page_has_a_real_form(anonymous_client: TestClient) -> None:
    page = Document(anonymous_client.get("/host/login").text)

    controls = page.form_controls()
    assert {control.attrs.get("name") for control in controls} == {"email", "password"}
    for control in controls:
        assert control.attrs.get("id"), "every control needs an id for its label"


def test_a_wrong_password_does_not_sign_you_in(anonymous_client: TestClient, host: object) -> None:
    response = anonymous_client.post(
        "/host/login", data={"email": HOST_EMAIL, "password": "wrong"}, follow_redirects=False
    )

    assert response.status_code == 401
    assert "do not match" in response.text
    assert anonymous_client.get("/host", follow_redirects=False).status_code == 401


def test_signing_in_lands_on_the_event(anonymous_client: TestClient, client: TestClient) -> None:
    create_event(client)

    response = anonymous_client.post(
        "/host/login",
        data={"email": HOST_EMAIL, "password": HOST_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/host"

    landed = anonymous_client.get("/host")
    assert landed.status_code == 200
    assert "How many people are coming?" in landed.text
