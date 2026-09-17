"""A per-address throttle for the public guest routes. TAP-7727.

In-process counters, deliberately. The guest ceiling is under a hundred people and the
app runs as a single instance; standing up Redis to count to sixty would cost more than
the thing it protects. If this is ever scaled out, the counter moves to Postgres — the
`SlidingWindowLimiter` interface is small enough that swapping it is a contained change,
and until then a shared store would only add a failure mode.

A sliding window rather than a fixed one: a fixed window lets a caller spend the whole
allowance in the last second of one window and the whole of the next in the first second
of the following, which is twice the intended rate at exactly the moment it matters.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable

from app.config import get_settings


class SlidingWindowLimiter:
    """Counts hits per key over a moving window.

    `clock` is injectable so the window can be tested without sleeping through it;
    it defaults to `time.monotonic`, which cannot go backwards when the system clock
    is adjusted — a wall clock stepping back would hand out free requests.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)

    def retry_after(self, key: str) -> float | None:
        """Record a hit and return None, or return the seconds until one frees up.

        A blocked call does NOT extend the window. Hammering a closed door must not
        keep it shut for longer, or one impatient reload turns into a long lockout.
        """
        now = self._clock()
        cutoff = now - self.window_seconds
        self._forget_idle(cutoff)

        hits = self._hits[key]
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self.limit:
            # The oldest hit is the one whose expiry frees a slot.
            return max(hits[0] - cutoff, 0.0)

        hits.append(now)
        return None

    def tracked_addresses(self) -> int:
        """How many keys are being remembered. Exists so a test can prove it is bounded."""
        return len(self._hits)

    def _forget_idle(self, cutoff: float) -> None:
        """Drop keys with nothing left in the window.

        Without this the dict grows for the life of the process, keyed by whatever
        connects — an unbounded allocation driven by strangers, which is the shape of
        the problem this module exists to prevent.
        """
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]


_limiter: SlidingWindowLimiter | None = None


def get_limiter() -> SlidingWindowLimiter:
    """The process-wide limiter, built from settings on first use."""
    global _limiter
    if _limiter is None:
        settings = get_settings()
        _limiter = SlidingWindowLimiter(
            limit=settings.invite_rate_limit,
            window_seconds=settings.invite_rate_window_seconds,
        )
    return _limiter


def reset_limiter() -> None:
    """Drop the counters. For tests, and for a settings change to take effect."""
    global _limiter
    _limiter = None


def client_address(client_host: str | None, headers: dict[str, str] | None = None) -> str:
    """The address to count against.

    Behind the Cloudflare tunnel, `request.client.host` is the local end of the tunnel,
    so every guest in the world shares one bucket and the first few would throttle the
    rest. `trusted_client_ip_header` names a header to believe instead —
    `CF-Connecting-IP` for a Cloudflare tunnel.

    It is unset by default and must only ever be set to a header the proxy in front of
    this app *overwrites* on every request — CF-Connecting-IP behind Cloudflare. A
    caller can put anything in a header, so trusting one that is merely appended to,
    such as a raw X-Forwarded-For, hands out an unlimited supply of fresh buckets and
    turns the limiter off.
    """
    header = get_settings().trusted_client_ip_header
    if header and headers:
        forwarded = headers.get(header.lower())
        if forwarded:
            # Take the first entry: with an overwriting proxy there is only one.
            return forwarded.split(",")[0].strip()
    return client_host or "unknown"
