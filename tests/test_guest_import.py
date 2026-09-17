"""TAP-7732 — a guest list out of a spreadsheet.

The rule the whole issue turns on: **validate everything, then import, or import
nothing.** A partial import of a wedding guest list is worse than a rejected one — the
host cannot tell which rows landed without reading the database, and re-running the
file would double the ones that did. So a single bad row refuses the whole file, and
every problem is reported at once with the line number to look at.
"""

from __future__ import annotations

import io
import uuid

from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from tests.factories import add_guest, create_event

GOOD = """name,email,party_size
Dana Whitfield,dana@example.com,2
Marcus Ellery,,1
The Calloway family,calloway@example.com,5
"""


def _upload(client: TestClient, event_id: str, text: str, filename: str = "guests.csv") -> Response:
    return client.post(
        f"/host/events/{event_id}/import",
        files={"file": (filename, io.BytesIO(text.encode()), "text/csv")},
        follow_redirects=False,
    )


def _names(event_id: str, db_session: Session) -> list[str]:
    from app.models import Guest

    db_session.expire_all()
    return sorted(
        db_session.scalars(select(Guest.name).where(Guest.event_id == uuid.UUID(event_id))).all()
    )


# -- The parser, away from HTTP -------------------------------------------


def test_a_clean_file_parses_every_row() -> None:
    from app.guest_import import parse

    plan = parse(GOOD)

    assert plan.ok
    assert [row.name for row in plan.rows] == [
        "Dana Whitfield",
        "Marcus Ellery",
        "The Calloway family",
    ]
    assert [row.party_size for row in plan.rows] == [2, 1, 5]
    assert plan.rows[1].email is None


def test_a_file_with_no_header_is_read_positionally() -> None:
    """Somebody will paste three columns without a header, and they are not wrong."""
    from app.guest_import import parse

    plan = parse("Dana Whitfield,dana@example.com,2\nMarcus Ellery,,1\n")

    assert plan.ok
    assert [row.name for row in plan.rows] == ["Dana Whitfield", "Marcus Ellery"]


def test_columns_are_found_by_their_heading_not_their_position() -> None:
    from app.guest_import import parse

    plan = parse("Seats,Name,Email\n4,The Raghunathans,priya@example.com\n")

    assert plan.ok
    assert plan.rows[0].name == "The Raghunathans"
    assert plan.rows[0].party_size == 4
    assert plan.rows[0].email == "priya@example.com"


def test_a_missing_seat_count_means_one() -> None:
    from app.guest_import import parse

    plan = parse("name,email,party_size\nMarcus Ellery,,\n")

    assert plan.ok
    assert plan.rows[0].party_size == 1


def test_every_problem_is_reported_not_just_the_first() -> None:
    """A host fixing one error per upload round trip would rather see them all."""
    from app.guest_import import parse

    plan = parse(
        "name,email,party_size\n"
        ",nobody@example.com,2\n"
        "Marcus Ellery,not-an-email,1\n"
        "Dana Whitfield,dana@example.com,many\n"
    )

    assert not plan.ok
    lines = sorted(problem.line for problem in plan.problems)
    assert lines == [2, 3, 4], f"expected a problem on each bad line, got {plan.problems}"


def test_a_problem_carries_the_line_number_from_the_file() -> None:
    """A row with content but no name is line 3, and says so."""
    from app.guest_import import parse

    plan = parse(
        "name,email,party_size\n"
        "Dana Whitfield,dana@example.com,2\n"
        ",orphan@example.com,2\n"
        "Marcus Ellery,,1\n"
    )

    assert not plan.ok
    assert any("Line 3" in str(problem) for problem in plan.problems), plan.problems


def test_a_row_of_empty_cells_is_treated_as_a_blank_line() -> None:
    """Spreadsheets export trailing `,,` rows. Refusing those would refuse most files."""
    from app.guest_import import parse

    plan = parse("name,email,party_size\nDana Whitfield,dana@example.com,2\n,,\n,,\n")

    assert plan.ok
    assert [row.name for row in plan.rows] == ["Dana Whitfield"]


def test_a_name_repeated_inside_the_file_is_a_problem() -> None:
    from app.guest_import import parse

    plan = parse("name\nDana Whitfield\ndana whitfield\n")

    assert not plan.ok
    assert any("line 2" in str(problem) for problem in plan.problems), plan.problems


def test_a_name_already_on_the_guest_list_is_a_problem() -> None:
    """Otherwise a second upload of the same file quietly doubles the wedding."""
    from app.guest_import import parse

    plan = parse("name\nDana Whitfield\n", existing_names={"dana whitfield"})

    assert not plan.ok
    assert any("already on the guest list" in str(problem) for problem in plan.problems)


def test_an_empty_file_is_refused_and_says_why() -> None:
    """Refusing is not enough — an empty plan is falsy anyway, so a host who uploads
    the wrong file would get a blank refusal with nothing to act on. It has to say
    that the file had no rows. Written after a mutation that removed the message
    passed the whole file on the strength of `.ok` alone."""
    from app.guest_import import parse

    for text in ("", "name,email,party_size\n"):
        plan = parse(text)
        assert not plan.ok
        assert plan.problems, f"refused {text!r} with no explanation"
        assert any("no rows" in str(problem) for problem in plan.problems), plan.problems


