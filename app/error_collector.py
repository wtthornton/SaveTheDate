"""Errors sent off this box, with no invite token in them. TAP-12484.

`app.observability` writes every error to stdout as a JSON line, and those lines live
and die with the machine. This module also sends each one to a collector somewhere
else, so a crash can raise an alert and outlive the box. It uses `sentry-sdk`, and
its wire format is the Sentry protocol, which more than one collector accepts. The DSN
decides where events go. Nothing in the code does.

**Inert without a DSN.** `init_error_collector` returns before it even imports the
SDK, so an unconfigured deployment starts no thread, patches no class and opens no
socket. That is the stance `app.mail.build_transport` takes with a missing Resend key:
not configured means nothing happens, visibly, and never means half-configured. The
DSN is a credential (it is what lets a sender write to the project), so it lives in
`.env.prod`. Every other setting is fixed in this file or committed in
`docker-compose.prod.yml`, where a test can read it.

**Redaction comes last, for the same reason it does in the log formatter.**
`app.observability` puts it at the last step before bytes leave the process, so that a
call site that forgets cannot leak. For an event, that step is `before_send`: the SDK
has already assembled and serialized the event, request data, breadcrumbs and
exception values included, and the next thing that happens to it is the transport.
`scrub` walks every string in the event and every key, rather than a list of fields
known to hold a URL. A list is only as good as its author's guess about what the SDK
attaches; the SDK adds fields between releases, and the Referer header is a field
nobody thinks of until it is the leak. If `before_send` ever raises, the SDK drops
the event. A broken scrubber loses an event. It does not send one unscrubbed.

**What the walk cannot see is closed where it starts**, which is the formatter's rule
again. `redact` knows a token by the `/invites/` in front of it, and a bare token is
just 43 characters. Three places in an event hold values with no URL around them:

* Stack-frame locals. The guest routes take `token` as an argument, so every frame
  under one holds the credential bare. `include_local_variables=False`.
* Request bodies. The RSVP form carries guests' names and dietary notes, and a body
  can carry anything. `max_request_body_size="never"`.
* The SDK's personal data: cookies (the host session cookie among them), the client
  address and the user. `send_default_pii=False`.

**The request id is read at send time**, from the same context variable the formatter
reads, and written to the event as a tag. An event and its JSON log line therefore
carry the same id, and nothing keeps a second copy of it that could drift.

**Errors only.** No tracing, no release-health sessions, and no trace headers added
to this app's own outbound requests (`trace_propagation_targets=[]`; by default the
SDK stamps `sentry-trace` and `baggage`, which name the environment and release, on
every HTTP call it sees, the mail provider's included). None of it has been asked for,
tracing and sessions would send something on every request rather than on failure,
and each is one more payload whose contents would have to be argued about.

**One crash, one event.** `app.main.log_request` logs the exception before re-raising
it, and the SDK's logging integration turns that ERROR line into an event. The outer
ASGI capture then sees the same exception object, and the SDK's dedupe drops it. The
response is not touched: the 500 still comes from `app.main.unhandled_error`, and the
SDK re-raises everything it catches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from app.config import Settings
from app.observability import current_request_id, redact

if TYPE_CHECKING:
    from sentry_sdk.transport import Transport
    from sentry_sdk.types import Event, Hint

# The tag an event's request id travels under. The same id is the `request_id` field
# of every JSON log line the request wrote, and the `X-Request-ID` the caller got.
REQUEST_ID_TAG = "request_id"


def scrub(value: Any) -> Any:
    """`value` with `redact` applied to every string in it, keys included.

    Containers come back as new objects; everything that is not a string, a dict, a
    list or a tuple is returned untouched. An event reaches `before_send` already
    serialized, so in practice those are numbers, booleans and None.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {scrub(key): scrub(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(item) for item in value]
    return value


def before_send(event: Event, hint: Hint) -> Event:
    """Tag the event with its request id, then scrub all of it."""
    request_id = current_request_id()
    if request_id is not None:
        tags = event.get("tags") or {}
        event["tags"] = {**tags, REQUEST_ID_TAG: request_id}
    return cast("Event", scrub(event))


def init_error_collector(settings: Settings, *, transport: Transport | None = None) -> bool:
    """Start sending errors if, and only if, a DSN is configured. True if it did.

    `transport` replaces the SDK's HTTP transport, and exists so a test can catch the
    exact bytes that would have left the process without anything opening a socket.
    """
    if not settings.sentry_dsn:
        return False

    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        transport=transport,
        before_send=before_send,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        traces_sample_rate=None,
        trace_propagation_targets=[],
        auto_session_tracking=False,
    )
    return True
