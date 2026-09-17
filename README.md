# SaveTheDate

Save-the-date and RSVP service for weddings and events.

Hosts create an event, add their guest list, and hand each guest a private invite
link. Guests open the link, see the event, and RSVP — no account, no password.
Hosts read the responses back off the guest list.

**Status:** early. The API below works end to end and is covered by tests. The
guest-facing pages are built — an invite link opens a real page, not JSON — but the
host-facing endpoints are still unauthenticated (see [Known gaps](#known-gaps)), so
only invented guests may exist on any running instance.

## Stack

- Python 3.12, [FastAPI](https://fastapi.tiangolo.com/)
- PostgreSQL 17 via SQLAlchemy 2.0 + Alembic
- Pydantic v2 for request/response schemas
- pytest, ruff, mypy (strict)

### Front end — built

Server-rendered **Jinja2 templates + htmx 2.x + Tailwind**, served by FastAPI itself.
No separate JavaScript application, and no Node toolchain: Tailwind is used via its
standalone binary.

```bash
# One-time: install the Tailwind v4 standalone CLI (no Node, no package.json)
curl -sSL -o ~/.local/bin/tailwindcss \
  https://github.com/tailwindlabs/tailwindcss/releases/download/v4.3.3/tailwindcss-linux-x64
chmod +x ~/.local/bin/tailwindcss

# Rebuild the stylesheet after editing app/static/src/app.css
~/.local/bin/tailwindcss -i app/static/src/app.css -o app/static/app.css
```

The built `app/static/app.css` is **committed on purpose.** Render does not run your
build step — a new `import` once broke a sibling project in production while CI stayed
green — so nothing about a deploy is allowed to depend on the binary being present.

htmx is vendored at `app/static/vendor/htmx-2.0.10.min.js` rather than loaded from a
CDN, so the version is whatever is in the repo and a test asserts it.

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
| `events` | The event itself. `event_date` is nullable — a save-the-date can go out before the date is fixed. `timezone` is an IANA name; `rsvp_opens_at` and `rsvp_deadline` bracket the window the form is live. |
| `segments` | One row per thing on the schedule — the welcome party, the ceremony, the optional golf round. Headcounts are per segment, because a Friday number is not a Sunday number. Optional items carry `price` (nullable while unconfirmed) and `booking_url`. |
| `guests` | One row per invitation, not per person. `party_size` is the number of seats the invitation covers ("Alex + guest" is one row with `party_size = 2`). `invite_token` is a 32-byte URL-safe secret. |
| `attendees` | One row per real person under an invitation, with their own `dietary_tags` and `dietary_notes`. This is what lets one person attend while their plus-one declines. |
| `attendance` | Whether one person is coming to one segment. The row carries an explicit boolean rather than meaning "yes" by existing, so "said no to golf" stays distinct from "never answered about golf". |
| `rsvps` | At most one per guest, and deliberately thin: `note` and `responded_at`. Its *existence* is what keeps "declined" distinct from "never replied". Re-submitting the same invite link replaces the answer, so guests can change their mind. |

There is no per-plate meal choice — dietary tags only, and headcounts are per day
rather than per main.

`attendees.attending` ("coming to anything at all") is kept alongside the per-segment
rows for query convenience, and a deferred constraint trigger keeps the two in step
rather than trusting the application to.

### The constraint that drives the design

Once invites are sent, `guests.invite_token` is in people's inboxes. That row can never
be re-keyed without breaking links already in the wild. So `events` and `guests` stay
stable and all change is absorbed by the tables hanging off them. Adding the four-day
schedule was therefore a data migration rather than a re-keying — no token changed
value, and a test asserts exactly that.

### Three RSVP phases

Save-the-dates go out 6–12 months ahead and invitations 6–8 weeks ahead, so the form is
not live the whole time. `GET /invites/{token}` reports which phase the event is in:

| Phase | When | What the guest gets |
| --- | --- | --- |
| `before_open` | before `rsvp_opens_at` | the save-the-date, no form |
| `open` | between the two | the form |
| `closed` | on or after `rsvp_deadline` | their existing answer, read-only |

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness probe |
| `POST` | `/events` | Create an event (409 if the slug is taken) |
| `GET` | `/events/{slug}` | Fetch an event by slug |
| `POST` | `/events/{event_id}/guests` | Add an invitation to the guest list |
| `GET` | `/events/{event_id}/guests` | List the guest list, with invite tokens |
| `GET` | `/api/invites/{token}` | What a guest sees, as JSON: event, their name, their current RSVP |
| `PUT` | `/api/invites/{token}/rsvp` | Submit or change an RSVP |

An RSVP is rejected with `422` if it names more people than the invitation covers, if a
person is marked attending but is coming to nothing, or if a person is marked not
attending while coming to something. It is rejected with `403` outside the RSVP window.

### Guest pages

`/invites/{token}` serves HTML, because that URL is the one that goes in somebody's
inbox. The JSON view of the same data moved to `/api/invites/{token}`. **The token is
unchanged** — only what the URL renders is different, which is the point: a `guests`
row is never re-keyed.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/invites/{token}` | Welcome — who is getting married, where, and why there |
| `GET` | `/invites/{token}/wedding` | The schedule, travel and lodging |
| `GET` | `/invites/{token}/rsvp` | Save-the-date, the form, or the read-only answer, by phase |
| `POST` | `/invites/{token}/rsvp` | The form's own submission — works with htmx absent |
| `GET` | `/invites/{token}/print` | A plain page for the fridge: no photos, no nav |

Every one of these is anonymous and carries `noindex, nofollow`. There is no guest
login and there must never be one: the link *is* the credential.

### Seeding a review instance

```bash
.venv/bin/python -m scripts.seed_review_data                    # RSVP window open
.venv/bin/python -m scripts.seed_review_data --phase real       # the true Oct 2027 dates
.venv/bin/python -m scripts.seed_review_data --phase before-open
.venv/bin/python -m scripts.seed_review_data --phase closed
```

Creates the event, the six schedule segments and a handful of **invented** guests,
printing an invite link for each. `--phase` decides which of the three RSVP states the
event is in: it defaults to `open`, because on the real dates the form stays shut until
October 2027 and a reviewer could not complete anything. `--base-url` prefixes the
printed links, for when the instance is behind a tunnel.

Guest data stays fictional until host authentication lands (TAP-7725) — until then
anyone who can reach the API can read the whole list.

**Re-seeding issues new invite tokens.** Any link already shared stops working. That is
fine for invented guests and is exactly what must never happen once the real list
exists.

### Showing it to reviewers — the public review instance

```bash
scripts/review-instance.sh up       # start, seed, and print the links
scripts/review-instance.sh status   # running? on what URL?
scripts/review-instance.sh down     # stop everything
```

Runs the app on this box and publishes it through a **Cloudflare Quick Tunnel** —
outbound-only, so no open ports, no static IP and no DNS change, which is why this does
not wait on the `nltlabs.ai` zone move. Needs `cloudflared`:

```bash
curl -sSL -o ~/.local/bin/cloudflared \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x ~/.local/bin/cloudflared
```

Three things to know before sending anyone a link:

- **The URL is random and does not survive a restart.** Every `up` prints a new one, and
  this box reboots roughly daily. Re-send the links after any restart.
- **Every page says `Draft preview`**, driven by `REVIEW_INSTANCE=true`, so nobody
  mistakes it for the invitation that was really sent. It defaults off.
- **The host endpoints are still unauthenticated.** Anyone with the URL can read every
  invite token and create junk events. That is survivable only because every guest is
  invented; `noindex` headers and a `robots.txt` deny keep the URLs out of search
  indexes, but they are not access control. TAP-7725 is the actual fix.

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
- **No host dashboard.** The guest pages exist; the host-facing side does not.
- **No email/SMS delivery.** Invite links have to be distributed by hand.
- **No rate limiting** on invite-token lookups.

## License

MIT — see [LICENSE](LICENSE).
