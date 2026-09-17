# Prompt for the next session

Copy everything between the rules into a fresh Claude Code session started in
`/home/wtthornton/code/SaveTheDate`. Update the "Where things stand" block as work
lands, or it will start lying.

---

Picking up SaveTheDate — the wedding site for Lisa Gorden & Bill Thornton,
Port Aransas, Texas, Sunday 13 February 2028.

Read these first, in order: **IMPLEMENTATION_PLAN.md** — §12 (what this actually is),
§8.1 and §11 (how it got hosted and what TAP-7733 still has to build), §13 (the most
recent session) and §7.1 (why the domain is what it is). Then **LESSONS_LEARNED.md** §6
and §7, which are mostly about the difference between a test passing and the thing
actually working. Then **.claude/CLAUDE.md** for the always-on invariants.

## Where things stand, 2026-09-17

**13 of 17 Linear issues are Done**, one is canceled. Phases 0–3 are complete: schema,
guest pages, the review instance, the RSVP window, host auth, ownership scoping, rate
limiting, the host dashboard, CSV import, email delivery, and the two public pages.

**254 tests.** Gate green: ruff, ruff format, `mypy --strict` over 50 files, migrations
up→down→up against the test database. GitHub Actions green on `main`. Zero `noqa`, zero
`type: ignore`, zero skipped tests, zero swallowed exceptions in the repository. Keep it
that way.

**Git is clean**: one branch (`main`), one worktree, in sync with origin, no stashes.

**Two things are live**, both from this box through the named Cloudflare Tunnel:
`https://dev-wedding.tapphouse.co` — the wedding site, with `/invites/<token>` for
guests and a welcome at its root — and `https://dev-savethedate.tapphouse.co`, the
public save-the-date card. They are **one process**, told apart by the Host header.

**Nothing is in production.** Both production hostnames return 503 on purpose. That is
the whole of the next piece of work.

## The next piece of work — TAP-7733, in order

The production stack, and then the backup restore that is the actual deliverable. Ask
Bill before the first step; everything after it is in-scope work you can just do.

**1. The production Compose stack.** A second Compose project on this box — app plus
Postgres, its own volume, its own database, `restart: unless-stopped` throughout so it
returns after a power cut with nobody logging in. It shares a machine with the dev
stack, so isolation matters more than usual: **a different database, a different port,
a different volume name.** Dev Postgres is on host port 5434; do not reuse it.

**2. Migrations as a release step, not on app boot.** Two instances racing
`alembic upgrade` at start is a bad way to find out about locking. A one-shot service or
an explicit command in the deploy, run before the app comes up.

**3. The environment, where the easy-to-miss items are.** In the production `.env`, which
is never committed:

- `TRUSTED_CLIENT_IP_HEADER=CF-Connecting-IP` — behind the tunnel every request arrives
  from the tunnel's local end, so without this the whole world shares one rate-limit
  bucket and the first few guests throttle everyone else.
- `SAVE_THE_DATE_HOSTS=savethedate.tapphouse.co` — or that hostname serves the wedding
  welcome instead of the card.
- `PUBLIC_BASE_URL=https://wedding.tapphouse.co` — `SESSION_COOKIE_SECURE` follows it.
- `REVIEW_INSTANCE` unset or false, or production wears the draft banner.
- `EMAIL_PROVIDER` stays `console` until mail is deliberately configured. An
  unconfigured deployment that prints is obvious; one that silently succeeds is a lie.
- `HOST_REGISTRATION_TOKEN` set once to create the single real host, then unset again.

**4. Point the tunnel at it.** Add `wedding.tapphouse.co` and `savethedate.tapphouse.co`
to the `ingress` block in `~/.cloudflared/config.yml`, above the catch-all, then restart
`cloudflared-tapphouse`. Back the config up first and run
`cloudflared tunnel ingress validate` before restarting.

**5. Backups, and the restore.** `pg_dump` on a timer — the database is a few megabytes.
**Off this machine**; a backup on the same disk as the database is not a backup. Then
**restore it into a scratch database and count the guests.** A backup nobody has
restored is a hypothesis, and TAP-7733 is not done until one has been restored and
verified. Keep enough history to survive a mistake found late: a bad migration noticed a
week later is the realistic case, not a disk dying.

**6. Verify like a guest, not like a deployer.** Two devices do not share a rate-limit
bucket. An invite token works end to end on production. Both hostnames serve their own
page. The stack comes back on its own after `docker compose down` and after a reboot.

