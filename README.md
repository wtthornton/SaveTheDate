# SaveTheDate

Save-the-date and RSVP service for weddings and events.

Hosts create an event, add their guest list, and hand each guest a private invite
link. Guests open the link, see the event, and RSVP — no account, no password.
Hosts read the responses back off the guest list.

It also serves **two pages that need no token**: a public save-the-date card, and a
welcome at the root of the wedding site for anyone who arrives without their link.

**Status:** live. Guest pages, host authentication, per-host scoping, rate
limiting, a host dashboard with per-day headcounts, CSV import and export, email
delivery with per-guest delivery state, and the two public pages — running in
production on `wedding.tapphouse.co` and `savethedate.tapphouse.co` since 2026-09-17,
on their own database. 273 tests against a real Postgres.

What is left is mostly not code: an off-machine home for the backups, error reporting,
and real photographs. Until the backups actually leave this machine, keep invented guests
only — losing the guest list has no recovery path.

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

The built `app/static/app.css` is **committed on purpose**, so a deploy never has to run
the build and the 110MB standalone binary never has to exist on the server. The cost of
that choice is that the committed file can fall behind the templates in silence — it did
once, and an image shipped with no height cap — so `tests/test_stylesheet.py` fails if a
class a template names has no rule.

htmx is vendored at `app/static/vendor/htmx-2.0.10.min.js` rather than loaded from a
CDN, so the version is whatever is in the repo and a test asserts it.

**Reference static files through `static_url()`, never by a bare path.** Cloudflare
returns `/static/*` with `max-age=14400` and caches it at its edge, so under a fixed URL
a rebuilt stylesheet keeps being served to returning browsers for four hours. That is
not hypothetical: a reviewer spent a while looking at a page with none of its new rules
— no card, no animation — while the server served the correct file throughout, and
nothing on the page could have said so. `static_url()` appends a content hash, so
changed bytes live at a changed address; the HTML itself is uncached, so the new address
is seen immediately. A test fails on any template that hard-codes `/static/app.css`.

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
durability and for backups that can be restored, not for throughput.

Deliberately ruled out at this size: Redis, a task queue or worker process, pagination,
and streaming CSV import. Staying at one service also keeps the whole deployment to a
single `docker compose up`, which is what makes self-hosting it reasonable.

## Hosting — the home lab

**Everything runs on the home lab.** One Docker Compose stack — FastAPI behind a
reverse proxy, with Postgres alongside it — published to the internet through a
**named Cloudflare Tunnel**. Outbound-only, so no ports are forwarded, no static IP is
needed, the home IP never appears in public DNS, and TLS terminates at Cloudflare's
edge.

No managed platform, no per-month compute bill. The software is entirely open source;
what the lab actually costs is electricity, hardware and offsite backup storage.

### The one thing that is not self-hosted

**Outbound email.** Transactional mail cannot be self-hosted reliably — residential
address space sits on blocklists, and SPF, DKIM and DMARC do not rescue deliverability
from one. `app/mail.py` therefore keeps a provider behind a small port. A hundred
invitations sits inside a free tier, and the default transport prints to the console, so
nothing is sent until that is deliberately configured.

### Hostnames

The domain is **`tapphouse.co`**, delegated to Cloudflare on 2026-09-17 and served by
a named tunnel from the home lab.

| Hostname | Serves | State |
| --- | --- | --- |
| `wedding.tapphouse.co` | Production — the wedding site | **Live** since 2026-09-17 |
| `dev-wedding.tapphouse.co` | The review instance | **Live** |
| `savethedate.tapphouse.co` | Production — the save-the-date card | **Live** since 2026-09-17 |
| `dev-savethedate.tapphouse.co` | The save-the-date card on the review instance | **Live** |
| `home.tapphouse.co` | Home Assistant (Nabu Casa) | Pre-existing, untouched |

