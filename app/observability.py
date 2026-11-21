"""Logs a machine can read, a request id to tie them together, and no credentials. TAP-7734.

Three things live here, and they live together because each one leans on the others:

**One JSON object per line, on stdout.** `docker compose logs` is where anyone looks
first on this box, and a line that is already JSON can be filtered with `jq` for a
status code or a request id without a regex. Standard library only: a formatter is
forty lines, and a logging framework is one more thing to keep current.

**A request id on every line a request causes.** It is set once, by the middleware in
`app.main`, held in a context variable, and read here by the formatter, so a log call
deep in a router does not have to be handed it. The same id goes back to the caller as
`X-Request-ID`, which is what lets a host's "it broke at 9:14" become one `grep`.

**Invite tokens in URLs never reach the output.** `/invites/{token}` is a bearer
credential, and it sits in the URL of every guest request. Before this module, uvicorn's
own access log wrote each one to stdout in full. The redaction is in the FORMATTER, the
last step before bytes leave the process, rather than at each call site: a call site
that forgets cannot leak a URL, and neither can a library's error message or a traceback
that happens to quote one.

What it does NOT do is recognise a bare token. Nothing can tell 43 URL-safe characters
from any other string, so a token logged on its own, outside a URL, would pass through.
The places that could do that are closed at their source instead: the engine sets
`hide_parameters` so a failed statement cannot quote a bound token, and nothing logs a
guest row. A new log call that includes `guest.invite_token` directly would leak it.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# What the redaction leaves in place of a token. Plain enough that a reader of the log
# knows a token was there and that its absence is deliberate.
REDACTED = "[token]"

# `/invites/<token>` and `/api/invites/<token>`, followed by anything. Tokens are
# `secrets.token_urlsafe`, so the URL-safe base64 alphabet is the whole of one; this
# also catches a made-up token someone typed, which is right — a near-miss of a real
# token is not something a log should hold either.
_INVITE_TOKEN = re.compile(r"(/invites/)[A-Za-z0-9_-]+")

# A caller may send its own id so its logs and ours line up. Accepted only if it is
# short and dull; anything else is replaced, so a header cannot inject a newline, a
# quote or a megabyte into every log line the request writes.
_ACCEPTABLE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")

REQUEST_ID_HEADER = "X-Request-ID"

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes every `LogRecord` carries. Anything else on a record arrived through
# `extra=` and belongs in the JSON object as a field of its own — except uvicorn's
# `color_message`, which repeats `message` with terminal escape codes in it.
_STANDARD_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "color_message"}
)


def redact(text: str) -> str:
    """Replace every invite token in `text`. Idempotent."""
    return _INVITE_TOKEN.sub(rf"\g<1>{REDACTED}", text)


def request_id_from(header_value: str | None) -> str:
    """The caller's id if it is acceptable, otherwise a fresh one."""
    if header_value is not None and _ACCEPTABLE_REQUEST_ID.fullmatch(header_value):
        return header_value
    return uuid.uuid4().hex


def bind_request_id(value: str) -> None:
    _request_id.set(value)


def current_request_id() -> str | None:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    """One JSON object per record, with every string value passed through `redact`."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = current_request_id()
        if request_id is not None:
            entry["request_id"] = request_id
        for key, value in vars(record).items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return redact(json.dumps(entry, default=str))


def configure_logging(level: int | None = None) -> None:
    """Send every logger, uvicorn's included, through one JSON handler on stdout.

    Called when `app.main` is imported. uvicorn has configured its own loggers by then,
    so this replaces their handlers rather than racing them, and it holds however the
    app is launched — the production image, the review instance, or a bare `uvicorn`.

    uvicorn's access log is switched off here rather than with `--no-access-log` on each
    command line, because a flag has to be remembered at every launch site and this
    does not. The request line it wrote is replaced by `app.main`'s own, which carries
    the request id and a duration and never held the token in the first place.

    The level is uvicorn's own `--log-level` unless one is passed. That flag is the knob
    every launch site already sets — `info` in the image, `warning` under the visual
    tests — and ignoring it once filled the visual tests' undrained stdout pipe with
    request lines until uvicorn blocked writing to it and every page load timed out.
    """
    if level is None:
        # uvicorn sets this logger's level from `--log-level` before importing the app.
        # NOTSET means uvicorn is not the launcher (the test suite, a script).
        level = logging.getLogger("uvicorn.error").level or logging.INFO

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    # Replace only a handler this function installed before, so a second call does not
    # print every line twice, and a handler someone else attached (pytest's capture,
    # for one) is left where it is.
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing.formatter, JsonFormatter):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
    access.disabled = True
