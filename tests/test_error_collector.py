"""Errors reach an off-box collector, carry their request id, and never an invite token.

TAP-12484's acceptance, one section each:

* with no DSN the app runs exactly as today, the SDK never initialised;
* an unhandled exception becomes an event carrying the request id;
* no invite token reaches the collector, checked on the bytes that would be sent;
* nor do a guest's details, which the JSON logs carry as record extras.

The last is checked on the SERIALIZED ENVELOPE, the payload the HTTP transport would
post, because that is what leaves the process. A test reading the event dict before
`before_send` would see the tokens, and one reading it after would miss anything the
SDK adds on the way to the wire. Nothing here opens a socket: the transport below
keeps envelopes in a list.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import secrets
import subprocess
import sys
from collections.abc import Iterator
from typing import Any, cast

import pytest
import sentry_sdk
from fastapi.testclient import TestClient
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from app.config import Settings
from app.error_collector import before_send, init_error_collector, keep_allowlisted, scrub
from app.observability import bind_request_id
from tests.conftest import PROJECT_ROOT
from tests.factories import add_guest, create_event

# Shaped like a DSN so the SDK accepts it, on a reserved name that resolves nowhere.
# The transport below means nothing would ever dial it anyway.
FAKE_DSN = "https://public@collector.invalid/1"

# Stands in for a signed-in host's session cookie, which is a credential too.
SESSION_COOKIE_VALUE = "host-session-" + "s3cr3t" * 6

# A guest's details, distinctive enough that finding one in the envelope means a leak.
GUEST_NAME = "Philippa Quarrington"
GUEST_EMAIL = "philippa.quarrington@example.org"
GUEST_DIET = "severe shellfish allergy"


class RecordingTransport(Transport):
    """Keeps envelopes instead of sending them."""

    def __init__(self) -> None:
        super().__init__()
        self.envelopes: list[Envelope] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)

    def sent_bytes(self) -> str:
        return b"".join(envelope.serialize() for envelope in self.envelopes).decode()

    def events(self) -> list[dict[str, Any]]:
        """Each event as the plain JSON object it is on the wire."""
        return [
            cast(dict[str, Any], event)
            for envelope in self.envelopes
            if (event := envelope.get_event()) is not None
        ]


def _deactivate() -> None:
    client = sentry_sdk.get_client()
    client.close()
    sentry_sdk.get_global_scope().set_client(None)


@pytest.fixture
def collector() -> Iterator[RecordingTransport]:
    """The SDK initialised exactly as production would be, with only the transport swapped.

    The integrations patch Starlette classes once per process and stay patched; they
    look up the active client on every call and pass straight through without one, so
    closing the client here returns the rest of the suite to the no-DSN behavior.
    """
    transport = RecordingTransport()
    settings = Settings(sentry_dsn=FAKE_DSN, sentry_environment="test")
    assert init_error_collector(settings, transport=transport)
    try:
        yield transport
    finally:
        _deactivate()


# -- No DSN: inert ------------------------------------------------------------


@pytest.mark.parametrize("dsn", [None, ""])
def test_without_a_dsn_the_sdk_is_never_initialised(
    monkeypatch: pytest.MonkeyPatch, dsn: str | None
) -> None:
    """Empty counts as unset: compose passes "" when `.env.prod` has no SENTRY_DSN."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("sentry_sdk.init was called without a DSN")

    monkeypatch.setattr(sentry_sdk, "init", refuse)
    assert init_error_collector(Settings(sentry_dsn=dsn)) is False
    assert not sentry_sdk.get_client().is_active()


# Imports the app in a fresh interpreter and reports what that import started. A
# fresh process is the only honest place to look: in this one `app.main` is already
# imported, so importing it again runs nothing. `sentry_sdk` is checked in
# `sys.modules` BEFORE this script imports it to ask about the client.
_IMPORT_THE_APP = """
import json, sys
import app.main
imported = "sentry_sdk" in sys.modules
import sentry_sdk
print(json.dumps({"imported": imported, "active": sentry_sdk.get_client().is_active()}))
"""


