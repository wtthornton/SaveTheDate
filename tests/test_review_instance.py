"""TAP-7738 — what has to be true before this is reachable from the internet.

A Quick Tunnel puts the whole app on a public URL. The host endpoints are still
unauthenticated (TAP-7725), which is accepted *only* because every guest on a review
instance is invented. What is not acceptable is an invite URL reaching a search index,
or a reviewer mistaking a draft for the real invitation.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import get_settings
from tests.factories import add_guest, add_segments, create_event
from tests.html_assertions import Document

GUEST_PATHS = ("", "/wedding", "/rsvp", "/print")


@pytest.fixture
def _as_review_instance(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run the next request as the review deployment rather than production."""
    monkeypatch.setenv("REVIEW_INSTANCE", "true")
    get_settings.cache_clear()
    yield
    monkeypatch.delenv("REVIEW_INSTANCE", raising=False)
    get_settings.cache_clear()


def _guest(client: TestClient, db_session: Session) -> str:
    event = create_event(
        client,
        rsvp_opens_at=datetime.now(UTC) - timedelta(days=1),
        rsvp_deadline=datetime.now(UTC) + timedelta(days=30),
    )
    add_segments(db_session, event["id"])
    return str(add_guest(client, event["id"], "Jordan Lee")["invite_token"])


# -- Keeping bearer-token URLs out of search indexes ----------------------


def test_robots_txt_denies_every_crawler(client: TestClient) -> None:
    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "User-agent: *" in body
    assert "Disallow: /" in body


def test_every_response_carries_the_noindex_header(client: TestClient, db_session: Session) -> None:
    """The meta tag only helps on HTML. The header covers the JSON API too.

    An invite token in a URL is a credential; a crawler that reaches one has published
    somebody's RSVP link.
    """
    token = _guest(client, db_session)

    checked = [f"/invites/{token}{path}" for path in GUEST_PATHS]
    checked += [f"/api/invites/{token}", "/health", "/robots.txt"]

    for path in checked:
        header = client.get(path).headers.get("x-robots-tag", "")
        assert "noindex" in header, f"{path} has no X-Robots-Tag: {header!r}"
        assert "nofollow" in header, f"{path} does not forbid following: {header!r}"


def test_an_unknown_token_is_also_noindexed(client: TestClient) -> None:
    assert "noindex" in client.get("/invites/nope").headers.get("x-robots-tag", "")


# -- Visibly a draft ------------------------------------------------------


def test_a_review_instance_says_so_on_every_guest_page(
    client: TestClient, db_session: Session, _as_review_instance: None
) -> None:
    """A reviewer must never mistake this for the invitation that was really sent."""
    token = _guest(client, db_session)

    for path in GUEST_PATHS:
        page = Document(client.get(f"/invites/{token}{path}").text)
        assert "draft" in page.text.lower(), f"/invites/{{token}}{path} does not say it is a draft"


def test_the_real_deployment_shows_no_draft_notice(client: TestClient, db_session: Session) -> None:
    """The banner is a deployment setting, not decoration — it must default to off."""
    token = _guest(client, db_session)

    page = Document(client.get(f"/invites/{token}").text)

    assert "draft" not in page.text.lower()


# -- A reviewer has to be able to finish an RSVP --------------------------


def test_the_seed_can_place_the_event_in_any_phase() -> None:
    """TAP-7738 is only done when a reviewer completes a fake RSVP.

    The real dates leave the form shut until October 2027, so the review seed has to be
    able to open it — and to show the other two phases on demand, since both are part
    of what there is to review.
    """
    from app.models import Event
    from app.rsvp import phase
    from scripts.seed_review_data import rsvp_window

    def _at(opens_at: datetime | None, deadline: datetime | None) -> Event:
        # A real model instance, unsaved — `phase()` only reads these two columns, and
        # a stand-in class would stop proving they are the columns it reads.
        return Event(
            slug="phase-check",
            title="T",
            host_name="H",
            timezone="America/Chicago",
            rsvp_opens_at=opens_at,
            rsvp_deadline=deadline,
        )

    for wanted in ("before-open", "open", "closed"):
        opens_at, deadline = rsvp_window(wanted)
        assert phase(_at(opens_at, deadline)) == wanted.replace("-", "_")

    # "real" is what production will actually run: shut until October 2027.
    opens_at, deadline = rsvp_window("real")
    assert opens_at is not None and opens_at.year == 2027 and opens_at.month == 10
    assert deadline is not None and deadline.year == 2027 and deadline.month == 12


def test_an_unknown_seed_phase_is_refused() -> None:
    from scripts.seed_review_data import rsvp_window

    with pytest.raises(ValueError, match="unknown"):
        rsvp_window("whenever")
