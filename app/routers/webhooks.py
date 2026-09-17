"""Callbacks from the mail provider. TAP-7731.

A bounced invite and a guest who ignored one look identical from here, which is the
failure this endpoint exists to prevent. The provider tells us which it was, and the
answer is recorded against the guest so a host can see it.

Public by necessity — the provider has no session — so it is authenticated by a shared
secret over the raw body, and refuses everything when no secret is configured.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app import invitations, mail
from app.config import get_settings
from app.deps import DbSession

router = APIRouter(prefix="/webhooks", tags=["ops"])

SIGNATURE_HEADER = "x-savethedate-signature"


@router.post("/email", status_code=status.HTTP_204_NO_CONTENT)
async def email_event(request: Request, db: DbSession) -> Response:
    """Apply one delivery event.

    Answers 204 for a message id we do not recognize, on purpose: a provider that gets
    an error retries, and there is nothing here to retry. It answers 401 only when the
    caller cannot prove who it is.
    """
    secret = get_settings().email_webhook_secret
    raw = await request.body()

    if not mail.signature_is_valid(secret or "", raw, request.headers.get(SIGNATURE_HEADER, "")):
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        payload = await request.json()
    except ValueError:
        # Malformed JSON from a correctly signed caller is not something a retry fixes.
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    if not isinstance(payload, dict):
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    event_name = str(payload.get("type", ""))
    data = payload.get("data")
    message_id = str(data.get("email_id", "")) if isinstance(data, dict) else ""

    if message_id:
        invitations.record_provider_event(db, message_id, event_name)

    return Response(status_code=status.HTTP_204_NO_CONTENT)
