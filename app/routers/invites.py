from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.deps import DbSession
from app.models import Guest, Rsvp
from app.schemas import EventOut, InviteOut, RsvpCreate, RsvpOut

router = APIRouter(prefix="/invites", tags=["invites"])


def _load_guest(token: str, db: DbSession) -> Guest:
    guest = db.scalar(select(Guest).where(Guest.invite_token == token))
    if guest is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invite not found")
    return guest


@router.get("/{token}", response_model=InviteOut)
def get_invite(token: str, db: DbSession) -> InviteOut:
    guest = _load_guest(token, db)
    return InviteOut(
        event=EventOut.model_validate(guest.event),
        guest_name=guest.name,
        party_size=guest.party_size,
        rsvp=RsvpOut.model_validate(guest.rsvp) if guest.rsvp is not None else None,
    )


@router.put("/{token}/rsvp", response_model=RsvpOut)
def submit_rsvp(token: str, payload: RsvpCreate, db: DbSession) -> Rsvp:
    guest = _load_guest(token, db)

    if payload.party_size > guest.party_size:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invitation covers at most {guest.party_size} guest(s)",
        )
    if payload.attending and payload.party_size < 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="an attending RSVP must claim at least one seat",
        )

    # A guest may change their mind, so the same token updates the existing RSVP in place.
    rsvp = guest.rsvp
    if rsvp is None:
        rsvp = Rsvp(guest_id=guest.id, **payload.model_dump())
        db.add(rsvp)
    else:
        rsvp.attending = payload.attending
        rsvp.party_size = payload.party_size
        rsvp.note = payload.note

    db.commit()
    return rsvp