**7. Then the things self-hosting put on the critical path.** A UPS and unattended
restart. What happens if the line drops while the household is in Texas for part of a
months-long RSVP window. And a runbook someone other than Bill can follow — if the one
person who understands the stack is at his own wedding, that has to be enough.

### Two traps specific to this work

- **`alembic downgrade base` drops every table in whatever `DATABASE_URL` points at, and
  it defaults to the DEV database.** Run the up→down→up round trip against the test
  database: `DATABASE_URL="$TEST_DATABASE_URL" .venv/bin/alembic …`
- **Put no real guest data anywhere until the restore has been proven.** Losing the guest
  list is the one failure here with no recovery path, and the date cannot move.

## After that — two issues, neither urgent

- **TAP-7734** observability — needs a Sentry DSN or a self-hosted collector. `/health`
  currently says the process is up, not that the service works.
- **TAP-7762** photography — needs somebody with a camera, not a keyboard. The invitation
  hero is still a stock photograph of another couple; the save-the-date's background is a
  CC0 Gulf beach rather than Port Aransas. Note that `photo_credit_for()` builds each
  page's credit from the filenames the template names, so swapping a CC BY image in makes
  a credit line appear by itself.

## What this project actually is — read §12 before designing anything

The **data layer is genuinely multi-tenant**: `hosts`, `events.host_id`, four
ownership-scoped queries, nine cross-host tests, another host's event answers 404 not
403.

The **presentation layer is one couple's wedding**: exactly seven values reach the
templates from the database; everything else — the story, the `L & B` monogram, the Port
Aransas photo matching — is hard-coded. The two public pages hard-code the couple, date
and place too, because they belong to no event row.

That split is deliberate and correct. **Do not "fix" it by generalising the templates.**
There is no second wedding, and building for a customer who does not exist is how a
four-page site acquires a CMS. If a second event is ever wanted, the honest route is a
second deployment, not a content model.

## Hosting — home lab, no managed platform

`tapphouse.co` is on Cloudflare. Tunnel `tapphouse` runs as the systemd **user** service
`cloudflared-tapphouse` (`Restart=always`, lingering on), config at
`~/.cloudflared/config.yml`.

| Hostname | Serves |
| --- | --- |
| `dev-wedding.tapphouse.co` | The review instance on :50681 — live |
| `dev-savethedate.tapphouse.co` | The save-the-date card, same instance — live |
| `wedding.tapphouse.co` | Production — **503 until the stack exists** |
| `savethedate.tapphouse.co` | Production save-the-date — **503 until the stack exists** |
| `home.tapphouse.co` | Home Assistant. **Not ours. Do not touch.** |

The two dev hostnames reach **one** process and are told apart by the Host header
(`SAVE_THE_DATE_HOSTS`). There is no `/save-the-date` path on either — the card is the
root of its own hostname, and locally that is `savethedate.localhost:<port>`, which every
browser resolves to loopback.

**`home.tapphouse.co` does not go through the tunnel** — it is a CNAME straight to Nabu
Casa. Leave it alone because it is not ours, not because cloudflared can break it: an
ingress change costs a few seconds of `dev-wedding` and nothing else. Note that **SIGHUP
does not reload cloudflared in place** — it exits, and `Restart=always` brings it back
with a new PID in about three seconds. There is no `ExecReload` on the unit.

**Do not point `wedding.tapphouse.co` at the development instance to make it look
finished.** A guest-facing hostname quietly serving the development database is how
invented guests start looking real and how a genuine RSVP lands somewhere disposable.

## How to run and look at it

