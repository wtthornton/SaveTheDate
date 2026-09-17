Picking up SaveTheDate — the wedding site for Lisa Gorden & Bill Thornton, Port
Aransas, Texas, Sunday 13 February 2028.

Read these first, in order: `IMPLEMENTATION_PLAN.md` — §12 (what this actually is), §14
(the production stack, the most recent session), §8.1 (what TAP-7733 set out to build
and what is ticked off), §13 and §7.1 (the two public pages, and why the domain is what
it is). Then `LESSONS_LEARNED.md` §8 and §7, which are mostly about the difference
between a check passing and the thing actually working. Then `docs/RUNBOOK.md`, which is
the operational truth. Then `.claude/CLAUDE.md` for the always-on invariants.

## Where things stand, 2026-09-17

13 of 17 Linear issues are Done, one is canceled. Phases 0–3 are complete, and Phase 4
is most of the way: schema, guest pages, the review instance, the RSVP window, host
auth, ownership scoping, rate limiting, the host dashboard, CSV import, email delivery,
the two public pages, **and now the production stack**.

268 tests. Gate green: ruff, ruff format, mypy --strict over 51 files, migrations
up→down→up against the test database. Zero `noqa`, zero `type: ignore`, zero skipped
tests, zero swallowed exceptions. Keep it that way.

**Four hostnames are live, on two stacks with separate databases.**

| Hostname | Stack | Serves |
| --- | --- | --- |
| `wedding.tapphouse.co` | `savethedate-prod` on 127.0.0.1:8100 | Production wedding site |
| `savethedate.tapphouse.co` | same process | Production save-the-date card |
| `dev-wedding.tapphouse.co` | review instance on :50681 | Development, invented guests |
| `dev-savethedate.tapphouse.co` | same process | The card, for review |
| `home.tapphouse.co` | — | Home Assistant. **Not ours.** A CNAME straight to Nabu Casa; it does not touch this tunnel. |

**The production database is empty on purpose.** It was seeded with invented guests to
prove the restore drill, then truncated and verified table by table. Both public pages
render fine on it, because they hard-code the couple, the date and the place.

## The one thing blocking TAP-7733

**The backups are not leaving this machine.** Everything else about them is built and
proven: `scripts/backup.sh` nightly at 03:30, `scripts/restore-drill.sh` weekly on
Sunday, both as systemd user timers, both run successfully under systemd. The drill
downloads the newest dump *from the remote*, restores it into a scratch database, and
compares row counts against a manifest taken from the live database at dump time. Five
deliberate failures were confirmed caught.

But `BACKUP_REMOTE` in `.env.backup` still points at a **local directory**, which is on
the same disk as the database and is therefore not a backup.

**Bill has to create the R2 bucket and token** — `.env.backup.example` has the exact
steps, including scoping the token to the one bucket. Once he does:

1. Put the four values into `.env.backup` (gitignored; `chmod 600`).
2. `./scripts/backup.sh` — it confirms the uploaded byte count, so a silent no-op fails.
3. `./scripts/restore-drill.sh` — this is the deliverable. It must print
   `GUESTS RESTORED` and exit 0.
4. Then TAP-7733 is done except for the UPS.

Do not load the real guest list before step 3 passes against R2. Losing the guest list
is the one failure with no recovery path, and the date cannot move.

## After that, in rough order

1. **Register the one real host account.** `docs/RUNBOOK.md` has the procedure. It needs
   `HOST_REGISTRATION_TOKEN` set in `.env.prod`, one registration, then the line deleted
   and a redeploy. Leaving it set means anyone who finds `/auth/register` on a public
   hostname can create events on the wedding's database.
2. **Load the real guest list**, via CSV import (TAP-7732). Only after the R2 drill
   passes.
3. **TAP-7734 observability.** Needs a Sentry DSN or a self-hosted collector. `/health`
   says the process is up, not that the service works. Right now a failed backup or
   drill is visible only to `scripts/prod.sh status`, which reports timer state and
   failed units — that is pull-based, and nobody pulls.
4. **TAP-7762 photography.** Needs somebody with a camera, not a keyboard. The
   invitation hero is still a stock photograph of another couple; the card's background
   is a CC0 Gulf beach rather than Port Aransas. `photo_credit_for()` builds each page's
   credit from the filenames the template names, so swapping a CC BY image in makes a
   credit line appear by itself.
5. **A UPS**, and what happens if the line drops while the household is in Texas for
   part of a months-long RSVP window. Hardware.
6. **A dependency lockfile.** There is none, so a production image rebuilt in 2028 may
   resolve newer libraries than were tested. The built image is what runs and rebuilding
   is deliberate, so this is a rebuild-time risk, not a running one — but it is real
   across 17 months. `uv lock` is the fix and it touches CI, so it is its own change.

## Traps specific to this box

- **`alembic downgrade base` drops every table in whatever `DATABASE_URL` points at, and
  it defaults to the DEV database.** Round-trip against the test DB only:
  `DATABASE_URL="postgresql+psycopg://savethedate:savethedate@localhost:5434/savethedate_test" .venv/bin/alembic …`