Two public faces, one codebase, one app per stack. **Both live at `/`**, and the
hostname decides which one you get: the card on a hostname listed in
`SAVE_THE_DATE_HOSTS`, the wedding welcome on every other. The list is explicit rather
than a substring test — this project is itself called savethedate, and a guest-facing
hostname quietly serving the wrong page is the failure that list exists to prevent.

There is deliberately **no `/save-the-date` path**. The card is reviewed locally at
`savethedate.localhost`, which every browser resolves to loopback, so it needs no DNS
entry and is still reachable under one name only.

The two production hostnames reach the `savethedate-prod` Compose stack on
`127.0.0.1:8100`, which has **its own database**, separate from development. While that
stack is down they return 502 rather than falling through to the review instance — a
guest-facing hostname quietly serving the development database is how invented guests
start looking real, and how a genuine RSVP lands somewhere disposable.

### The two pages that need no token — built 2026-09-17

Everything else this app serves a guest hangs off `guests.invite_token`. These two do
not, which makes them the only pages where "what does this say to a stranger?" is a
question with consequences.

**The welcome**, at the root of the wedding hostname. It names the couple, the date and
Port Aransas, says the invitation is a personal link sent by email, and tells a guest
how to get theirs resent. It answers **200**, not 404: it is a real page at a real
address, and the "nothing here without a token" signal is carried by what it says, which
a person can read, rather than by a status code, which they cannot. It does **not** show
the schedule, the address, or anything else behind a token.

**The save-the-date card**, at the root of the save-the-date hostname. A sealed portrait
envelope whose face is **two doors**, hinged on their outer edges and held shut by one
wax seal. The wax gives, the doors swing open and turn their teal liner toward the
reader, and the whole shell drops out of frame, leaving the card — which carries the
couple, the date, Port Aransas, and a link onward to the wedding site, over a
full-bleed photograph.

The paper and the wax are **generated, not photographed**: three SVG filters build fibre,
mottle and a pressed wax lozenge. A photograph could not do it, because these doors turn
in three dimensions and a picture of an envelope cannot fold. It costs nothing over the
wire and stays sharp at any size.

The animation is **pure CSS**. Nothing to run, so there is no state in which a reader
gets a blank rectangle because a script failed or had not arrived — on the one page
whose whole job is to say a date out loud. `prefers-reduced-motion: reduce` removes the
drift and the reveal together and leaves the card already open; that is a vestibular
accessibility requirement, not a preference, and the guest list skews old.

Neither page reads a guest row, and neither takes input. The couple, the date and the
place are hard-coded rather than read from `events`: these pages belong to no event row,
choosing one without a token or a signed-in host would mean inventing a "primary event"
— a content model on the exact axis plan §12 says not to build one — and hard-coding
means they render on an empty production database, which is the state it will be in on
its first day.

Both are `noindex` like every other page. Public here means "needs no token", not "wants
to be searchable": a couple's names and a wedding date are not something to hand a
crawler.

Two things neither may ever become:

- **A "find your invitation" lookup form.** Zola and Minted both do this and TAP-7725
  rejects the pattern on friction grounds — but it is also a guest-list oracle, letting
  anyone test names to learn who was invited. Asserted by a test, on both pages.
- **A guest login.** The link is the credential. This is the invariant the whole schema
  hangs off.

The existing "We could not find that invitation" page stays for *bad tokens* and unknown
paths, where it is accurate. It is the wrong thing to show someone who merely typed the
domain: they never submitted an invitation, so telling them one could not be found reads
as their mistake.

**Why a separate domain and not `invite.nltlabs.ai`.** A named Cloudflare Tunnel needs
its zone on Cloudflare nameservers — the partial (CNAME) setup that would let a zone stay
at GoDaddy is Business-plan only, around $200/month. So `invite.nltlabs.ai` would have
meant migrating the `nltlabs.ai` zone, which carries a live Microsoft 365 deployment
behind a `quarantine` DMARC policy, where a mistake is silent and unrecoverable.
`tapphouse.co` carries no mail at all, so delegating it risked nothing. `nltlabs.ai` was
never touched.

