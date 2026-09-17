"""The pages a guest actually sees.

`/invites/{token}` is the link that goes in somebody's inbox, so it serves HTML. The
JSON view of the same data lives under `/api/invites/{token}`.

These routes are deliberately anonymous. The token IS the credential: there is no
guest login, no name lookup and no account, and adding one would be the single most
valuable thing about this page quietly disappearing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app import rsvp as rsvp_domain
from app.config import get_settings
from app.deps import DbSession
from app.models import Guest
from app.schemas import DIETARY_LABELS
from app.templating import templates

router = APIRouter(prefix="/invites", tags=["guest pages"])


def _is_review() -> bool:
    """Read per request, not at import, so a test can flip the deployment."""
    return get_settings().review_instance


def _not_found(request: Request) -> Response:
    """A dead link gets a page, not a stack trace — and never repeats the token back.

    Reflecting it would copy a bearer credential into logs, screenshots and any
    referrer header the page goes on to send.
    """
    return templates.TemplateResponse(
        request=request,
        name="not_found.html",
        context={"review_instance": _is_review()},
        status_code=status.HTTP_404_NOT_FOUND,
    )


def _context(request: Request, guest: Guest, db: DbSession) -> dict[str, Any]:
    segments = rsvp_domain.segments_for(guest.event_id, db)
    return {
        "review_instance": _is_review(),
        "event": guest.event,
        "guest": guest,
        "token": guest.invite_token,
        "segments": segments,
        "required_segments": [s for s in segments if not s.is_optional],
        "optional_segments": [s for s in segments if s.is_optional],
        "phase": rsvp_domain.phase(guest.event),
        "answer": guest.rsvp,
    }


def _rsvp_context(request: Request, guest: Guest, db: DbSession) -> dict[str, Any]:
    context = _context(request, guest, db)
    context["rows"] = rsvp_domain.form_rows(guest, context["segments"])
    context["dietary_options"] = list(DIETARY_LABELS.items())
    context["error"] = None
    return context


def _find(token: str, db: DbSession) -> Guest | None:
    return db.scalar(select(Guest).where(Guest.invite_token == token))


@router.get("/{token}", response_class=HTMLResponse)
def welcome(token: str, request: Request, db: DbSession) -> Response:
    guest = _find(token, db)
    if guest is None:
        return _not_found(request)
    return templates.TemplateResponse(
        request=request, name="welcome.html", context=_context(request, guest, db)
    )


@router.get("/{token}/wedding", response_class=HTMLResponse)
def wedding(token: str, request: Request, db: DbSession) -> Response:
    guest = _find(token, db)
    if guest is None:
        return _not_found(request)
    return templates.TemplateResponse(
        request=request, name="wedding.html", context=_context(request, guest, db)
    )


@router.get("/{token}/rsvp", response_class=HTMLResponse)
def rsvp_page(token: str, request: Request, db: DbSession) -> Response:
    guest = _find(token, db)
    if guest is None:
        return _not_found(request)
    return templates.TemplateResponse(
        request=request, name="rsvp.html", context=_rsvp_context(request, guest, db)
    )


@router.post("/{token}/rsvp", response_class=HTMLResponse)
async def submit(token: str, request: Request, db: DbSession) -> Response:
    guest = _find(token, db)
    if guest is None:
        return _not_found(request)

    phase = rsvp_domain.phase(guest.event)
    if phase != "open":
        # The window is enforced on the write, not only hidden in the template — a
        # stale tab or a resubmitted form must not slip an answer past the deadline.
        context = _rsvp_context(request, guest, db)
        context["error"] = (
            "RSVPs have not opened yet."
            if phase == "before_open"
            else "The deadline for replies has passed. Please get in touch with us directly."
        )
        return templates.TemplateResponse(
            request=request,
            name="rsvp.html",
            context=context,
            status_code=status.HTTP_403_FORBIDDEN,
        )

    segments = rsvp_domain.segments_for(guest.event_id, db)
    form = await request.form()

    try:
        payload = rsvp_domain.parse_form(form, guest, segments)
        rsvp_domain.validate(payload, guest, {segment.id for segment in segments})
    except rsvp_domain.RsvpRefused as refused:
        context = _rsvp_context(request, guest, db)
        context["error"] = refused.message
        return templates.TemplateResponse(
            request=request,
            name="rsvp.html",
            context=context,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    rsvp_domain.save(payload, guest, db)
    return templates.TemplateResponse(
        request=request, name="rsvp.html", context=_rsvp_context(request, guest, db)
    )


@router.get("/{token}/print", response_class=HTMLResponse)
def print_view(token: str, request: Request, db: DbSession) -> Response:
    guest = _find(token, db)
    if guest is None:
        return _not_found(request)
    return templates.TemplateResponse(
        request=request, name="print.html", context=_context(request, guest, db)
    )
