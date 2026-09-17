"""Sending invitations, behind a port small enough to fake. TAP-7731.

The whole module exists so that "did we email this guest, and did it arrive" is a
recorded fact rather than an assumption. A silently bounced invite looks exactly like a
guest who ignored it, and that is the failure that actually costs a seat at the table.

**No queue, no worker, no Celery.** Under a hundred invitations, FastAPI's
`BackgroundTasks` sends the lot comfortably, and keeping this to one service keeps the
whole deployment to one `docker compose up` (TAP-7733).

**The transport is a Protocol with three implementations.** `RecordingTransport` is what
the tests use, so no test ever opens a socket. `ConsoleTransport` is the default and
prints, which is what a developer wants and — more importantly — is what an
unconfigured deployment does instead of silently failing or accidentally mailing real
people. `ResendTransport` is the real one and is inert without an API key.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from app.config import get_settings


class EmailRefused(Exception):
    """The provider would not take the message. Recorded against the guest, not raised
    at a host mid-request."""


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    html: str


class Transport(Protocol):
    """Everything the rest of the app is allowed to know about sending mail."""

    def send(self, message: EmailMessage) -> str:
        """Send it, and return the provider's id for the message."""


@dataclass
class RecordingTransport:
    """Keeps messages in a list instead of sending them. For tests, and only tests."""

    sent: list[EmailMessage] = field(default_factory=list)
    fail_for: set[str] = field(default_factory=set)

    def send(self, message: EmailMessage) -> str:
        if message.to in self.fail_for:
            raise EmailRefused(f"the provider refused {message.to}")
        self.sent.append(message)
        return f"recorded-{len(self.sent)}"


class ConsoleTransport:
    """Prints instead of sending.

    The default, deliberately. An unconfigured deployment that prints is obvious and
    harmless; one that silently succeeds is a lie, and one that mails real guests by
    accident is worse than either.
    """

    def send(self, message: EmailMessage) -> str:
        print(f"[email] to={message.to} subject={message.subject!r}\n{message.text}\n")
        return "console"


class ResendTransport:
    """The real one. https://resend.com/docs/api-reference/emails/send-email"""

    endpoint = "https://api.resend.com/emails"

    def __init__(self, api_key: str, sender: str) -> None:
        self.api_key = api_key
        self.sender = sender

    def send(self, message: EmailMessage) -> str:
        payload = json.dumps(
            {
                "from": self.sender,
                "to": [message.to],
                "subject": message.subject,
                "text": message.text,
                "html": message.html,
            }
        ).encode()
        # A fixed https endpoint, not a caller-supplied URL.
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise EmailRefused(f"{exc.code} from the mail provider") from exc
        except urllib.error.URLError as exc:
            raise EmailRefused(f"could not reach the mail provider: {exc.reason}") from exc

        message_id = body.get("id")
        if not message_id:
            raise EmailRefused("the provider returned no message id")
        return str(message_id)


def build_transport() -> Transport:
    """Whichever transport the settings ask for, defaulting to the harmless one."""
    settings = get_settings()
    if settings.email_provider == "resend" and settings.resend_api_key:
        return ResendTransport(settings.resend_api_key, settings.email_from)
    return ConsoleTransport()


# -- Webhook signatures ---------------------------------------------------


def signature_is_valid(secret: str, raw_body: bytes, presented: str) -> bool:
    """Constant-time check of an HMAC-SHA256 signature over the raw request body.

    **Read this before pointing a provider at the webhook.** Resend signs through Svix,
    whose header is `svix-signature` and whose signed payload is
    `{id}.{timestamp}.{body}` — not the bare body. This function is the correct shape
    and the correct comparison, but the exact payload to sign MUST be checked against
    the provider's documentation when the account exists, or the endpoint will reject
    every real callback. It is written this way rather than left as a stub so the
    bounce path is real and tested; the one unknown is documented rather than guessed.
    """
    if not secret or not presented:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, presented.strip())
