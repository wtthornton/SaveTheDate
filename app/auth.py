"""Host authentication. TAP-7725.

Three decisions worth stating, because each closes off a plausible alternative:

**Cookies, not bearer tokens.** The host UI (TAP-7730) is server-rendered Jinja like
the guest pages. A bearer token would need JavaScript to attach it, in a stack that has
none on purpose.

**Server-side sessions, not signed cookies.** A signed cookie cannot be revoked before
it expires. A row can: signing out ends the session, and a stolen cookie can be killed.

**Registration is closed unless a bootstrap token is configured.** This is a
single-wedding install whose whole point is to be reachable from any inbox. A public
`/auth/register` on that URL is an invitation to strangers to create events on it.

Guests are not touched by any of this. `GET /invites/{token}` and its RSVP write stay
anonymous; the link is the credential, and adding a guest login would destroy the one
thing this does better than every competing product.
"""

from __future__ import annotations

import hashlib
import secrets
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.deps import DbSession, Now
from app.models import Host, HostSession

_hasher = PasswordHasher()

# Verified against when no host matches the address given, so a wrong email costs the
# same time as a wrong password. Without it, login is a membership oracle for the
# host's email address.
_DUMMY_HASH = _hasher.hash("a password that is never anybody's")


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(stored: str, raw: str) -> bool:
    try:
        return _hasher.verify(stored, raw)
    except (VerifyMismatchError, InvalidHashError):
        return False


def burn_dummy_verification() -> None:
    """Spend the same time as a real check when the address is unknown.

    The mismatch is the point, not an error being hidden: `verify` signals "wrong
    password" by raising, and this call exists only to cost the same argon2 work as a
    real attempt. Anything else going wrong here still propagates.
    """
    with suppress(VerifyMismatchError):
        _hasher.verify(_DUMMY_HASH, "not the password either")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(host: Host, db: Session, now: datetime) -> str:
    """Create a session row and return the raw cookie value, which is never stored."""
    token = secrets.token_urlsafe(32)
    lifetime = timedelta(hours=get_settings().session_lifetime_hours)
    db.add(
        HostSession(
            host_id=host.id,
            token_hash=_digest(token),
            expires_at=now + lifetime,
        )
    )
    db.commit()
    return token


def end_session(token: str | None, db: Session) -> None:
    """Sign out. Deliberately silent about whether the session existed."""
    if not token:
        return
    session = db.scalar(select(HostSession).where(HostSession.token_hash == _digest(token)))
    if session is not None:
        db.delete(session)
        db.commit()


def host_for_token(token: str | None, db: Session, now: datetime) -> Host | None:
    if not token:
        return None
    session = db.scalar(select(HostSession).where(HostSession.token_hash == _digest(token)))
    if session is None:
        return None
    if session.expires_at <= now:
        # Expired sessions are cleared on sight rather than left to accumulate; there
        # is no scheduled job in this deployment and at this scale there need not be.
        db.delete(session)
        db.commit()
        return None
    return session.host


def current_host(request: Request, db: DbSession, now: Now) -> Host:
    """The signed-in host, or 401. The dependency every host route hangs off."""
    token = request.cookies.get(get_settings().session_cookie_name)
    host = host_for_token(token, db, now)
    if host is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="sign in to use this endpoint",
            headers={"WWW-Authenticate": "Cookie"},
        )
    return host


CurrentHost = Annotated[Host, Depends(current_host)]