def _import_the_app_with(dsn: str | None) -> dict[str, bool]:
    env = {key: value for key, value in os.environ.items() if key != "SENTRY_DSN"}
    if dsn is not None:
        env["SENTRY_DSN"] = dsn
    finished = subprocess.run(
        [sys.executable, "-c", _IMPORT_THE_APP],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr
    return cast(dict[str, bool], json.loads(finished.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize("dsn", [None, ""])
def test_importing_the_app_without_a_dsn_starts_no_collector(dsn: str | None) -> None:
    """The import-time call in `app.main`, run for real: no client, and no SDK loaded."""
    assert _import_the_app_with(dsn) == {"imported": False, "active": False}


def test_importing_the_app_with_a_dsn_does_start_one() -> None:
    """The control for the test above, so its False cannot come from a broken probe.

    This client has the SDK's real HTTP transport, pointed at a `.invalid` host. It is
    safe only because no event fires: nothing errors, sessions are off, and the process
    exits. A change that made this import send something would try the network here.
    """
    assert _import_the_app_with(FAKE_DSN) == {"imported": True, "active": True}


# -- The settings fixed in code ----------------------------------------------


def test_the_sdk_is_configured_to_send_nothing_the_walk_cannot_scrub(
    collector: RecordingTransport,
) -> None:
    """Each of these closes a place where a token or a guest's details sit without a URL.

    Frame locals hold the guest routes' `token` argument bare. Request bodies hold RSVP
    answers. The SDK's own PII is the host session cookie and client addresses. Tracing,
    sessions and outbound trace headers would send something on every request rather
    than on failure.
    """
    options = sentry_sdk.get_client().options
    assert options["send_default_pii"] is False
    assert options["include_local_variables"] is False
    assert options["max_request_body_size"] == "never"
    assert options["traces_sample_rate"] is None
    assert options["trace_propagation_targets"] == []
    assert options["auto_session_tracking"] is False
    # What the SDK resolves those to, which is what its integrations actually read.
    assert options["data_collection"]["user_info"] is False
    assert options["data_collection"]["stack_frame_variables"] is False
    assert options["before_send"] is before_send
    assert options["environment"] == "test"


# -- The walk -----------------------------------------------------------------


def test_the_walk_redacts_every_string_wherever_it_sits() -> None:
    token = secrets.token_urlsafe(32)
    url = f"https://wedding.tapphouse.co/invites/{token}/rsvp"
    event: dict[str, Any] = {
        "request": {
            "url": url,
            "query_string": f"next=/invites/{token}",
            "headers": {"Referer": url, "Host": "wedding.tapphouse.co"},
        },
        "breadcrumbs": {
            "values": [
                {"message": f"GET /api/invites/{token}", "data": {"url": url}},
                {"category": "log", "data": {"extra": [1, {"deep": (url, None)}]}},
            ]
        },
        "exception": {"values": [{"type": "RuntimeError", "value": f"could not render {url}"}]},
        "logentry": {"message": "render failed for %s", "params": [url], "formatted": url},
        "extra": {"nested": {"list": [url, 7, True, None]}},
        "contexts": {"page": {f"/invites/{token}": "a key can hold a URL too"}},
        "level": "error",
        "timestamp": 1732147200.5,
    }

    scrubbed = scrub(event)

    assert token not in json.dumps(scrubbed)
    assert scrubbed["request"]["headers"]["Referer"] == (
        "https://wedding.tapphouse.co/invites/[token]/rsvp"
    )
    assert scrubbed["breadcrumbs"]["values"][0]["message"] == "GET /api/invites/[token]"
    assert scrubbed["contexts"]["page"] == {"/invites/[token]": "a key can hold a URL too"}
    # Everything that is not a string comes through as it was.
    assert scrubbed["extra"]["nested"]["list"][1:] == [7, True, None]
    assert scrubbed["timestamp"] == 1732147200.5
    assert scrubbed["request"]["headers"]["Host"] == "wedding.tapphouse.co"


def test_before_send_tags_the_event_with_the_request_id() -> None:
    def in_a_request() -> Any:
        bind_request_id("edge-9f1")
        return before_send({"tags": {"kept": "yes"}, "message": "boom"}, {})

    event = contextvars.copy_context().run(in_a_request)
    assert event["tags"] == {"kept": "yes", "request_id": "edge-9f1"}


def test_before_send_outside_a_request_adds_no_tag() -> None:
    event = contextvars.copy_context().run(before_send, {"message": "at startup"}, {})
    assert "tags" not in event


# -- The allowlist ------------------------------------------------------------


def test_the_allowlist_keeps_only_named_fields_where_free_form_data_collects() -> None:
    event: dict[str, Any] = {
        "breadcrumbs": {
            "values": [
                {
                    "type": "log",
                    "category": "app.mail",
                    "level": "info",
                    "timestamp": "2026-11-21T22:07:06Z",
                    "message": "email not sent: console transport",
                    "data": {"to": GUEST_EMAIL, "body": f"Dear {GUEST_NAME}"},
                },
                "not a breadcrumb",
            ]
        },
        "logentry": {"message": "for %s", "formatted": "for a guest", "params": [GUEST_NAME]},
        "extra": {"dietary_notes": GUEST_DIET},
        "exception": {"values": [{"type": "RuntimeError", "value": "boom"}]},
        "level": "error",
    }

    kept = keep_allowlisted(event)

    assert kept["breadcrumbs"]["values"] == [
        {
            "type": "log",
            "category": "app.mail",
            "level": "info",
            "timestamp": "2026-11-21T22:07:06Z",
            "message": "email not sent: console transport",
        },
        {},
    ]
    assert kept["logentry"] == {"message": "for %s", "formatted": "for a guest"}
    assert "extra" not in kept
    assert kept["exception"] == event["exception"]
    assert kept["level"] == "error"


@pytest.mark.parametrize("field", ["breadcrumbs", "logentry"])
def test_a_field_in_a_shape_the_allowlist_cannot_read_is_dropped(field: str) -> None:
    kept = keep_allowlisted({field: [GUEST_EMAIL], "level": "error"})
    assert kept == {"level": "error"}


# -- A crash, end to end ------------------------------------------------------


def _crash_while_rendering(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    """Make the guest page raise from inside the route, where `token` is a local.

    On the way it leaves a log breadcrumb and an HTTP breadcrumb, each quoting the
    guest's URL, and raises with the URL in the exception's message. The HTTP
    breadcrumb's URL sits in its `data`, which the allowlist removes.
    """
    url = f"https://wedding.tapphouse.co/invites/{token}"

    def broken(*args: object, **kwargs: object) -> None:
        logging.getLogger("app").info("rendering %s", url)
        sentry_sdk.add_breadcrumb(category="httplib", data={"url": url, "method": "GET"})
        raise RuntimeError(f"could not render {url}")

    monkeypatch.setattr("app.rsvp.segments_for", broken)


def _crash(token: str, request_id: str) -> Any:
    """Request the guest's page, getting the 500 a browser would rather than a raise."""
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as crashing:
        # A host following a guest's link sends their session along with it.
        crashing.cookies.set("savethedate_host", SESSION_COOKIE_VALUE)
        # The guest's path appears plain, encoded once and encoded twice: the query
        # string is written out by hand so its bytes reach the app exactly as typed.
        return crashing.get(
            f"/invites/{token}?from=/invites/{token}&next=%252Finvites%252F{token}",
            headers={
                "X-Request-ID": request_id,
                "Referer": f"https://wedding.tapphouse.co/login?next=%2Finvites%2F{token}%2Frsvp",
            },
        )


def test_an_unhandled_exception_reaches_the_collector_without_the_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, collector: RecordingTransport
) -> None:
    event_row = create_event(client)
    token = str(add_guest(client, event_row["id"], "Marcus Ellery")["invite_token"])
    _crash_while_rendering(monkeypatch, token)

    response = _crash(token, "crash-7")

    assert response.status_code == 500
    events = collector.events()
    assert len(events) == 1, "a crash nothing else logged is one event"
    (event,) = events
    assert event["tags"]["request_id"] == "crash-7"
    assert event["exception"]["values"][-1]["type"] == "RuntimeError"

    sent = collector.sent_bytes()
    assert token not in sent
    assert SESSION_COOKIE_VALUE not in sent
    # The URL WAS in all of these, so their redacted form proves the walk reached them.
    headers = event["request"]["headers"]
    assert headers["referer"] == (
        "https://wedding.tapphouse.co/login?next=%2Finvites%2F[token]%2Frsvp"
    )
    # The SDK decodes a query string once, so what was encoded twice on the wire is
    # encoded once in the event, which a literal-slash pattern would have let through.
    assert event["request"]["query_string"] == "from=/invites/[token]&next=%2Finvites%2F[token]"
    assert event["request"]["url"].endswith("/invites/[token]")
    assert event["exception"]["values"][-1]["value"].endswith("/invites/[token]")
    # The test client is itself an outbound httpx call the SDK instruments, so this is
    # where a trace header stamped on the app's own outbound requests would show.
    assert "baggage" not in headers and "sentry-trace" not in headers
    crumbs = event["breadcrumbs"]["values"]
    assert any(crumb.get("message", "").endswith("/invites/[token]") for crumb in crumbs)
    # The HTTP breadcrumb was recorded, and arrives without its `data`.
    assert any(crumb.get("category") == "httplib" for crumb in crumbs)
    assert all("data" not in crumb for crumb in crumbs)
    # The seeded guest's own details were never logged, and must not be sent either.
    assert "Marcus Ellery" not in sent
    assert "guest@example.com" not in sent


def test_guest_details_logged_in_a_request_never_reach_the_collector(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, collector: RecordingTransport
) -> None:
    """The JSON logs keep a guest's details; the collector gets none of them.

    Production's console mail transport logs a guest's address, subject and invitation
    as record extras, which the SDK copies into a breadcrumb. An ERROR record's extras
    become the event's own `extra`. Both happen here, in one request, before a crash.
    """
    # Imported here, not at the top: the session fixture's Alembic run calls
    # `fileConfig`, which disables every logger that already exists, and importing
    # `app.mail` at collection would create `app.mail`'s logger first and silence it.
    from app.mail import ConsoleTransport, EmailMessage

    event_row = create_event(client)
    token = str(add_guest(client, event_row["id"], "Marcus Ellery")["invite_token"])
    guest_details = {"guest_name": GUEST_NAME, "email": GUEST_EMAIL, "dietary_notes": GUEST_DIET}

    def broken(*args: object, **kwargs: object) -> None:
        ConsoleTransport().send(
            EmailMessage(
                to=GUEST_EMAIL,
                subject=f"Save the date, {GUEST_NAME}",
                text=f"Dear {GUEST_NAME}, we have noted: {GUEST_DIET}.",
                html=f"<p>Dear {GUEST_NAME}</p>",
            )
        )
        logging.getLogger("app").info("guest answered", extra=guest_details)
        try:
            raise RuntimeError("could not render the page")
        except RuntimeError:
            logging.getLogger("app").exception("render failed", extra=guest_details)
            raise

    monkeypatch.setattr("app.rsvp.segments_for", broken)

    # Keep a copy of each event as the SDK hands it over, before the allowlist runs.
    before_filter: list[str] = []

    def recording(event: dict[str, Any]) -> dict[str, Any]:
        before_filter.append(json.dumps(event, default=str))
        return keep_allowlisted(event)

    monkeypatch.setattr("app.error_collector.keep_allowlisted", recording)

    assert _crash(token, "crash-guest").status_code == 500

    sent = collector.sent_bytes()
    for detail in (GUEST_NAME, GUEST_EMAIL, GUEST_DIET):
        assert detail not in sent
    # Without this, a pass could mean the SDK never picked the details up at all. They
    # were in the events the SDK built (the breadcrumbs' `data`, the ERROR record's
    # `extra`), and only the allowlist kept them from being sent.
    assert before_filter
    for detail in (GUEST_NAME, GUEST_EMAIL, GUEST_DIET):
        assert any(detail in raw for raw in before_filter), detail
    # One event IS the guest-detail ERROR record, and the mail transport's breadcrumb
    # arrived, only without its `data`. (The request-failed line is a second event
    # here, because Starlette re-raises the error wrapped in an ExceptionGroup, which
    # the SDK's dedupe does not match.)
    events = collector.events()
    (event,) = [e for e in events if e.get("logentry", {}).get("message") == "render failed"]
    assert all("extra" not in e for e in events)
    messages = [crumb.get("message") for crumb in event["breadcrumbs"]["values"]]
    assert "email not sent: console transport" in messages
    assert "guest answered" in messages


def test_the_collector_does_not_change_the_response_to_a_crash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 500 a guest gets is the same with the collector on as with it off."""
    event_row = create_event(client)
    token = str(add_guest(client, event_row["id"], "Marcus Ellery")["invite_token"])
    _crash_while_rendering(monkeypatch, token)

    without = _crash(token, "crash-off")

    transport = RecordingTransport()
    assert init_error_collector(Settings(sentry_dsn=FAKE_DSN), transport=transport)
    try:
        with_collector = _crash(token, "crash-on")
    finally:
        _deactivate()

    assert transport.events(), "the collector should have been on for the second request"
    assert with_collector.status_code == without.status_code == 500
    assert with_collector.text == without.text == "Internal Server Error"
    assert with_collector.headers["X-Request-ID"] == "crash-on"
    assert without.headers["X-Request-ID"] == "crash-off"
    assert with_collector.headers["X-Robots-Tag"] == without.headers["X-Robots-Tag"]
    assert with_collector.headers["content-type"] == without.headers["content-type"]
