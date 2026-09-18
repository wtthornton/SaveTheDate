"""The pages you can reach without a token.

Every other guest-facing route in this app hangs off `guests.invite_token`. These two
do not, and that is the whole of what makes them delicate: whatever is written here is
written to the open internet.

**Both of them live at `/`**, and the hostname decides which one you get: the card on a
hostname listed in `SAVE_THE_DATE_HOSTS`, the welcome on every other.

* The welcome (TAP-7775). For somebody who typed the wedding domain, or was handed it
  by a relative without the link. Telling them their invitation could not be found
  reads as their mistake when they have not made one.
* The card (TAP-7781). The public save-the-date, sent broadly long before the guest
  list is final.

There is deliberately **no `/save-the-date` path**. An earlier version served the card
at one, so that it could be reviewed without a DNS entry, which meant the card was also
reachable on the wedding hostname — `dev-wedding.tapphouse.co/save-the-date`. Bill
rejected that on sight, and he was right: a page belongs on the hostname it is for, and
one reachable from two names is one that gets linked to by the wrong one. Local review
uses `savethedate.localhost`, which every browser resolves to loopback, so nothing is
harder to look at.

**Neither page reads a guest row, and neither takes input.** No lookup form, ever: a
name box on an anonymous page is a guest-list oracle, and anyone could walk it to learn
who was invited. TAP-7725 rejected the pattern on friction grounds; it is also a
disclosure.

**The couple, the date and the place are hard-coded here** rather than read from
`events`, which is deliberate and worth explaining, because everything a *guest* sees
comes from the database. These pages belong to no event row. Choosing one without a
token or a signed-in host would mean inventing a "primary event" — a content model, on
the exact axis plan §12 says not to build one. Hard-coding also means the page renders
on an empty production database, which is the state it will be in on its first day.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Guest
from app.templating import templates

router = APIRouter(tags=["public pages"])

# One wedding's facts, on one wedding's domain. See the module docstring.
COUPLE = ("Lisa", "Bill")
WEDDING_DAY = date(2028, 2, 20)

# The island, and nothing finer. 183 Stargrass Ln is a private home; it appears on no
# page that does not require a token.
WEDDING_PLACE = "Port Aransas, Texas"


def _context() -> dict[str, Any]:
    settings = get_settings()
    return {
        "review_instance": settings.review_instance,
        "couple": COUPLE,
        "wedding_day": WEDDING_DAY,
        "wedding_place": WEDDING_PLACE,
        # Per deployment, from PUBLIC_BASE_URL — see `Settings.wedding_site_url`. The
        # couple and the date are the same wedding everywhere; the link out of the card
        # is not, and hard-coding it sent the dev card to the production site.
        "wedding_site_url": settings.wedding_site_url,
    }


def serves_the_card(request: Request) -> bool:
    """Does this request's hostname show the card at its root?

    The port is stripped: a tunnel or a proxy is free to pass `host:port`, and a card
    that quietly stopped appearing would be read as a DNS fault rather than as string
    matching. IPv6 literals arrive bracketed (`[::1]:8000`), so the split is on the
    last colon only when what follows it is a port.
    """
    hostname = request.headers.get("host", "").casefold()
    head, separator, tail = hostname.rpartition(":")
    if separator and tail.isdigit():
        hostname = head
    return hostname.strip("[]") in get_settings().save_the_date_host_list


def _review_shortcut() -> Response | None:
    """On a REVIEW INSTANCE only, the wedding root opens the guest site itself.

    `dev-wedding.tapphouse.co/` served the same minimal welcome as production, because
    the four pages worth looking at hang off `/invites/{token}` and a token is not
    something anyone keeps in their head. Reviewing the site therefore meant finding a
    43-character string in a gitignored file first, and when the saved link had gone
    stale the site read as simply *gone*. That is what this removes.

    **It cannot exist in production.** `review_instance` is `False` by default, is
    `"false"` in `docker-compose.prod.yml`, and `test_production_stack.py` fails if that
    ever drifts — so this is gated on the one flag in the system that is already
    asserted never to be true out there. Nothing here changes a production response:
    the session below is opened INSIDE this branch rather than taken as a route
    dependency, so the production root still touches no database at all and still
    renders on an empty one, as this module's docstring promises.

    **It is not a lookup.** No name, no input, no way to ask for a particular guest —
    it is one fixed row, chosen by the query and not by the caller. The invariant that
    matters is that an anonymous page must never answer questions about who was
    invited, and a constant answers no question. On a review instance every guest is
    invented anyway, which the draft banner on the destination says out loud.

    The guest with the most seats, so the RSVP page being reviewed is the one with the
    most in it; `id` breaks the tie so the choice is stable rather than whatever the
    planner felt like returning. Reseeding re-keys tokens, so this resolves per request
    instead of being written down anywhere.

    Returns `None` when there is nothing to open — an empty review database is an
    ordinary state, and the welcome is the honest page for it.
    """
    if not get_settings().review_instance:
        return None
    with SessionLocal() as db:
        token = db.scalar(
            select(Guest.invite_token).order_by(Guest.party_size.desc(), Guest.id).limit(1)
        )
    if token is None:
        return None
    # 302, never 301. A permanent redirect is cached by the browser and would outlive
    # the tokens it points at: reseed the review data and every reviewer keeps landing
    # on a dead invitation, with nothing on the page to say why. This project has been
    # bitten by a stale cache presenting as "the site is broken" once already.
    return RedirectResponse(f"/invites/{token}", status_code=status.HTTP_302_FOUND)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def front_door(request: Request) -> Response:
    """The card on a save-the-date hostname, the welcome everywhere else.

    Answering **200** rather than 404 is deliberate. This is a real page at a real
    address, and it says so to a cache, a monitor and a screen reader alike. The "there
    is nothing here without a token" signal is carried by what the page says, which a
    person can read, rather than by a status code, which they cannot. A bad token still
    answers 404 — the two pages are not merged.

    The one exception is a review instance, which opens the guest site here instead of
    the welcome — see `_review_shortcut`. The card's hostname is unaffected: the card is
    the thing being reviewed there.
    """
    if serves_the_card(request):
        return templates.TemplateResponse(
            request=request, name="save_the_date.html", context=_context()
        )
    opened = _review_shortcut()
    if opened is not None:
        return opened
    return templates.TemplateResponse(
        request=request, name="public_welcome.html", context=_context()
    )