def test_an_empty_upload_tells_the_host_what_was_wrong(client: TestClient) -> None:
    event = create_event(client)

    response = _upload(client, str(event["id"]), "name,email,party_size\n")

    assert response.status_code == 422
    assert "no rows" in response.text


def test_blank_lines_between_rows_are_ignored() -> None:
    from app.guest_import import parse

    plan = parse("name\nDana Whitfield\n\n\nMarcus Ellery\n")

    assert plan.ok
    assert len(plan.rows) == 2


def test_seats_outside_the_range_are_refused() -> None:
    from app.guest_import import parse

    assert not parse("name,email,party_size\nDana,,0\n").ok
    assert not parse("name,email,party_size\nDana,,99\n").ok


# -- Through the dashboard ------------------------------------------------


def test_a_clean_file_imports_in_one_request(
    client: TestClient, anonymous_client: TestClient, db_session: Session
) -> None:
    event = create_event(client)

    response = _upload(client, str(event["id"]), GOOD)

    assert response.status_code == 303
    assert _names(str(event["id"]), db_session) == [
        "Dana Whitfield",
        "Marcus Ellery",
        "The Calloway family",
    ]


def test_every_imported_row_gets_a_working_link(
    client: TestClient, anonymous_client: TestClient, db_session: Session
) -> None:
    from app.models import Guest

    event = create_event(client)
    _upload(client, str(event["id"]), GOOD)

    tokens = db_session.scalars(
        select(Guest.invite_token).where(Guest.event_id == uuid.UUID(str(event["id"])))
    ).all()

    assert len(set(tokens)) == 3, "every invitation needs its own token"
    for token in tokens:
        assert anonymous_client.get(f"/invites/{token}").status_code == 200


def test_one_bad_row_imports_nothing_at_all(client: TestClient, db_session: Session) -> None:
    """The heart of the issue. A partial import is worse than a rejected one."""
    event = create_event(client)

    response = _upload(
        client,
        str(event["id"]),
        "name,email,party_size\n"
        "Dana Whitfield,dana@example.com,2\n"
        "Marcus Ellery,not-an-email,1\n"
        "The Calloway family,calloway@example.com,5\n",
    )

    assert response.status_code == 422
    assert _names(str(event["id"]), db_session) == [], "a refused file must write nothing"


def test_the_refusal_names_every_bad_line(client: TestClient) -> None:
    event = create_event(client)

    response = _upload(
        client,
        str(event["id"]),
        "name,email,party_size\nDana,bad-email,2\nMarcus,,nope\n",
    )

    assert response.status_code == 422
    assert "Line 2" in response.text
    assert "Line 3" in response.text
    assert "Nothing was imported" in response.text


def test_importing_the_same_file_twice_is_refused(client: TestClient, db_session: Session) -> None:
    """The second run must not double the guest list."""
    event = create_event(client)

    first = _upload(client, str(event["id"]), GOOD)
    assert first.status_code == 303

    second = _upload(client, str(event["id"]), GOOD)

    assert second.status_code == 422
    assert "already on the guest list" in second.text
    assert len(_names(str(event["id"]), db_session)) == 3


def test_a_name_that_clashes_with_a_hand_added_guest_is_refused(
    client: TestClient, db_session: Session
) -> None:
    event = create_event(client)
    add_guest(client, event["id"], "Dana Whitfield")

    response = _upload(client, str(event["id"]), "name\nDana Whitfield\n")

    assert response.status_code == 422
    assert len(_names(str(event["id"]), db_session)) == 1


def test_a_spreadsheet_saved_with_a_byte_order_mark_still_works(
    client: TestClient, db_session: Session
) -> None:
    """Excel writes a BOM. Without utf-8-sig the first header becomes '\\ufeffname'
    and the name column disappears, which reads as "every row has no name"."""
    event = create_event(client)

    response = client.post(
        f"/host/events/{event['id']}/import",
        files={
            "file": (
                "guests.csv",
                io.BytesIO(b"\xef\xbb\xbf" + GOOD.encode()),
                "text/csv",
            )
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text
    assert len(_names(str(event["id"]), db_session)) == 3


def test_importing_needs_a_session(anonymous_client: TestClient, client: TestClient) -> None:
    event = create_event(client)

    assert _upload(anonymous_client, str(event["id"]), GOOD).status_code == 401


def test_another_host_cannot_import_into_your_event(client: TestClient, engine: Engine) -> None:
    from app.auth import hash_password
    from app.main import app
    from app.models import Host

    event = create_event(client)

    with Session(engine) as session:
        session.add(Host(email="thief@example.com", password_hash=hash_password("another pass!!")))
        session.commit()

    with TestClient(app) as other:
        other.post("/host/login", data={"email": "thief@example.com", "password": "another pass!!"})
        assert _upload(other, str(event["id"]), GOOD).status_code == 404
