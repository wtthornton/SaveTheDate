"""The two pages anybody can reach without a token.

Everything else this app serves to a guest hangs off `guests.invite_token`. These two
do not, which makes them the only pages where "what does it say to a stranger?" is a
question with teeth:

* `/` — the welcome, TAP-7775. Somebody typed the domain, or was given it by a
  relative without the link. They get told how to get their own link, not that their
  invitation could not be found.
* `/save-the-date` — the card, TAP-7781. The public save-the-date, sent broadly long
  before the guest list is final.

The tests below are mostly about what these pages must NOT contain. A leak here is
not a rendering bug, it is the guest list or a private address published on an
unauthenticated URL, so each prohibition is asserted against parsed markup rather
than trusted to review.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.factories import add_guest, add_segments, create_event
from tests.html_assertions import Document

# A real browser sends this. `TestClient` defaults to `Accept: */*`, and the 404
# handler routes on exactly that header — two earlier rounds of 404 tests proved
# nothing because content negotiation sent every one of them to the JSON branch.
BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

SAVE_THE_DATE_HOST = "savethedate.tapphouse.co"
WEDDING_HOST = "wedding.tapphouse.co"

# The house. It is somebody's home address and appears on no unauthenticated page.
PRIVATE_ADDRESS = "Stargrass"


@pytest.fixture
def save_the_date_hosts(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Configure which hostnames serve the card at their root."""
    from app.config import get_settings

    monkeypatch.setenv("SAVE_THE_DATE_HOSTS", f"{SAVE_THE_DATE_HOST},dev-savethedate.tapphouse.co")
    get_settings.cache_clear()
    yield SAVE_THE_DATE_HOST
    get_settings.cache_clear()


def _host(name: str) -> dict[str, str]:
    return {**BROWSER, "Host": name}


def _fetch(client: TestClient, page: str) -> Any:
    """One of the two public pages, addressed the way the internet addresses it.

    Both live at `/`; the hostname is the whole of the difference. Parametrizing over
    hostnames rather than over paths is not a detail — there IS no second path any
    more, and a test that invented one would be testing something the site does not do.
    """
    hostname = SAVE_THE_DATE_HOST if page == "card" else WEDDING_HOST
    return client.get("/", headers=_host(hostname))


# -- The welcome at the root, TAP-7775 -------------------------------------------


def test_the_root_welcomes_rather_than_reporting_a_missing_invitation(
    anonymous_client: TestClient,
) -> None:
    """Someone who typed the domain never submitted a token, so nothing is missing."""
    response = anonymous_client.get("/", headers=BROWSER)

    assert response.status_code == 200
    assert "We could not find that invitation" not in Document(response.text).text


def test_the_root_names_the_couple_and_the_date(anonymous_client: TestClient) -> None:
    text = Document(anonymous_client.get("/", headers=BROWSER).text).text

    assert "Lisa" in text
    assert "Bill" in text
    assert "February 13, 2028" in text


def test_the_root_says_the_invitation_is_a_personal_link(
    anonymous_client: TestClient,
) -> None:
    """The one thing this page exists to tell somebody who arrived without one."""
    text = Document(anonymous_client.get("/", headers=BROWSER).text).text.casefold()

    assert "link" in text
    assert "email" in text


def test_a_bad_token_still_says_the_invitation_was_not_found(
    anonymous_client: TestClient,
) -> None:
    """The welcome and the 404 are two pages, not one. Merging them loses both jobs."""
    response = anonymous_client.get("/invites/not-a-real-token", headers=BROWSER)

    assert response.status_code == 404
    assert "We could not find that invitation" in Document(response.text).text


def test_an_unknown_path_still_gets_the_written_404(anonymous_client: TestClient) -> None:
    response = anonymous_client.get("/nothing-here", headers=BROWSER)

    assert response.status_code == 404
    assert "We could not find that invitation" in Document(response.text).text


def test_a_missing_api_route_is_still_json(anonymous_client: TestClient) -> None:
    """A caterer's script should not have to parse wedding prose. TAP-7728.

    A path with no route at all, rather than a bad token: this is the branch of the
    404 handler that the welcome route could plausibly have broken.
    """
    response = anonymous_client.get("/api/does-not-exist", headers=BROWSER)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


