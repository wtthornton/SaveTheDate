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

## Scale ceiling — under 100 guests

Sized for a single wedding: under 100 invitations, perhaps 180 seats, a burst of about
100 page opens when invites land, and a few thousand requests over the project's life.

Nothing here is a performance problem. The consequence is that the engineering budget
goes to **availability and not losing the guest list** — a wedding has an immovable
date, and losing 100 people's responses has no recovery path. Postgres is kept for
managed backups, not for throughput.

Deliberately ruled out at this size: Redis, a task queue or worker process, pagination,
and streaming CSV import. Staying at one service is also the main lever on hosting cost.

## Hosting

**Now:** self-hosted on the dev box and published with
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/)
— outbound-only, so no open ports, no static IP, and TLS at the edge. This is for design
review on real phones, with **fake guest data only**, because the host endpoints are
still unauthenticated.

**Later:** a managed host (Railway or Render, paid tier). Render's *free* tier is
disqualified for this project on two counts: free Postgres is deleted 30 days after
creation, and free web services cold-start for 30–60 seconds — fatal for a link a guest
opens exactly once.

### Hostnames

| Use | Hostname |
| --- | --- |
| Guest-facing | `invite.nltlabs.ai` |
| Review instance | `invite-review.nltlabs.ai` |

`invite` because the site lives through two phases months apart: `rsvp` is wrong while
it is still a save-the-date, and `savethedate` is wrong once the invitation and RSVP go
out. An *invitation* is the whole artifact and both are phases of it.

Keep prefixes to a single label. Cloudflare's free Universal SSL wildcard covers
`*.nltlabs.ai` but not `a.b.nltlabs.ai`, so use hyphens rather than a second dot.

`nltlabs.ai` is on GoDaddy nameservers today, so neither hostname routes to a tunnel
until the zone moves to Cloudflare (TAP-7740) — that change touches the live company
site, so it is tracked separately.

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

### Redesign pending (TAP-7739)

Design review found this model cannot carry a real wedding, and the changes land
before the invite page is built:

- **Per-person `attendees`.** `rsvps.party_size = 2` says two people are coming but
  never who, and cannot hold two meal choices or two allergies. Dietary needs are
  collected per person, and caterers need per-plate counts. Attendance moves down to
  the individual, so one person can attend while their plus-one declines.
- **`meal_options`** per event, as a table rather than free text.
- **`rsvp_opens_at`** on the event. Save-the-dates go out 6–12 months ahead and
  invitations 6–8 weeks ahead; without an open date the form is live from day one.

**The constraint that drives the design:** once invites are sent,
`guests.invite_token` is in people's inboxes. That row can never be re-keyed without
breaking links already in the wild. So `events` and `guests` stay stable and all future
change is absorbed by the tables hanging off them — which is also what makes adding
multiple sub-events later a data migration rather than a schema one.

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
