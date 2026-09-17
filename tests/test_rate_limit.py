"""TAP-7727 — `/invites/*` is public, so it gets a throttle.

Tokens are 32 random bytes, so brute force was never the threat. An unthrottled public
endpoint is a free scraping and amplification surface, and this one does database work
on every hit.

The limiter runs **before** the token is looked up. That is the whole of the "do not
distinguish valid from invalid" requirement: once an address is over its limit, a real
token and a made-up one get byte-identical 429s having done no query at all, so neither
the status nor the time taken says whether the token was real.

In-process counters, not Redis. The guest ceiling is under 100 people and the app runs
as a single instance; a second service to count to sixty would be absurd.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.factories import add_guest, create_event


@pytest.fixture
def tight_limit(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Three requests per minute, so a test does not have to make sixty."""
    from app.config import get_settings
    from app.ratelimit import reset_limiter

    monkeypatch.setenv("INVITE_RATE_LIMIT", "3")
    monkeypatch.setenv("INVITE_RATE_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    reset_limiter()
    yield 3
    get_settings.cache_clear()
    reset_limiter()


@pytest.fixture(autouse=True)
def _clean_limiter() -> Iterator[None]:
    """Counters are process-global, so they have to be dropped between tests."""
    from app.config import get_settings
    from app.ratelimit import reset_limiter

    get_settings.cache_clear()
    reset_limiter()
    yield
    get_settings.cache_clear()
    reset_limiter()


def _a_token(client: TestClient, db_session: Session) -> str:
    event = create_event(client)
    guest = add_guest(client, event["id"], "Jordan Lee", party_size=1)
    return str(guest["invite_token"])


# -- The throttle itself --------------------------------------------------


def test_going_over_the_limit_returns_429(
    client: TestClient, anonymous_client: TestClient, db_session: Session, tight_limit: int
) -> None:
    token = _a_token(client, db_session)

    codes = [anonymous_client.get(f"/invites/{token}").status_code for _ in range(tight_limit + 1)]

    assert codes[:tight_limit] == [200] * tight_limit
    assert codes[-1] == 429


def test_the_429_says_when_to_come_back(
    client: TestClient, anonymous_client: TestClient, db_session: Session, tight_limit: int
) -> None:
    token = _a_token(client, db_session)
    for _ in range(tight_limit):
        anonymous_client.get(f"/invites/{token}")

    throttled = anonymous_client.get(f"/invites/{token}")

    assert throttled.status_code == 429
    assert "retry-after" in {key.lower() for key in throttled.headers}
    assert int(throttled.headers["retry-after"]) >= 1


def test_a_throttled_real_token_is_indistinguishable_from_a_made_up_one(
    client: TestClient, anonymous_client: TestClient, db_session: Session, tight_limit: int
) -> None:
    """The limiter runs before the lookup, so neither answer reveals which token exists."""
    token = _a_token(client, db_session)
    for _ in range(tight_limit):
        anonymous_client.get(f"/invites/{token}")

    real = anonymous_client.get(f"/invites/{token}")
    invented = anonymous_client.get("/invites/this-token-does-not-exist-at-all")

    assert real.status_code == invented.status_code == 429
    assert real.text == invented.text


def test_the_json_view_is_throttled_too(
    client: TestClient, anonymous_client: TestClient, db_session: Session, tight_limit: int
) -> None:
    """`/api/invites/{token}` is the same data behind a different renderer."""
    token = _a_token(client, db_session)

    codes = [
        anonymous_client.get(f"/api/invites/{token}").status_code for _ in range(tight_limit + 1)
    ]

    assert codes[-1] == 429


# -- What it must not throttle -------------------------------------------


def test_the_stylesheet_is_not_throttled(anonymous_client: TestClient, tight_limit: int) -> None:
    """A guest page pulls CSS, htmx and four photographs. Counting those would throttle
    a single visitor on their first page."""
    codes = [anonymous_client.get("/static/app.css").status_code for _ in range(tight_limit + 3)]

    assert codes == [200] * len(codes)


def test_host_routes_are_not_throttled(client: TestClient, tight_limit: int) -> None:
    """They are already behind a session; throttling them would only hurt the host."""
    event = create_event(client)

    codes = [
        client.get(f"/events/{event['id']}/guests").status_code for _ in range(tight_limit + 3)
    ]

    assert codes == [200] * len(codes)


def test_health_is_not_throttled(anonymous_client: TestClient, tight_limit: int) -> None:
    """A platform health check hits this every few seconds for as long as it runs."""
    codes = [anonymous_client.get("/health").status_code for _ in range(tight_limit + 3)]

    assert codes == [200] * len(codes)


# -- The counter itself, away from HTTP -----------------------------------


def test_each_address_gets_its_own_allowance() -> None:
    from app.ratelimit import SlidingWindowLimiter

    limiter = SlidingWindowLimiter(limit=2, window_seconds=60, clock=_stopped_clock())

    assert limiter.retry_after("10.0.0.1") is None
    assert limiter.retry_after("10.0.0.1") is None
    assert limiter.retry_after("10.0.0.1") is not None
    # A different guest on a different connection is unaffected.
    assert limiter.retry_after("10.0.0.2") is None


def test_the_allowance_comes_back_when_the_window_passes() -> None:
    from app.ratelimit import SlidingWindowLimiter

    clock = _stopped_clock()
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60, clock=clock)

    limiter.retry_after("10.0.0.1")
    limiter.retry_after("10.0.0.1")
    assert limiter.retry_after("10.0.0.1") is not None

    clock.advance(61)
    assert limiter.retry_after("10.0.0.1") is None