**One record on `tapphouse.co` was not disposable.** `home.tapphouse.co` points at Home
Assistant Cloud (Nabu Casa) and was carried across unproxied — a proxied record would put
Cloudflare's certificate in front of a service that issues its own. It resolves correctly
through the new nameservers.

**The tunnel runs as a systemd user service**, `cloudflared-tapphouse`, with
`Restart=always` and lingering enabled, so it returns by itself after a reboot or a power
cut without anyone logging in. Config lives in `~/.cloudflared/config.yml`.

The Quick Tunnel from `scripts/review-instance.sh` still works and still mints a random
URL, but `dev-wedding.tapphouse.co` is the stable address for review — the invite token
is hostname-independent, so every published link works on either.

### What the home lab has to provide

Losing the guest list is the one failure here with no recovery path, and a wedding date
cannot move. Self-hosting means these are yours to get right rather than somebody
else's:

- ~~**Automated Postgres backups, off this machine**, and a **restore that has actually
  been performed**.~~ Built 2026-09-17: `scripts/backup.sh` nightly and
  `scripts/restore-drill.sh` weekly, both on systemd user timers, with the drill
  restoring the newest dump into a scratch database and comparing row counts against a
  manifest taken at dump time. **One leg outstanding** — the destination is Cloudflare
  R2 and the bucket is not created yet, so until `.env.backup` holds real credentials
  the dumps have not actually left the machine.
- ~~**Unattended restart**: the stack comes back on its own after a power cut.~~ Done:
  `restart: unless-stopped` throughout, Docker, the tunnel and both timers enabled at
  boot, and lingering on so the user units survive logout.
- ~~**Power continuity** through the RSVP window — a UPS.~~ Done: the box is on UPS
  hardware. What remains is the *network* half, which a battery does not answer — what
  happens if the house loses internet while you are in Texas. The tunnel reconnects by
  itself, so the open question is long outages, and whether anyone would notice.

Tracked as TAP-7733. The operational procedures are in [docs/RUNBOOK.md](docs/RUNBOOK.md),
written to be followable by someone who has never seen this project.

### Running production

```bash
cp .env.prod.example .env.prod     # fill in POSTGRES_PASSWORD; it is gitignored
./scripts/prod.sh deploy           # build, migrate, start, wait for /health
./scripts/prod.sh status           # containers, health, and backup timer state
```

Always go through `scripts/prod.sh`. It supplies `--env-file .env.prod`, and Compose
substitutes *nothing* for an unset variable rather than failing — so running the compose
file directly starts Postgres with a blank password instead of erroring.

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
| `hosts` | The only accounts in the system. Guests deliberately have no row here and never will — the invite token is their whole credential. argon2id password digests. |
| `host_sessions` | One logged-in browser. Server-side rather than a signed cookie, so signing out actually ends the session and a stolen cookie can be revoked. Stores the sha256 of the cookie value, never the value. |
| `deliveries` | One attempt to email one guest, per kind. A silently bounced invite looks identical to a guest who ignored it, so the outcome is recorded rather than assumed, and shown on the dashboard. |

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

### The review instance opens the guest site at its root

`https://dev-wedding.tapphouse.co/` redirects to one guest's invitation, so the four
token-gated pages can be reviewed without first finding a 43-character token in a
gitignored file. It picks the largest party, so the RSVP page being looked at is the one
with the most in it, and resolves per request, so re-seeding cannot strand anyone.

**This cannot happen in production.** It is gated on `REVIEW_INSTANCE`, which is `False`
by default and `"false"` in `docker-compose.prod.yml`;
`test_production_stack.py` fails if that drifts, and
`test_production_never_opens_the_guest_site_at_the_root` asserts the root still welcomes
**with guests in the database** — the state where a leak would actually matter.