- **Never `docker compose ... down -v` the production stack.** `-v` removes the volume,
  and that volume is the guest list. `scripts/prod.sh down` offers no way to pass it.
- **Always drive production through `scripts/prod.sh`.** It supplies
  `--env-file .env.prod`, and Compose substitutes *nothing* for an unset variable rather
  than failing — so the raw compose file starts Postgres with a blank password.
- **`reload` after any change under `app/*.py`** on the review instance. Jinja re-reads
  templates each request but imports Python once, so a long-lived instance keeps serving
  the old module. That shipped a live 500 once while every test was green.
- **Rebuild and commit `app/static/app.css`** after any template or CSS change.
  `tests/test_stylesheet.py` fails if you forget, because a missing class shipped a
  broken image to a phone.

## What this project actually is — read §12 before designing anything

The data layer is genuinely multi-tenant: hosts, `events.host_id`, four
ownership-scoped queries, nine cross-host tests, another host's event answers 404 not
403.

The presentation layer is one couple's wedding: exactly seven values reach the templates
from the database; everything else — the story, the L & B monogram, the Port Aransas
photo matching — is hard-coded. The two public pages hard-code the couple, date and
place too, because they belong to no event row.

That split is deliberate and correct. **Do not "fix" it by generalising the templates.**
There is no second wedding, and building for a customer who does not exist is how a
four-page site acquires a CMS. If a second event is ever wanted, the honest route is a
second deployment.

## Non-negotiable

- `guests.invite_token` is in people's inboxes once sent. **NEVER re-key a guests row.**
  Withdrawing an invitation deletes the row; that is what kills the link.
- Guest routes stay anonymous. The link IS the credential. No guest login, ever, and no
  "find your invitation" name lookup — on an anonymous page that is a guest-list oracle.
  This applies to the two public pages too.
- htmx 2.x only. htmx 4 changed attribute inheritance and fails silently.
- 18px body text, 44px touch targets, native form controls. When new CSS trips the 18px
  floor, raise the type — do not add an exemption.
- Reference static files with `static_url()`, never a bare `/static/...` path.
  Cloudflare caches them for four hours, so a fixed URL serves a stale stylesheet long
  after a rebuild — and it presents as "the design is broken", not as a cache.
- Anything that animates to `opacity: 0` and stays in the layout needs
  `pointer-events: none`. Invisible is not intangible; it still eats taps.
- **The production app binds `127.0.0.1` only.** That is what makes trusting
  `CF-Connecting-IP` defensible. Binding it wider silently turns the rate limiter off.
- **Production settings live in `docker-compose.prod.yml`, not a `.env`**, so they can be
  reviewed and asserted. `tests/test_production_stack.py` fails if one drifts. Only
  secrets go in `.env.prod` / `.env.backup`.
- No `# noqa`, no `# type: ignore`, no skipped tests, no swallowed exceptions. If the
  right fix is out of scope, stop and say so.
- American English. Postgres is on host port 5434 for dev; production publishes none.
- **This repo is PUBLIC.** Never write a credential into it. Invite tokens are
  credentials — review links live in `.review/INVITE_LINKS.md`, gitignored.
- Never migrate the `nltlabs.ai` zone for this project. It carries live company mail
  behind a quarantine DMARC policy. Plan §7.1 explains the alternative that was used.
- Tailscale on this box depends on IPv6 ULAs. Nothing may disable IPv6 wholesale.
- Do NOT use the Workflow tool unless asked — the token pool is shared with other
  sessions. Plain subagents are fine.

## Habits, all of which earned their place

- **If a check passes on the first run, it has proved nothing.** Break the source on
  purpose and confirm the right test goes red. **And check the mutation applied** — this
  has now bitten three times: a CSS build that failed so the old artifact stayed on
  disk, an unquoted `-k` expression that selected zero tests, and `kill` not existing in
  a slim image so the exec errored while the next line printed "recovered".
- **Watch a counter move, not a request succeed.** Proving the restart policy took three
  attempts; the first two had plausible success messages and a `RestartCount` that had
  not moved.
- **Look at the page.** Every save-the-date screenshot was once taken with half the card
  still inside the envelope, and not one assertion noticed.
- Ask the browser for the condition rather than deriving it. `document.getAnimations()`
  beats naming the element you happen to think of.
- Sampling the centre of an element is not sampling the element.
- A test that passes alone and fails in the suite is shared state, not flakiness.
- Watch what your test client actually sends. Two rounds of 404 tests were worthless
  because `TestClient` defaults to `Accept: */*`; and the RSVP route is `PUT`, not
  `POST`, which a 405 said faster than reading would have.
- **A check that is red for a known and acceptable reason trains people to ignore it.**
  The restore drill warns and passes on an honestly-empty database rather than failing
  weekly until the guest list exists.
- Write what you measured, not what you assume. A claim in a comment gets believed later.
- When taste is the specification, ask for a reference — then go and read the reference.

Linear: project SaveTheDate, team TappsCodingAgents (TAP), issues TAP-7725–7781.
