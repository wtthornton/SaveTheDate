"""Move events owned by a placeholder host onto a real one. TAP-7726.

`events.host_id` is NOT NULL, so the TAP-7726 migration had to give existing events an
owner. On a database that had events but no registered host — which is every
deployment at the moment this landed — it invented one whose password hash cannot be
parsed as argon2, so nobody can sign in as it. The events survive and are unreachable
rather than being deleted or handed to whoever registers first.

This hands them to a real account:

    .venv/bin/python -m scripts.adopt_events --to you@example.com
    .venv/bin/python -m scripts.adopt_events --to you@example.com --dry-run

It touches `events.host_id` and nothing else. In particular it never looks at `guests`,
so no invite token is re-keyed.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Event, Host

# Kept in step with the same names in the TAP-7726 migration and the seed script.
PLACEHOLDER_EMAILS = ("unassigned@invalid.localhost", "review@invalid.localhost")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", required=True, help="email of the host to adopt the events")
    parser.add_argument(
        "--dry-run", action="store_true", help="say what would move, and change nothing"
    )
    args = parser.parse_args(argv)

    with Session(engine) as session:
        new_owner = session.scalar(select(Host).where(Host.email == args.to.strip().casefold()))
        if new_owner is None:
            print(f"no host registered as {args.to!r}", file=sys.stderr)
            return 1

        placeholders = list(session.scalars(select(Host).where(Host.email.in_(PLACEHOLDER_EMAILS))))
        if not placeholders:
            print("no placeholder host found; nothing to adopt")
            return 0

        orphan_ids = [host.id for host in placeholders]
        events = list(session.scalars(select(Event).where(Event.host_id.in_(orphan_ids))))
        if not events:
            print("the placeholder host owns no events; nothing to adopt")
            return 0

        for event in events:
            print(f"{'would move' if args.dry_run else 'moving'} {event.slug!r} -> {args.to}")
            if not args.dry_run:
                event.host_id = new_owner.id

        if args.dry_run:
            print(f"{len(events)} event(s) would move. Nothing was changed.")
            return 0

        session.commit()
        print(f"{len(events)} event(s) moved to {args.to}.")
        print("The placeholder host is left in place; delete it by hand once you are sure.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
