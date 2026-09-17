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

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.templating import templates

router = APIRouter(tags=["public pages"])

# One wedding's facts, on one wedding's domain. See the module docstring.
COUPLE = ("Lisa", "Bill")
WEDDING_DAY = date(2028, 2, 13)

# The island, and nothing finer. 183 Stargrass Ln is a private home; it appears on no
# page that does not require a token.
WEDDING_PLACE = "Port Aransas, Texas"

# Where the card sends somebody who wants more than a date. Absolute, because the card
# is served on its own hostname and a relative link would keep them there.
WEDDING_SITE_URL = "https://wedding.tapphouse.co/"


def _context() -> dict[str, Any]:
    return {
        "review_instance": get_settings().review_instance,
        "couple": COUPLE,
        "wedding_day": WEDDING_DAY,
        "wedding_place": WEDDING_PLACE,
        "wedding_site_url": WEDDING_SITE_URL,
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


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def front_door(request: Request) -> Response:
    """The card on a save-the-date hostname, the welcome everywhere else.

    Answering **200** rather than 404 is deliberate. This is a real page at a real
    address, and it says so to a cache, a monitor and a screen reader alike. The "there
    is nothing here without a token" signal is carried by what the page says, which a
    person can read, rather than by a status code, which they cannot. A bad token still
    answers 404 — the two pages are not merged.
    """
    if serves_the_card(request):
        return templates.TemplateResponse(
            request=request, name="save_the_date.html", context=_context()
        )
    return templates.TemplateResponse(
        request=request, name="public_welcome.html", context=_context()
    )