- Postgres: `docker compose up -d db` (host port **5434**)
- Gate: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && \
  .venv/bin/mypy app migrations scripts tests && .venv/bin/pytest -q`
- Review instance: `scripts/review-instance.sh up | reload | status | down`.
  **`reload` after any change under `app/*.py`** — Jinja re-reads templates each request
  but imports Python once, so a long-lived instance keeps serving the old module. That
  shipped a live 500 once while every test was green. `reload` keeps the port, so the URL
  and every invite token survive; `up` does not.
- CSS: `~/.local/bin/tailwindcss -i app/static/src/app.css -o app/static/app.css`, and
  **commit the result**.
- Screenshots land in `tests/screenshots/`. Look at them.

## Open questions only Bill can answer

- **Is the card on file for `tapphouse.co` current?** It expires **2027-08-01**, roughly
  six months before the wedding and inside the RSVP window. `renewAuto` is on, but a
  lapsed card takes the guest site's domain with it.
- **Where should backups go?** Off this machine is the requirement; which object store,
  and whose account pays for it, is not a decision to make for him.

## Non-negotiable

- `guests.invite_token` is in people's inboxes once sent. **NEVER re-key a guests row.**
  Withdrawing an invitation deletes the row; that is what kills the link.
- **Guest routes stay anonymous. The link IS the credential.** No guest login, ever, and
  no "find your invitation" name lookup — on an anonymous page that is a guest-list
  oracle. This applies to the two public pages too.
- htmx 2.x only. htmx 4 changed attribute inheritance and fails silently.
- 18px body text, 44px touch targets, native form controls. **When new CSS trips the
  18px floor, raise the type — do not add an exemption.** The "eyebrow" loophole in
  LESSONS_LEARNED is exactly that mistake.
- **Rebuild `app/static/app.css` after any template or CSS change, and commit it.**
  `tests/test_stylesheet.py` fails if you forget — it exists because a missing class
  shipped a broken image to a phone.
- **Reference static files with `static_url()`, never a bare `/static/...` path.**
  Cloudflare caches them for four hours, so a fixed URL serves a stale stylesheet long
  after a rebuild — and it presents as "the design is broken", not as a cache.
- **Anything that animates to `opacity: 0` and stays in the layout needs
  `pointer-events: none`.** Invisible is not intangible; it still eats taps.
- No `# noqa`, no `# type: ignore`, no skipped tests, no swallowed exceptions. If the
  right fix is out of scope, stop and say so.
- American English. Postgres is on host port **5434**.
- **This repo is PUBLIC.** Never write a credential into it. Invite tokens are
  credentials — the review links live in `.review/INVITE_LINKS.md`, gitignored.
- **Never migrate the `nltlabs.ai` zone for this project.** It carries live company mail
  behind a `quarantine` DMARC policy. Plan §7.1 explains the alternative that was used.
- **Tailscale on this box depends on IPv6 ULAs.** Nothing may disable IPv6 wholesale.

Do NOT use the Workflow tool unless asked — the token pool is shared with other sessions.
Plain subagents are fine.

## Habits, all of which earned their place

- **If tests pass on the first run, they have proved nothing.** Break the source on
  purpose and confirm the right test goes red. And **check the mutation applied** — two
  mutations once reported "not caught" while never changing the file at all, one because
  it broke the CSS build so the old artifact stayed on disk, one because an unquoted
  `-k` expression selected zero tests.
- **Look at the page.** Every save-the-date screenshot was taken with half the card
  still inside the envelope, and not one assertion noticed, because the wait watched the
  flap and the flap finishes 900ms before the pocket. Only opening the file caught it.
- **Ask the browser for the condition rather than deriving it.** `document.getAnimations()`
  beats naming the element you happen to think of.
- **Sampling the centre of an element is not sampling the element.** An overlay lying
  across the bottom edge of a link passes a centre-point hit test.
- **A test that passes alone and fails in the suite is shared state, not flakiness.**
- **Watch what your test client actually sends.** Two rounds of 404 tests were worthless
  because `TestClient` defaults to `Accept: */*`; content negotiation alone routed
  everything to JSON and the logic under test was never exercised.
- **Write what you measured, not what you assume.** A CSS comment once claimed an
  overlay "swallows every tap" when it covered part of one link. A claim in a comment
  gets believed later.
- **When taste is the specification, ask for a reference** — then go and read the
  reference rather than guessing at what is on the page.

Linear: project SaveTheDate, team TappsCodingAgents (TAP), issues TAP-7725–7781.

---

## If you want a shorter version

Read IMPLEMENTATION_PLAN.md §12 and §8.1, LESSONS_LEARNED.md §7, and .claude/CLAUDE.md.
Phases 0–3 are done, 13 of 17 issues closed, 254 tests, gate green, git clean, and both
public faces are live — `dev-wedding.tapphouse.co` and `dev-savethedate.tapphouse.co`,
one process, told apart by the Host header. **Nothing is in production**; both production
hostnames return 503. Next is TAP-7733: a production Compose stack with its own database,
migrations as a release step, then backups off the machine with a restore actually
performed and verified by counting guests. Everything self-hosts on the home lab — there
is no managed platform in this project.
