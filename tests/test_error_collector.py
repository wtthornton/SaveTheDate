"""Errors reach an off-box collector, carry their request id, and never an invite token.

TAP-12484's acceptance, one section each:

* with no DSN the app runs exactly as today, the SDK never initialised;
* an unhandled exception becomes an event carrying the request id;
* no invite token reaches the collector, checked on the bytes that would be sent.

The last is checked on the SERIALIZED ENVELOPE, the payload the HTTP transport would
post, because that is what leaves the process. A test reading the event dict before
`before_send` would see the tokens, and one reading it after would miss anything the
SDK adds on the way to the wire. Nothing here opens a socket: the transport below
keeps envelopes in a list.
"""

from __future__ import annotations

import contextvars
import importlib
import json
import logging
import secrets
from collections.abc import Iterator
from typing import Any, cast

import pytest
import sentry_sdk
from fastapi.testclient import TestClient
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from app.config import Settings
from app.error_collector import before_send, init_error_collector, scrub
from app.observability import bind_request_id
from tests.factories import add_guest, create_event

# Shaped like a DSN so the SDK accepts it, on a reserved name that resolves nowhere.
# The transport below means nothing would ever dial it anyway.
FAKE_DSN = "https://public@collector.invalid/1"

# Stands in for a signed-in host's session cookie, which is a credential too.
SESSION_COOKIE_VALUE = "host-session-" + "s3cr3t" * 6


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


def test_the_app_as_configured_here_starts_no_collector() -> None:
    """Importing the app is what calls `init_error_collector`; with no DSN, nothing starts."""
    importlib.import_module("app.main")
    assert not sentry_sdk.get_client().is_active()


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


# -- A crash, end to end ------------------------------------------------------


def _crash_while_rendering(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    """Make the guest page raise from inside the route, where `token` is a local.

    On the way it leaves a log breadcrumb and an HTTP breadcrumb, each quoting the
    guest's URL, and raises with the URL in the exception's message.
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
        return crashing.get(
            f"/invites/{token}",
            params={"from": f"/invites/{token}"},
            headers={
                "X-Request-ID": request_id,
                "Referer": f"https://wedding.tapphouse.co/invites/{token}/rsvp",
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
    assert len(events) == 1, "one crash, one event"
    (event,) = events
    assert event["tags"]["request_id"] == "crash-7"
    assert event["exception"]["values"][-1]["type"] == "RuntimeError"

    sent = collector.sent_bytes()
    assert token not in sent
    assert SESSION_COOKIE_VALUE not in sent
    # The URL WAS in all of these, so their redacted form proves the walk reached them.
    headers = event["request"]["headers"]
    assert headers["referer"].endswith("/invites/[token]/rsvp")
    assert event["request"]["query_string"] == "from=/invites/[token]"
    assert event["request"]["url"].endswith("/invites/[token]")
    assert event["exception"]["values"][-1]["value"].endswith("/invites/[token]")
    # The test client is itself an outbound httpx call the SDK instruments, so this is
    # where a trace header stamped on the app's own outbound requests would show.
    assert "baggage" not in headers and "sentry-trace" not in headers
    crumbs = event["breadcrumbs"]["values"]
    assert any(crumb.get("message", "").endswith("/invites/[token]") for crumb in crumbs)
    assert any(
        crumb.get("data", {}).get("url", "").endswith("/invites/[token]") for crumb in crumbs
    )


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