It is not a lookup. There is no name box and no way to ask for a particular guest: it is
one fixed row chosen by the query, not by the caller. An anonymous page must never
answer questions about who was invited, and a constant answers no question.

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
scripts/review-instance.sh reload   # restart after a Python change, keeping the URL
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
  this box reboots roughly daily. Re-send the links after any restart. Use `reload`
  rather than `up` after changing anything under `app/*.py`: Jinja re-reads templates on
  every request but imports Python once, so a running instance otherwise keeps serving
  the old module. `reload` restarts on the same port, so the URL and every invite token
  already sent stay valid.
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
playwright install chromium                                      # once
.venv/bin/pytest
```

Point the suite at a different database with `TEST_DATABASE_URL`.

**`tests/test_visual.py` drives a real browser.** It loads every page at 390px and
1440px, measures rendered geometry against the design artboards, and writes full-page
screenshots to `tests/screenshots/` (gitignored — regenerated each run).

It exists because of a specific failure: the welcome page shipped with an empty grey box
where the design has a photographic hero, and all 45 tests at the time stayed green. They
checked structure, phases, labels and type sizes — nothing that knows what a page looks
like. Later the same gap hid a whole missing breakpoint.

Two things it does that a stylesheet scan cannot:

- **Measures computed styles**, so the cascade and Tailwind utilities count. That caught
  three links rendering at 29px against a declared 44px floor.
- **Measures x-height, not font-size.** 20px Cormorant Garamond light italic has a
  smaller x-height than 18px Karla, so the hero date was rendering *smaller* than body
  text while passing an 18px floor. All-caps runs are measured by cap-height instead,
  where x-height describes nothing.

**Look at the screenshots.** An assertion only catches what somebody thought to assert.

### Photography

Ten photographs in `app/static/img/`, all openly-licensed placeholders, all listed with
their licenses in [`app/static/img/CREDITS.md`](app/static/img/CREDITS.md). Two are of
Port Aransas itself. Five are CC BY and carry a credit line rendered at the foot of every
guest page from `PHOTO_CREDITS` in `app/templating.py` — **if a CC BY photograph is
removed, remove its name too.** Which picture goes with which scheduled item is decided
by `segment_image()`, with a fallback so no card can render empty.

## Known gaps

These are tracked as epics in the
[Linear project](https://linear.app/tappscodingagents/project/savethedate-6d14ff49f534)
and are deliberately not stubbed out:

- **The backups have not left this machine yet.** The pipeline is built and its restore
  path is proven end to end, but the destination is a Cloudflare R2 bucket that does not
  exist yet, so `BACKUP_REMOTE` still points at a local directory — which is on the same
  disk as the database, and therefore not a backup. `.env.backup.example` has the steps.
  **This is the only remaining gap that could cost the guest list**, and it is the last
  thing standing between TAP-7733 and done.
- **No dependency lockfile.** CI installs with `uv pip install -e ".[dev]"` and the
  production image resolves at build time, so an image rebuilt in 2028 may pull newer
  libraries than were tested here. The built image is what runs and rebuilding is
  deliberate, so this is a rebuild-time risk rather than a running one — but it is real
  across a 17-month deployment.
- **No answer for a long internet outage.** The box is on a UPS and everything returns
  unattended after a power cut, so power is covered. A battery does nothing for the line
  going down while the household is in Texas, though — the tunnel reconnects by itself,
  so what is actually unsolved is a long outage, and whether anyone would know.
- **No error reporting**, and `/health` says the process is up rather than that the
  service works. TAP-7734.
- **Every photograph is a placeholder**, and the hero is a stock photograph of another
  couple. TAP-7762.
- **The mail webhook's signature scheme is unverified** against a live provider — see
  the docstring in `app/mail.py`. Sending itself is tested end to end against a fake
  transport.

## License

MIT — see [LICENSE](LICENSE).