# -- What neither page may ever contain ------------------------------------------


@pytest.mark.parametrize("page", ["welcome", "card"])
def test_no_public_page_offers_a_way_to_look_an_invitation_up(
    anonymous_client: TestClient, save_the_date_hosts: str, page: str
) -> None:
    """A name box here is a guest-list oracle: anyone could test names to learn who
    was invited. TAP-7725 rejected the pattern on friction grounds; it is also a
    disclosure. No form, no control, no lookup — ever."""
    document = Document(_fetch(anonymous_client, page).text)

    assert document.find_all("form") == []
    assert document.form_controls() == []
    assert document.faked_controls() == []


@pytest.mark.parametrize("page", ["welcome", "card"])
def test_no_public_page_shows_the_private_address(
    anonymous_client: TestClient, save_the_date_hosts: str, page: str
) -> None:
    """183 Stargrass Ln is a private home, not a venue with a parking lot."""
    assert PRIVATE_ADDRESS not in _fetch(anonymous_client, page).text


@pytest.mark.parametrize("page", ["welcome", "card"])
def test_no_public_page_shows_a_guest_or_the_schedule(
    anonymous_client: TestClient,
    client: TestClient,
    db_session: Session,
    save_the_date_hosts: str,
    page: str,
) -> None:
    """Seeded first, so this fails if the page ever learns to read a guest row.

    A test that only checked for a hard-coded string would keep passing on the day
    somebody wires this page to the database.
    """
    event = create_event(client)
    guest = add_guest(client, event["id"], name="Marguerite Vandersloot")
    add_segments(db_session, event["id"])

    body = _fetch(anonymous_client, page).text

    assert guest["name"] not in body
    assert guest["invite_token"] not in body
    assert "Welcome party on the beach" not in body


@pytest.mark.parametrize("page", ["welcome", "card"])
def test_no_public_page_invites_a_crawler(
    anonymous_client: TestClient, save_the_date_hosts: str, page: str
) -> None:
    """Public means "needs no token", not "wants to be searchable"."""
    response = _fetch(anonymous_client, page)
    robots = Document(response.text).find("meta", name="robots")

    assert response.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"
    assert robots is not None
    assert "noindex" in robots.attrs["content"]


# -- The card, and which hostname gets it, TAP-7781 ------------------------------


def test_the_card_has_no_path_of_its_own_on_the_wedding_hostname(
    anonymous_client: TestClient, save_the_date_hosts: str
) -> None:
    """There is no `/save-the-date` anywhere, and least of all on the wedding site.

    An earlier version served the card at that path on every hostname, so that it
    could be reviewed without a DNS entry. That made it reachable at
    `dev-wedding.tapphouse.co/save-the-date`, Bill rejected it on sight, and he was
    right: a page reachable under two names is one that gets linked to by the wrong
    one. Asserted on BOTH hostnames so the path cannot creep back on either.
    """
    for hostname in (WEDDING_HOST, save_the_date_hosts):
        response = anonymous_client.get("/save-the-date", headers=_host(hostname))
        assert response.status_code == 404, hostname


def test_the_card_carries_the_four_things_it_is_for(
    anonymous_client: TestClient, save_the_date_hosts: str
) -> None:
    text = Document(_fetch(anonymous_client, "card").text).text

    assert "Lisa" in text
    assert "Bill" in text
    assert "February 13, 2028" in text
    assert "Port Aransas" in text


def test_the_card_points_onward_to_the_wedding_site(
    anonymous_client: TestClient, save_the_date_hosts: str
) -> None:
    document = Document(_fetch(anonymous_client, "card").text)
    onward = [
        anchor for anchor in document.find_all("a") if WEDDING_HOST in anchor.attrs.get("href", "")
    ]

    assert onward, "the card should link to the wedding site"


def test_the_welcome_names_the_island_not_just_the_state(
    anonymous_client: TestClient,
) -> None:
    """Bill overruled TAP-7775's country/state rule on 2026-09-17.

    The welcome and the card are equally public and equally noindex, so naming the
    town on one while hiding it on the other was incoherent. The private address is
    what stays off both, and that is asserted separately.
    """
    text = Document(anonymous_client.get("/", headers=BROWSER).text).text

    assert "Port Aransas" in text


