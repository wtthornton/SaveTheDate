# Prompt for the next session

Copy everything between the rules into a fresh Claude Code session started in
`/home/wtthornton/code/SaveTheDate`. Update the "Where things stand" block as work
lands, or it will start lying.

---

Picking up SaveTheDate — the wedding site for Lisa Gorden & Bill Thornton,
Port Aransas, Texas, Sunday 13 February 2028.

Read these first, in order: **IMPLEMENTATION_PLAN.md** — especially §12 (what this
actually is), §11 (how it got hosted), §8.1 (what TAP-7733 still has to build) and §7.1
(why the domain is what it is). Then **LESSONS_LEARNED.md** §6, which is mostly about
how to write a test that is worth anything here. Then **.claude/CLAUDE.md** for the
always-on invariants.

## Where things stand, 2026-09-17

**12 of 16 Linear issues are Done.** Phases 0–3 are complete: schema, guest pages, the
review instance, the RSVP window, host auth, ownership scoping, rate limiting, the host
dashboard, CSV import, email delivery.

**206 tests.** Gate green: ruff, ruff format, `mypy --strict` over 48 files, migrations
up→down→up against the test database. Zero `noqa`, zero `type: ignore`, zero skipped
tests, zero swallowed exceptions in the repository. Keep it that way.

**Git is clean**: one branch (`main`), one worktree, in sync with origin, no stashes.

**It is live.** `https://dev-wedding.tapphouse.co/invites/<token>` serves the guest pages
from this box through a named Cloudflare Tunnel.

## What this project actually is — read §12 before designing anything

The **data layer is genuinely multi-tenant**: `hosts`, `events.host_id`, four
ownership-scoped queries, nine cross-host tests, another host's event answers 404 not
403.

The **presentation layer is one couple's wedding**: exactly seven values reach the
templates from the database; everything else — the story, the `L & B` monogram, the Port
Aransas photo matching — is hard-coded.

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
| `wedding.tapphouse.co` | Production — **503 on purpose** |
| `savethedate.tapphouse.co` | Reserved — 503, contents undecided |
| `home.tapphouse.co` | Home Assistant. **Not ours. Do not touch.** |

**Do not point `wedding.tapphouse.co` at the development instance to make it look
finished.** A guest-facing hostname quietly serving the development database is how
invented guests start looking real and how a genuine RSVP lands somewhere disposable.

## What is left — four issues, and what each needs

- **TAP-7733** home lab hosting — *In Progress.* Domain and tunnel are done. What
  remains: the **production Compose stack** behind `wedding.tapphouse.co` with its own
  database, migrations as a release step, and **backups off the machine with a restore
  actually performed**. The restore is the deliverable, not the backup.
- **TAP-7775** the root of the guest site should welcome, not error — *new, designed,
  ready to build.* Decided with Bill; the issue carries the full brief and the two
  things it must never become.
- **TAP-7734** observability — needs a Sentry DSN or a self-hosted collector.
- **TAP-7762** photography — needs somebody to take photographs. Not a coding task. The
  hero is still a stock photograph of another couple.

## Open questions only Bill can answer

- **What is `savethedate.tapphouse.co` for?** Routed and reserved, serving 503. Given
  §12, a plausible reading is a *second deployment* — its own event row, its own guest
  list — rather than another page of this one. Ask; do not assume.
- **Is the card on file for `tapphouse.co` current?** It expires **2027-08-01**, roughly
  six months before the wedding and inside the RSVP window. `renewAuto` is on, but a
  lapsed card takes the guest site's domain with it.

## Non-negotiable

- `guests.invite_token` is in people's inboxes once sent. **NEVER re-key a guests row.**
  Withdrawing an invitation deletes the row; that is what kills the link.
- **Guest routes stay anonymous. The link IS the credential.** No guest login, ever, and
  no "find your invitation" name lookup — that is a guest-list oracle.
- htmx 2.x only. htmx 4 changed attribute inheritance and fails silently.
- 18px body text, 44px touch targets, native form controls. **When new CSS trips the
  18px floor, raise the type — do not add an exemption.** The "eyebrow" loophole in
  LESSONS_LEARNED is exactly that mistake.
- **Rebuild `app/static/app.css` after any template or CSS change, and commit it.**
  `tests/test_stylesheet.py` fails if you forget — it exists because a missing class
  shipped a broken image to a phone.
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

## Four habits, all of which earned their place today

- **If tests pass on the first run, they have proved nothing.** Break the source on
  purpose and confirm the right test goes red. Roughly sixty mutations this session found
  four real gaps, including a shared fixture that made three security tests vacuous and a
  rule asserted only in a docstring.
- **A test that passes alone and fails in the suite is shared state, not flakiness.**
- **Watch what your test client actually sends.** Two rounds of 404 tests were worthless
  because `TestClient` defaults to `Accept: */*`; content negotiation alone routed
  everything to JSON and the logic under test was never exercised.
- **Look at the page.** Three real dashboard problems were invisible to 158 passing
  tests. Screenshots go to `tests/screenshots/`.

Linear: project SaveTheDate, team TappsCodingAgents (TAP), issues TAP-7725–7775.

---

## If you want a shorter version

Read IMPLEMENTATION_PLAN.md §12 and §8.1, LESSONS_LEARNED.md §6, and .claude/CLAUDE.md.
Phases 0–3 are done, 12 of 16 issues closed, 206 tests, gate green, git clean, and the
site is live at `dev-wedding.tapphouse.co`. Next is TAP-7733's production stack and
backups, then TAP-7775's welcome page. Everything self-hosts on the home lab — there is
no managed platform in this project. Ask Bill what `savethedate.tapphouse.co` is for
before building anything behind it.