def test_retry_after_counts_down_as_the_window_slides() -> None:
    from app.ratelimit import SlidingWindowLimiter

    clock = _stopped_clock()
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60, clock=clock)

    limiter.retry_after("10.0.0.1")
    first = limiter.retry_after("10.0.0.1")
    clock.advance(30)
    later = limiter.retry_after("10.0.0.1")

    assert first is not None and later is not None
    assert later < first


def test_hammering_a_closed_door_does_not_keep_it_shut_for_longer() -> None:
    """A blocked call must not count as a hit.

    If it did, one impatient guest reloading a throttled page would keep pushing their
    own unlock further away — a few taps on a phone turns a one-minute wait into an
    arbitrarily long lockout. Written after a mutation that made blocked calls extend
    the window passed the whole file.
    """
    from app.ratelimit import SlidingWindowLimiter

    clock = _stopped_clock()
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60, clock=clock)

    limiter.retry_after("10.0.0.1")  # the one allowed hit, at t+0

    clock.advance(30)
    for _ in range(5):
        assert limiter.retry_after("10.0.0.1") is not None

    # 61s after the *allowed* hit, the window has passed and the allowance is back.
    clock.advance(31)
    assert limiter.retry_after("10.0.0.1") is None


def test_the_wait_does_not_grow_while_being_hammered() -> None:
    from app.ratelimit import SlidingWindowLimiter

    clock = _stopped_clock()
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60, clock=clock)

    limiter.retry_after("10.0.0.1")
    waits = [limiter.retry_after("10.0.0.1") for _ in range(4)]

    assert all(wait is not None for wait in waits)
    assert len(set(waits)) == 1, f"the wait moved while standing still: {waits}"


def test_idle_addresses_are_forgotten_rather_than_accumulating() -> None:
    """Otherwise the dict is an unbounded memory leak keyed by anything that connects."""
    from app.ratelimit import SlidingWindowLimiter

    clock = _stopped_clock()
    limiter = SlidingWindowLimiter(limit=5, window_seconds=60, clock=clock)

    for octet in range(50):
        limiter.retry_after(f"10.0.0.{octet}")
    assert limiter.tracked_addresses() == 50

    clock.advance(61)
    limiter.retry_after("10.0.1.1")

    assert limiter.tracked_addresses() == 1


class _StoppedClock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _stopped_clock() -> _StoppedClock:
    return _StoppedClock()
