"""Register, sign in, sign out. TAP-7725.

Guest routes are not affected by anything in here and must never be. `/invites/{token}`
and its RSVP write stay anonymous: the link is the credential, and asking an older
relative to make an account is the one thing this does better than Joy, Zola and Minted.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import auth
from app.auth import CurrentHost
from app.config import get_settings
from app.deps import DbSession, Now
from app.models import Host
from app.schemas import HostLogin, HostOut, HostRegister

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_lifetime_hours * 3600,
        httponly=True,
        # Lax rather than Strict: a host following a link to their own dashboard from
        # an email should arrive signed in. Nothing here is a state-changing GET.
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


@router.post("/register", response_model=HostOut, status_code=status.HTTP_201_CREATED)
def register(payload: HostRegister, db: DbSession) -> Host:
    """Create the host account. Closed unless a bootstrap token is configured.

    This install is meant to be reachable from any inbox, so an open registration
    endpoint on that URL would let strangers create events on it. Closed by default
    means forgetting to configure the token fails safe rather than wide open.
    """
    settings = get_settings()
    expected = settings.host_registration_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="registration is closed",
        )
    # Compared in constant time so the endpoint is not an oracle for the token.
    if not secrets.compare_digest(payload.registration_token, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="registration is closed",
        )

    host = Host(
        email=payload.email.strip().casefold(),
        password_hash=auth.hash_password(payload.password),
    )
    db.add(host)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="that email is already registered"
        ) from exc
    return host


@router.post("/login", response_model=HostOut)
def login(payload: HostLogin, response: Response, db: DbSession, now: Now) -> Host:
    """Sign in, and set the session cookie.

    One message for both failures. Saying "no such account" separately from "wrong
    password" turns this into a membership check on the host's email address.
    """
    email = payload.email.strip().casefold()
    host = db.scalar(select(Host).where(Host.email == email))

    if host is None:
        # Spend the same time as a real verification, so a wrong address and a wrong
        # password are not distinguishable by how long the answer takes.
        auth.burn_dummy_verification()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="those details do not match"
        )

    if not auth.verify_password(host.password_hash, payload.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="those details do not match"
        )

    _set_session_cookie(response, auth.start_session(host, db, now))
    return host


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: DbSession) -> Response:
    """Sign out. The session row goes, so the cookie is dead even if it was copied."""
    settings = get_settings()
    auth.end_session(request.cookies.get(settings.session_cookie_name), db)
    response.delete_cookie(key=settings.session_cookie_name, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=dict(response.headers))


@router.get("/me", response_model=HostOut)
def me(host: CurrentHost) -> Host:
    """Who the cookie belongs to. 401 when there is no valid session."""
    return host
