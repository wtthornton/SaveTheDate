# SaveTheDate

Save-the-date and RSVP service for weddings and events.

Hosts create an event, add their guest list, and hand each guest a private invite
link. Guests open the link, see the event, and RSVP — no account, no password.
Hosts read the responses back off the guest list.

**Status:** early. The API below works end to end and is covered by tests. It is
an API only — there is no web client yet, and the host-facing endpoints are not
yet authenticated (see [Known gaps](#known-gaps)).

## Stack

- Python 3.12, [FastAPI](https://fastapi.tiangolo.com/)
- PostgreSQL 17 via SQLAlchemy 2.0 + Alembic
- Pydantic v2 for request/response schemas
- pytest, ruff, mypy (strict)

### Front end — decided, not yet built

Server-rendered **Jinja2 templates + htmx + Tailwind**, served by FastAPI itself.
No separate JavaScript application, and no Node toolchain: Tailwind is used via its
standalone binary.

Why, in short:

- Invite URLs are bearer-token secrets and must be `noindex`, so the SEO advantage
  of Astro or Next.js does not apply here.
- Nothing in the product needs real-time or collaborative client state. The heaviest
  interaction is an RSVP form with a seat counter.
- One language and one quality gate (ruff + mypy + pytest) beats maintaining a second
  dependency ecosystem, CORS, and a second deploy target for a project this size.

**Pin htmx 2.x, not 4.x.** htmx 4 made attribute inheritance explicit, and `latest`
deliberately stays on 2.x until early 2027. Under htmx 4 an un-inherited `hx-headers`
silently fails to reach the child request — a failure mode that is easy to miss when
templates are generated or edited by tooling.

**Revisit if** the host dashboard grows something like a drag-and-drop seating chart.
That is the case where a React front end (Next.js, with a typed client generated from
`/openapi.json`) earns its overhead.

## Quick start

```bash
# 1. Start Postgres
docker compose up -d db

# 2. Install
uv venv --python 3.12
uv pip install -e ".[dev]"

# 3. Configure
cp .env.example .env

# 4. Create the schema
.venv/bin/alembic upgrade head

# 5. Run
.venv/bin/uvicorn app.main:app --reload
```

Interactive docs are then at <http://localhost:8000/docs>.

> Postgres is published on host port **5434**, not 5432, to avoid colliding with
> other local projects. `DATABASE_URL` in `.env.example` already reflects this.

## Data model

| Table | What it holds |
| --- | --- |
| `events` | The event itself. `event_date` is nullable — a save-the-date can go out before the date is fixed. |
| `guests` | One row per invitation, not per person. `party_size` is the number of seats the invitation covers ("Alex + guest" is one row with `party_size = 2`). `invite_token` is a 32-byte URL-safe secret. |
| `rsvps` | At most one per guest. Re-submitting the same invite link updates it in place, so guests can change their mind. |

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness probe |
| `POST` | `/events` | Create an event (409 if the slug is taken) |
| `GET` | `/events/{slug}` | Fetch an event by slug |
| `POST` | `/events/{event_id}/guests` | Add an invitation to the guest list |
| `GET` | `/events/{event_id}/guests` | List the guest list, with invite tokens |
| `GET` | `/invites/{token}` | What a guest sees: event, their name, their current RSVP |
| `PUT` | `/invites/{token}/rsvp` | Submit or change an RSVP |

An RSVP is rejected with `422` if it claims more seats than the invitation covers,
or if it says "attending" while claiming zero seats.

## Tests

The suite runs against a real Postgres database, because the schema uses
Postgres-native UUID columns.

```bash
docker compose exec db createdb -U savethedate savethedate_test   # once
.venv/bin/pytest
```

Point the suite at a different database with `TEST_DATABASE_URL`.

## Known gaps

These are tracked as epics in the
[Linear project](https://linear.app/tappscodingagents/project/savethedate-6d14ff49f534)
and are deliberately not stubbed out:

- **Host endpoints are unauthenticated.** Anyone who can reach the API can create
  events and read any guest list, including invite tokens. This must be closed
  before the service is exposed publicly.
- **No web client.** The invite page and host dashboard do not exist yet.
- **No email/SMS delivery.** Invite links have to be distributed by hand.
- **No rate limiting** on invite-token lookups.

## License

MIT — see [LICENSE](LICENSE).
