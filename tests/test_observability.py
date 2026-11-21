"""Health that means something, logs a machine can read, and no tokens in them. TAP-7734.

The issue's two "done when" lines are the two halves of this file:

* a database outage makes the health check fail rather than report ok, and
* no invite token appears anywhere in the logs, verified by a test.

The second is checked against FORMATTED output — the bytes the handler would write —
because that is where the redaction lives. A test reading `caplog.records` would see
the raw messages, tokens and all, and pass or fail for the wrong reason.
"""

from __future__ import annotations

import importlib
import io
import json
import logging
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from tests.factories import add_guest, create_event

# -- Health ------------------------------------------------------------------


def test_health_checks_the_database(anonymous_client: TestClient) -> None:
    response = anonymous_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


@pytest.fixture
def database_down(anonymous_client: TestClient) -> Iterator[TestClient]:
    """The app's sessions point at a port nothing listens on.

    Port 1 on loopback refuses at once, so the test measures the app's handling of an
    outage rather than a TCP timeout.
    """
    from app.db import get_db
    from app.main import app

    unreachable = create_engine("postgresql+psycopg://nobody:nothing@127.0.0.1:1/none")

    def no_database() -> Iterator[Session]:
        with Session(unreachable) as session:
            yield session

    app.dependency_overrides[get_db] = no_database
    try:
        yield anonymous_client
    finally:
        del app.dependency_overrides[get_db]
        unreachable.dispose()


def test_a_database_outage_fails_the_health_check(database_down: TestClient) -> None:
    """What the Docker healthcheck, `prod.sh status` and `prod.sh deploy` all read."""
    response = database_down.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unreachable"}


def test_liveness_does_not_depend_on_the_database(database_down: TestClient) -> None:
    """Restarting the app cannot fix Postgres, so a restart probe must not ask about it."""
    response = database_down.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# -- Request ids -------------------------------------------------------------


def test_every_response_carries_a_request_id(anonymous_client: TestClient) -> None:
    first = anonymous_client.get("/robots.txt").headers["X-Request-ID"]
    second = anonymous_client.get("/robots.txt").headers["X-Request-ID"]
    assert first and second and first != second


def test_a_callers_request_id_is_kept_so_both_logs_line_up(
    anonymous_client: TestClient,
) -> None:
    response = anonymous_client.get("/robots.txt", headers={"X-Request-ID": "edge-4f2a.9"})
    assert response.headers["X-Request-ID"] == "edge-4f2a.9"


@pytest.mark.parametrize("hostile", ["a\nfake log line", 'x"y', "z" * 129, ""])
def test_a_request_id_that_could_corrupt_a_log_line_is_replaced(
    anonymous_client: TestClient, hostile: str
) -> None:
    response = anonymous_client.get("/robots.txt", headers={"X-Request-ID": hostile})
    assert response.headers["X-Request-ID"] != hostile
    assert len(response.headers["X-Request-ID"]) == 32


# -- No invite token in the logs ---------------------------------------------


@pytest.fixture
def formatted_log() -> Iterator[io.StringIO]:
    """Everything logged during the test, formatted exactly as production writes it."""
    from app.observability import JsonFormatter

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonFormatter())
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


@pytest.fixture
def one_request_allowed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """So the second guest request in a test is answered 429 by the throttle."""
    from app.config import get_settings
    from app.ratelimit import reset_limiter

    monkeypatch.setenv("INVITE_RATE_LIMIT", "1")
    get_settings.cache_clear()
    reset_limiter()
    yield
    get_settings.cache_clear()
    reset_limiter()


def test_no_invite_token_reaches_the_log(
    client: TestClient,
    db_session: Session,
    formatted_log: io.StringIO,
    one_request_allowed: None,
) -> None:
    """Every route by which a token could reach a log line, through the real formatter.

    The guest pages put it in the URL. The throttle sees it in a 429. The console mail
    transport — production's until mail is configured — prints the invitation, link
    included. A failed SQL statement can quote its bound values. A traceback can quote
    anything.
    """
    from app.invitations import INVITE, compose
    from app.mail import ConsoleTransport
    from app.models import Event, Guest

    event = create_event(client)
    token = str(add_guest(client, event["id"], "Marcus Ellery")["invite_token"])

    # A guest's page, and then the same again, which the throttle refuses.
    assert client.get(f"/invites/{token}").status_code != 429
    assert client.get(f"/api/invites/{token}").status_code == 429

    # The invitation, as production's console transport handles it.
    guest = db_session.query(Guest).filter_by(invite_token=token).one()
    event_row = db_session.get(Event, uuid.UUID(event["id"]))
    assert event_row is not None
    message = compose(event_row, guest, "https://wedding.example", INVITE)
    assert token in message.text, "the fixture should be testing a body that holds the link"
    ConsoleTransport().send(message)

    # A failing statement with the token bound into it, logged as the app would.
    from app.db import engine

    with engine.connect() as connection:
        try:
            connection.execute(
                text("select no_such_column from guests where invite_token = :t"), {"t": token}
            )
        except SQLAlchemyError:
            logging.getLogger("app").exception("a statement failed")

    # A traceback whose message is the guest's URL.
    try:
        raise RuntimeError(f"could not render https://wedding.example/invites/{token}/rsvp")
    except RuntimeError:
        logging.getLogger("app").exception("render failed")

    output = formatted_log.getvalue()
    assert token not in output
    lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    assert lines, "nothing was logged, so the absence of the token proves nothing"

    request_lines = [line for line in lines if line["logger"] == "app.request"]
    assert {line["path"] for line in request_lines} >= {
        "/invites/[token]",
        "/api/invites/[token]",
    }
    assert any(line["status"] == 429 for line in request_lines)
    assert any("/invites/[token]" in line.get("body", "") for line in lines)


def test_uvicorns_own_access_log_is_off() -> None:
    """It wrote every request line, token and all, before TAP-7734."""
    # Importing the app is what configures logging, so make sure it has happened.
    importlib.import_module("app.main")
    assert logging.getLogger("uvicorn.access").disabled


def test_request_lines_are_json_with_the_fields_a_reader_filters_on(
    anonymous_client: TestClient, formatted_log: io.StringIO
) -> None:
    response = anonymous_client.get("/robots.txt", headers={"X-Request-ID": "trace-me"})
    assert response.status_code == 200
    lines = [json.loads(line) for line in formatted_log.getvalue().splitlines()]
    (request_line,) = [line for line in lines if line["logger"] == "app.request"]
    assert request_line["request_id"] == "trace-me"
    assert request_line["method"] == "GET"
    assert request_line["path"] == "/robots.txt"
    assert request_line["status"] == 200
    assert isinstance(request_line["duration_ms"], float)


def test_health_probes_do_not_flood_the_log(
    anonymous_client: TestClient, formatted_log: io.StringIO
) -> None:
    """Docker hits /health every few seconds; a line for each would bury real traffic."""
    for _ in range(3):
        assert anonymous_client.get("/health").status_code == 200
        assert anonymous_client.get("/health/live").status_code == 200
    assert "app.request" not in formatted_log.getvalue()


def test_a_failing_health_probe_is_logged(
    database_down: TestClient, formatted_log: io.StringIO
) -> None:
    assert database_down.get("/health").status_code == 503
    output = formatted_log.getvalue()
    assert '"status": 503' in output
    assert "database unreachable" in output