def test_the_root_of_a_save_the_date_host_serves_the_card(
    anonymous_client: TestClient, save_the_date_hosts: str
) -> None:
    response = anonymous_client.get("/", headers=_host(save_the_date_hosts))

    assert response.status_code == 200
    assert "Save the date" in Document(response.text).text


def test_the_root_of_the_wedding_host_serves_the_welcome(
    anonymous_client: TestClient, save_the_date_hosts: str
) -> None:
    """The dispatch is an explicit allow-list, so the wedding hostname is unaffected."""
    text = Document(anonymous_client.get("/", headers=_host(WEDDING_HOST)).text).text

    assert "personal link" in text.casefold()


def test_a_hostname_that_merely_contains_the_project_name_is_not_a_save_the_date_host(
    anonymous_client: TestClient, save_the_date_hosts: str
) -> None:
    """This project is itself called savethedate, and `dev-wedding` is one careless
    substring match away from serving the wrong page on a guest-facing hostname."""
    text = Document(anonymous_client.get("/", headers=_host("dev-wedding.tapphouse.co")).text).text

    assert "personal link" in text.casefold()


def test_a_port_on_the_host_header_does_not_defeat_the_match(
    anonymous_client: TestClient, save_the_date_hosts: str
) -> None:
    """A tunnel or proxy can pass `host:port`, and a card that silently stopped
    appearing would look like a DNS problem rather than a string-matching one."""
    text = Document(
        anonymous_client.get("/", headers=_host(f"{save_the_date_hosts}:8000")).text
    ).text

    assert "Save the date" in text


def test_the_dispatch_is_off_by_default(anonymous_client: TestClient) -> None:
    """No configuration means the root is the wedding welcome, on every hostname."""
    text = Document(anonymous_client.get("/", headers=_host(SAVE_THE_DATE_HOST)).text).text

    assert "personal link" in text.casefold()


def test_a_save_the_date_host_still_serves_real_invitations(
    anonymous_client: TestClient,
    client: TestClient,
    db_session: Session,
    save_the_date_hosts: str,
) -> None:
    """Only the root is dispatched. An invite link is hostname-independent, and a
    token that worked in somebody's inbox has to keep working whatever they typed."""
    event = create_event(client)
    guest = add_guest(client, event["id"], name="Marguerite Vandersloot")
    add_segments(db_session, event["id"])

    response = anonymous_client.get(
        f"/invites/{guest['invite_token']}", headers=_host(save_the_date_hosts)
    )

    assert response.status_code == 200
    assert guest["name"] in Document(response.text).text


# -- The credit these pages owe, and the one they do not -------------------------


def test_a_page_of_cc0_photographs_credits_nobody() -> None:
    """CC0 obliges nothing, and a credit line for a picture nobody can see is noise."""
    from app.templating import photo_credit_for

    assert photo_credit_for("gulf-sunset.jpg", "gulf-evening.jpg") == ""


def test_a_cc_by_photograph_brings_its_credit_with_it() -> None:
    """The obligation follows the file, so swapping a picture cannot lose the credit."""
    from app.templating import photo_credit_for

    line = photo_credit_for("/static/img/ferry-sunset.jpg")

    assert "BlankBlankBlank" in line
    assert "Mike Dickison" not in line, "credited a photographer whose work is not shown"


@pytest.mark.parametrize("page", ["welcome", "card"])
def test_every_cc_by_photograph_on_a_public_page_is_credited_on_it(
    anonymous_client: TestClient, save_the_date_hosts: str, page: str
) -> None:
    """The rule img/CREDITS.md states, checked against what the page actually renders.

    CC BY requires the credit to be visible to a reader. These two pages show one
    photograph each rather than all ten, so they cannot lean on the site-wide credit
    line the invitation pages use.
    """
    from app.templating import CC_BY_PHOTOGRAPHS

    body = _fetch(anonymous_client, page).text
    missing = [
        f"{filename} by {photographer}"
        for filename, photographer, _ in CC_BY_PHOTOGRAPHS
        if filename in body and photographer not in body
    ]

    assert missing == [], f"the {page} shows a CC BY photograph without its credit: {missing}"
