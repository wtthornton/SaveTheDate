"""The current instant, as an injectable dependency.

`rsvp.phase()` used to call `datetime.now(UTC)` itself, which made the instant either
side of an RSVP deadline impossible to assert — the existing tests could only build a
window as `now() ± timedelta` and hope. Routes take the clock as a dependency so a test
can pin it to the boundary exactly. TAP-7729.

There is deliberately no frozen-time library behind this. `freezegun` and `time-machine`
both patch the interpreter's clock globally, which is a large hammer for one comparison,
and neither is currently a dependency of this project.
"""

from datetime import UTC, datetime


def now() -> datetime:
    """The present, always time-zone aware."""
    return datetime.now(UTC)
