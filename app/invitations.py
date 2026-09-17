"""Composing and sending invitations and reminders. TAP-7731.

Separated from `app.mail` (which only knows how to put a message on the wire) and from
the routes (which only know about HTTP), so the interesting decisions — who gets an
email, what it says, what is recorded — are testable on their own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.mail import EmailMessage, EmailRefused, Transport
from app.models import Delivery, Event, Guest

INVITE = "invite"
REMINDER = "reminder"


@dataclass(frozen=True)
class SendReport:
    sent: int
    failed: int
    skipped: int

    @property
    def attempted(self) -> int:
        return self.sent + self.failed


def _invite_url(base_url: str, guest: Guest) -> str:
    return f"{base_url.rstrip('/')}/invites/{guest.invite_token}"


def compose(event: Event, guest: Guest, base_url: str, kind: str) -> EmailMessage:
    """One message for one invitation.

    The link is the whole point of the email, so it appears as plain text as well as a
    hyperlink: plenty of mail clients strip or rewrite anchors, and a guest who can see
    the URL can still get there.
    """
    url = _invite_url(base_url, guest)
    when = event.event_date.strftime("%B %-d, %Y") if event.event_date else "Date to follow"

    if kind == REMINDER:
        subject = f"A gentle reminder — {event.title}"
        opening = "We have not heard from you yet, and we would love to know if you can come."
    else:
        subject = f"You are invited — {event.title}"
        opening = "We would be delighted if you could join us."

    text = (
        f"Dear {guest.name},\n\n"
        f"{opening}\n\n"
        f"{event.title}\n"
        f"{when}"
        f"{' · ' + event.location if event.location else ''}\n\n"
        f"Everything you need, and the form to reply, is here:\n"
        f"{url}\n\n"
        f"The link is personal to you — please do not forward it.\n\n"
        f"With love,\n{event.host_name}\n"
    )
    html = (
        f"<p>Dear {guest.name},</p>"
        f"<p>{opening}</p>"
        f"<p><strong>{event.title}</strong><br>{when}"
        f"{' &middot; ' + event.location if event.location else ''}</p>"
        f'<p><a href="{url}">Open your invitation</a></p>'
        f"<p>Or paste this into your browser:<br>{url}</p>"
        f"<p>The link is personal to you — please do not forward it.</p>"
        f"<p>With love,<br>{event.host_name}</p>"
    )
    return EmailMessage(to=guest.email or "", subject=subject, text=text, html=html)


def recipients(event_id: uuid.UUID, db: Session, kind: str) -> list[Guest]:
    """Who should get this send.

    An invite goes to everyone with an address who has not already had one — sending
    the same invitation twice reads as a mistake and invites a second RSVP.

    A reminder goes only to people who have not replied at all. Someone who declined
    has answered; chasing them is the rudest thing this software could do.
    """
    guests = list(
        db.scalars(
            select(Guest)
            .where(Guest.event_id == event_id)
            .order_by(Guest.name)
            .options(selectinload(Guest.deliveries), selectinload(Guest.rsvp))
        )
    )

    chosen: list[Guest] = []
    for guest in guests:
        if not guest.email:
            continue
        if kind == REMINDER:
            if guest.rsvp is not None:
                continue
        elif any(
            delivery.kind == INVITE and delivery.status != "failed" for delivery in guest.deliveries
        ):
            continue
        chosen.append(guest)
    return chosen


def send(
    event: Event,
    db: Session,
    transport: Transport,
    base_url: str,
    kind: str = INVITE,
) -> SendReport:
    """Send to everyone who should get one, recording the outcome per guest.

    A provider refusing one address must not stop the rest of the list, so each send is
    recorded and the loop continues. Nothing is swallowed: a failure becomes a
    `Delivery` row with status "failed" and the provider's reason, which is visible on
    the dashboard — the opposite of a silent bounce.
    """
    everyone = list(
        db.scalars(
            select(Guest).where(Guest.event_id == event.id).options(selectinload(Guest.deliveries))
        )
    )
    chosen = recipients(event.id, db, kind)
    chosen_ids = {guest.id for guest in chosen}

    sent = failed = 0
    for guest in chosen:
        delivery = Delivery(guest_id=guest.id, kind=kind, status="queued")
        db.add(delivery)
        try:
            delivery.provider_message_id = transport.send(compose(event, guest, base_url, kind))
            delivery.status = "sent"
            sent += 1
        except EmailRefused as refused:
            delivery.status = "failed"
            delivery.error = str(refused)
            failed += 1

    db.commit()
    return SendReport(sent=sent, failed=failed, skipped=len(everyone) - len(chosen_ids))


def record_provider_event(
    db: Session, provider_message_id: str, event_name: str
) -> Delivery | None:
    """Apply a bounce, complaint or delivery confirmation from the provider's webhook.

    Returns None for a message id nothing here recognizes, which the route answers 200
    to anyway: a provider that gets an error retries, and there is nothing to retry.
    """
    delivery = db.scalar(
        select(Delivery).where(Delivery.provider_message_id == provider_message_id)
    )
    if delivery is None:
        return None

    status = {
        "email.delivered": "delivered",
        "email.bounced": "bounced",
        "email.complained": "complained",
        "email.delivery_delayed": "sent",
    }.get(event_name)
    if status is None:
        return delivery

    delivery.status = status
    if status in {"bounced", "complained"}:
        delivery.error = event_name
    db.commit()
    return delivery
