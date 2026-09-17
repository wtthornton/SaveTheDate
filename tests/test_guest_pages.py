"""TAP-7728 — the guest-facing pages.

What a guest opening their link actually gets. The invite URL serves HTML; the JSON
API moved under `/api`, because the link in somebody's inbox has to be a page.

The accessibility assertions here parse the rendered markup rather than the template
source. A template can contain a perfectly good `<label>` and still render a page
without one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.factories import add_guest, add_segments, create_event
from tests.html_assertions import Document

# The smallest body text a guest should ever be asked to read, per TAP-7728.
MINIMUM_BODY_PX = 18


def _open_event_with_guest(
    client: TestClient,
    db_session: Session,
    *,
    party_size: int = 2,
    event_date: str | None = "2028-02-13",
) -> tuple[dict[str, str], str]:
    """An event whose RSVP window is open right now, plus one guest's token."""
    event = create_event(
        client,
        event_date=event_date,
        rsvp_opens_at=datetime.now(UTC) - timedelta(days=1),
        rsvp_deadline=datetime.now(UTC) + timedelta(days=30),
    )
    segments = add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "Jordan Lee", party_size=party_size)
    return segments, str(guest["invite_token"])


# -- The link is a page, not a payload ------------------------------------


def test_the_invite_link_serves_html_not_json(client: TestClient, db_session: Session) -> None:
    _, token = _open_event_with_guest(client, db_session)

    response = client.get(f"/invites/{token}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Jordan Lee" in response.text
    assert not response.text.lstrip().startswith("{")


def test_the_json_api_is_still_available_under_api(client: TestClient, db_session: Session) -> None:
    _, token = _open_event_with_guest(client, db_session)

    response = client.get(f"/api/invites/{token}")

    assert response.status_code == 200
    assert response.json()["guest_name"] == "Jordan Lee"


def test_an_unknown_token_gets_a_page_and_never_echoes_the_token(client: TestClient) -> None:
    response = client.get("/invites/not-a-real-token")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    # Reflecting the token back would put it in logs, screenshots and referrers.
    assert "not-a-real-token" not in response.text
    assert "Traceback" not in response.text


def test_guest_pages_are_noindex(client: TestClient, db_session: Session) -> None:
    """An invite URL is a bearer token. It must never reach a search index."""
    _, token = _open_event_with_guest(client, db_session)

    for path in ("", "/wedding", "/rsvp", "/print"):
        page = Document(client.get(f"/invites/{token}{path}").text)
        robots = page.find("meta", name="robots")
        assert robots is not None, f"no robots meta on /invites/{{token}}{path}"
        assert "noindex" in robots.attrs.get("content", "")


# -- The three RSVP phases ------------------------------------------------


def test_before_the_window_opens_there_is_no_form(client: TestClient, db_session: Session) -> None:
    event = create_event(client, rsvp_opens_at=datetime.now(UTC) + timedelta(days=30))
    add_segments(db_session, event["id"])
    guest = add_guest(client, event["id"], "Jordan Lee")

    page = Document(client.get(f"/invites/{guest['invite_token']}/rsvp").text)

    assert page.find("form") is None, "a save-the-date view must not offer a form"
    assert "save the date" in page.text.lower()


def test_while_the_window_is_open_there_is_a_row_for_each_person(
    client: TestClient, db_session: Session
) -> None:
    _, token = _open_event_with_guest(client, db_session, party_size=2)

    page = Document(client.get(f"/invites/{token}/rsvp").text)

    assert page.find("form") is not None
    # One fieldset per person the invitation covers — the whole point of TAP-7739.
    assert len(page.find_all("fieldset")) >= 2
    for index in (0, 1):
        assert page.find("input", name=f"attendee-{index}-name") is not None
        assert page.find_all("input", name=f"attendee-{index}-attending"), (
            f"no attending choice for attendee {index}"
        )


def test_each_person_answers_for_each_segment_separately(
    client: TestClient, db_session: Session
) -> None:
    segments, token = _open_event_with_guest(client, db_session, party_size=2)

    page = Document(client.get(f"/invites/{token}/rsvp").text)

    for index in (0, 1):
        values = {
            control.attrs.get("value")
            for control in page.find_all("input", name=f"attendee-{index}-segments")
        }
        assert values == set(segments.values()), (
            f"attendee {index} cannot answer for every segment separately"
        )


def test_a_large_party_folds_each_person_away(client: TestClient, db_session: Session) -> None:
    """Five people times six segments is a very long scroll for the guest this is for.

    Folding uses `<details>`, a real element the keyboard and a screen reader can both
    reach, so nothing is actually hidden — only rolled up.
    """
    segments, token = _open_event_with_guest(client, db_session, party_size=5)

    page = Document(client.get(f"/invites/{token}/rsvp").text)

    folds = page.find_all("details")
    assert len(folds) >= 4, "a party of five is not folded at all"
    assert any("open" in fold.attrs for fold in folds), "the first person should start open"

    # Folded is not missing: every person still answers for every segment.
    for index in range(5):
        values = {
            control.attrs.get("value")
            for control in page.find_all("input", name=f"attendee-{index}-segments")
        }
        assert values == set(segments.values()), f"attendee {index} lost segments to the fold"


def test_a_small_party_is_never_folded(client: TestClient, db_session: Session) -> None:
    """An invitation for two is short enough to read straight down."""
    _, token = _open_event_with_guest(client, db_session, party_size=2)

    page = Document(client.get(f"/invites/{token}/rsvp").text)

    assert page.find("details") is None, "a party of two does not need folding"


def test_after_the_deadline_the_page_is_read_only(client: TestClient, db_session: Session) -> None:
    from app.models import Event

    _, token = _open_event_with_guest(client, db_session)
    stored = db_session.query(Event).one()
    stored.rsvp_deadline = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()

    page = Document(client.get(f"/invites/{token}/rsvp").text)

    assert page.find("form") is None, "a closed window must not offer a form"
    assert "closed" in page.text.lower() or "passed" in page.text.lower()


# -- Answering, and changing the answer -----------------------------------


def test_a_guest_can_reply_from_the_form(client: TestClient, db_session: Session) -> None:
    segments, token = _open_event_with_guest(client, db_session, party_size=2)

    response = client.post(
        f"/invites/{token}/rsvp",
        data={
            "attendee-count": "2",
            "attendee-0-name": "Jordan Lee",
            "attendee-0-attending": "yes",
            "attendee-0-segments": [segments["welcome"], segments["ceremony"]],
            "attendee-0-diet": ["gluten-free"],
            "attendee-1-name": "Sam Lee",
            "attendee-1-attending": "no",
            "note": "Flying in Friday morning.",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200, response.text

    answer = client.get(f"/api/invites/{token}").json()["rsvp"]
    assert answer["note"] == "Flying in Friday morning."
    by_name = {person["name"]: person for person in answer["attendees"]}
    assert by_name["Jordan Lee"]["attending"] is True
    assert by_name["Jordan Lee"]["dietary_tags"] == ["gluten-free"]
    assert by_name["Sam Lee"]["attending"] is False

    coming_to = {
        row["segment_id"] for row in by_name["Jordan Lee"]["attendance"] if row["attending"]
    }
    assert coming_to == {segments["welcome"], segments["ceremony"]}


def test_an_existing_reply_comes_back_filled_in(client: TestClient, db_session: Session) -> None:
    segments, token = _open_event_with_guest(client, db_session, party_size=2)
    client.post(
        f"/invites/{token}/rsvp",
        data={
            "attendee-count": "2",
            "attendee-0-name": "Jordan Lee",
            "attendee-0-attending": "yes",
            "attendee-0-segments": [segments["welcome"]],
            "attendee-1-name": "Sam Lee",
            "attendee-1-attending": "no",
            "note": "See you there.",
        },
        follow_redirects=True,
    )

    page = Document(client.get(f"/invites/{token}/rsvp").text)

    assert "Sam Lee" in page.text
    name_field = page.find("input", name="attendee-0-name")
    assert name_field is not None and name_field.attrs.get("value") == "Jordan Lee"

    welcome = [
        control
        for control in page.find_all("input", name="attendee-0-segments")
        if control.attrs.get("value") == segments["welcome"]
    ]
    assert welcome and "checked" in welcome[0].attrs, "a previous yes must come back checked"


def test_a_reply_after_the_deadline_is_refused(client: TestClient, db_session: Session) -> None:
    from app.models import Event

    segments, token = _open_event_with_guest(client, db_session)
    stored = db_session.query(Event).one()
    stored.rsvp_deadline = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()

    response = client.post(
        f"/invites/{token}/rsvp",
        data={
            "attendee-count": "1",
            "attendee-0-name": "Jordan Lee",
            "attendee-0-attending": "yes",
            "attendee-0-segments": [segments["ceremony"]],
        },
        follow_redirects=True,
    )

    assert response.status_code == 403
    assert client.get(f"/api/invites/{token}").json()["rsvp"] is None


# -- The details a guest still needs --------------------------------------


def test_an_event_with_no_date_says_the_date_is_to_follow(
    client: TestClient, db_session: Session
) -> None:
    _, token = _open_event_with_guest(client, db_session, event_date=None)

    text = Document(client.get(f"/invites/{token}").text).text.lower()

    assert "to follow" in text
    assert "none" not in text.split(), "a null date must never render as the word None"


def test_the_print_view_is_plain(client: TestClient, db_session: Session) -> None:
    _, token = _open_event_with_guest(client, db_session)

    page = Document(client.get(f"/invites/{token}/print").text)

    assert page.find("img") is None, "the print view carries no photography"
    assert page.find("nav") is None, "the print view carries no navigation"
    assert "Port Aransas" in page.text


def test_every_page_offers_the_print_view(client: TestClient, db_session: Session) -> None:
    _, token = _open_event_with_guest(client, db_session)

    page = Document(client.get(f"/invites/{token}/wedding").text)

    assert any(link.attrs.get("href", "").endswith("/print") for link in page.find_all("a")), (
        "no way to reach the print view"
    )


# -- No blank picture slots -----------------------------------------------


def test_every_scheduled_item_has_a_picture() -> None:
    """A schedule is host-entered text, so the mapping must never come up empty.

    Photographs where one genuinely matches, an engraved plate otherwise, and a
    fallback for anything the hosts invent later.
    """
    from app.templating import segment_image

    for name in (
        "Ceremony and reception",
        "Welcome party on the beach",
        "Golf at Palmilla Beach",
        "Bay fishing",
        "Dinner in town and a bar crawl",
        "Departure breakfast",
        "Something nobody has thought of yet",
    ):
        src, alt = segment_image(name)
        assert src.startswith("/static/img/"), f"{name} got no picture"
        assert len(alt) > 15, f"{name} got a thin alt text: {alt!r}"


def test_the_picture_for_an_item_suits_it() -> None:
    """A fishing photograph beside golf copy is worse than no photograph at all.

    Named explicitly rather than by matching a keyword against the filename: bay
    fishing is illustrated by `pier-sunset.jpg`, a photograph of an actual fishing
    pier, and a filename check called that wrong.
    """
    from app.templating import segment_image

    expected = {
        "Bay fishing": "pier-sunset.jpg",
        "Golf at Palmilla Beach": "golf-course.jpg",
        "Dinner in town and a bar crawl": "dinner-table.jpg",
        "Departure breakfast": "breakfast-coffee.jpg",
        "Welcome party on the beach": "beach-fire.jpg",
        "Ceremony and reception": "gulf-evening.jpg",
    }
    for name, filename in expected.items():
        assert segment_image(name)[0].endswith(filename), (
            f"{name} is illustrated by {segment_image(name)[0]}, expected {filename}"
        )

    # Two items on the same page sharing a photograph reads as a mistake.
    used = [segment_image(name)[0] for name in expected]
    assert len(set(used)) == len(used), f"a photograph is used twice: {used}"


def test_no_page_renders_an_empty_picture_slot(client: TestClient, db_session: Session) -> None:
    """The grey "Photo — ..." rectangles are gone, everywhere.

    They were honest while nothing existed to put there, but a page of them reads as
    unfinished rather than as tasteful restraint.
    """
    _, token = _open_event_with_guest(client, db_session)

    for path in ("", "/wedding", "/rsvp"):
        page = Document(client.get(f"/invites/{token}{path}").text)
        blanks = [
            element
            for element in page.all
            if "photo-slot" in element.attrs.get("class", "") or "Photo —" in element.deep_text
        ]
        assert not blanks, f"/invites/{{token}}{path} still has {len(blanks)} empty slot(s)"


def test_every_picture_on_a_page_is_actually_served(
    client: TestClient, db_session: Session
) -> None:
    """A src that 404s is a blank spot with extra steps."""
    _, token = _open_event_with_guest(client, db_session)

    seen = 0
    for path in ("", "/wedding", "/rsvp"):
        page = Document(client.get(f"/invites/{token}{path}").text)
        for image in page.find_all("img"):
            src = image.attrs.get("src", "")
            if not src.startswith("/static/"):
                continue
            seen += 1
            assert client.get(src).status_code == 200, f"{src} is referenced but not served"
            assert image.attrs.get("alt"), f"{src} has no alt text"
    assert seen >= 4, f"only {seen} pictures found across the guest pages"


# -- Accessibility, as a requirement rather than an aspiration ------------


def test_every_control_a_guest_fills_in_has_a_label(
    client: TestClient, db_session: Session
) -> None:
    _, token = _open_event_with_guest(client, db_session)

    page = Document(client.get(f"/invites/{token}/rsvp").text)

    # Without this the test passes on any page that has no controls at all.
    assert page.form_controls(), "the RSVP page rendered no controls to check"
    orphans = page.unlabelled_controls()
    assert not orphans, f"controls with no label: {orphans}"


def test_no_page_fakes_a_control_with_a_div(client: TestClient, db_session: Session) -> None:
    _, token = _open_event_with_guest(client, db_session)

    for path in ("", "/wedding", "/rsvp", "/print"):
        response = client.get(f"/invites/{token}{path}")
        assert response.status_code == 200, f"/invites/{{token}}{path} did not render"
        page = Document(response.text)
        assert page.find("html") is not None, f"/invites/{{token}}{path} is not a page"
        assert not page.faked_controls(), (
            f"/invites/{{token}}{path} dresses a div or span up as a control"
        )


def test_the_stylesheet_never_asks_a_guest_to_read_below_18px(client: TestClient) -> None:
    """The guest list skews old. 18px is the floor for anything that is read or tapped.

    Small tracked uppercase eyebrows are exempt: they are ornament, not reading text,
    and are declared with the `.eyebrow` class so the exemption is visible here.
    """
    import re

    response = client.get("/static/app.css")
    assert response.status_code == 200, "no stylesheet is being served"
    assert response.headers["content-type"].startswith("text/css")
    css = response.text
    # Without a floor the scan below passes on an empty or 404 body.
    assert css.count("font-size") >= 5, "the stylesheet declares almost no type sizes"

    offenders = []
    for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selector, body = rule.group(1).strip(), rule.group(2)
        if "eyebrow" in selector or "print" in selector:
            continue
        for size in re.finditer(r"font-size:\s*(\d+(?:\.\d+)?)px", body):
            if float(size.group(1)) < MINIMUM_BODY_PX:
                offenders.append(f"{selector} -> {size.group(0)}")

    assert not offenders, "text below 18px outside the eyebrow exemption: " + "; ".join(offenders)


def test_every_tap_target_is_at_least_44px(client: TestClient) -> None:
    import re

    response = client.get("/static/app.css")
    assert response.status_code == 200, "no stylesheet is being served"
    css = response.text

    targets = [
        (rule.group(1).strip(), rule.group(2))
        for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)
        if "btn" in rule.group(1) or "opt" in rule.group(1) or "tap" in rule.group(1)
    ]
    assert targets, "the stylesheet declares no tap targets to check"

    offenders = []
    for selector, body in targets:
        declared = re.findall(r"min-height:\s*(\d+(?:\.\d+)?)px", body)
        for size in declared:
            if float(size) < 44:
                offenders.append(f"{selector} -> min-height: {size}px")

    assert not offenders, "tap targets under 44px: " + "; ".join(offenders)


# -- The htmx pin ---------------------------------------------------------


def test_htmx_is_pinned_to_2x(client: TestClient, db_session: Session) -> None:
    """htmx 4 made attribute inheritance explicit and fails silently when it is wrong.

    The page asserts its own major version, so a swapped file is loud rather than quiet.
    """
    _, token = _open_event_with_guest(client, db_session)
    page = Document(client.get(f"/invites/{token}/rsvp").text)

    sources = [script.attrs["src"] for script in page.find_all("script") if script.attrs.get("src")]
    assert any("htmx-2." in src for src in sources), f"htmx 2.x is not loaded: {sources}"

    served = client.get("/static/vendor/htmx-2.0.10.min.js")
    assert served.status_code == 200
    assert 'version:"2.0.10"' in served.text
