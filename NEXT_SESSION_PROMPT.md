# Prompt for the next session

Copy everything between the rules into a fresh Claude Code session started in
`/home/wtthornton/code/SaveTheDate`. Update the "Where things stand" block as work
lands, or it will start lying.

---

Picking up SaveTheDate — the wedding site for Lisa Gorden & Bill Thornton,
Port Aransas, Texas, Sunday 13 February 2028.

Read these three first, in order: IMPLEMENTATION_PLAN.md (build order, harness,
definition of done — note §7.1 on DNS, §8.1 on deployment, and §10 for what last
shipped), LESSONS_LEARNED.md (traps already paid for — read §6 before writing a test),
and .claude/CLAUDE.md (always-on invariants).

Where things stand as of 2026-09-17:
- **11 of 15 Linear issues are Done.** Phases 0 through 3 are complete: the schema, the
  guest pages, the review instance, the RSVP window, host auth, ownership scoping, rate
  limiting, the host dashboard, CSV import and email delivery.
- 199 tests. Gate green: ruff, ruff format, mypy --strict over 48 files, migrations
  up→down→up against the test database.
- The review instance is live and has been looked at on a real phone.
- **Four issues remain, and none of them is blocked on code:**
  - **TAP-7733** hosting — **on the home lab**, decided 2026-09-17. Needs a wedding
    domain registered and delegated to Cloudflare first (see plan §7.1), then a Compose
    stack behind a named tunnel. Its real deliverable is a *tested restore*.
  - **TAP-7734** observability. Needs a Sentry DSN or equivalent.
  - **TAP-7762** photography. Every image is an openly-licensed placeholder and the hero
    is still someone else's wedding. Somebody has to take pictures.
  - ~~**TAP-7740** DNS → Cloudflare.~~ **Closed 2026-09-17.** The guest site gets its
    own wedding domain on Cloudflare; `nltlabs.ai` is never touched. Plan §7.1.

Useful things that are true now and were not before:
- Sign in to the dashboard at `/host/login`. **There is no registered host yet.** Set
  `HOST_REGISTRATION_TOKEN`, `POST /auth/register` once with it, then unset it.
- If a database was migrated before any host existed, its events belong to a placeholder
  account nobody can sign in as. `python -m scripts.adopt_events --to you@example.com`
  moves them; `--dry-run` first.
- `EMAIL_PROVIDER` defaults to `console` and prints instead of sending. That is on
  purpose. Resend with no API key stays inert rather than going live.
- The bounce webhook signs with HMAC-SHA256 over the raw body. **Resend actually signs
  through Svix over `{id}.{timestamp}.{body}`** — check the provider's docs before
  pointing anything at it. The docstring says so.

Non-negotiable:
- guests.invite_token is in people's inboxes once sent. NEVER re-key a guests row.
  Withdrawing an invitation deletes the row; that is what kills the link.
- Guest routes stay anonymous. The link IS the credential. No guest login, ever.
- htmx 2.x only — htmx 4 changed attribute inheritance and fails silently.
- 18px body text, 44px touch targets, native form controls. The guest list skews old.
  When new CSS trips the 18px floor, **raise the type** — do not add an exemption. The
  "eyebrow" loophole in LESSONS_LEARNED is exactly that mistake.
- **Rebuild `app/static/app.css` after any template or CSS change, and commit it.**
  `tests/test_stylesheet.py` will fail if you forget, which is new and is there because
  a missing class shipped a broken image to a phone.
- No # noqa, no # type: ignore, no skipped tests, no swallowed exceptions. There are
  currently zero of all four in the repo; keep it that way.
- American English. Postgres is on host port 5434, not 5432.
- This repo is PUBLIC on GitHub. Never write a credential into it — record where a
  secret lives, never its value. Invite tokens count as credentials: the review links
  live in `.review/INVITE_LINKS.md`, which is gitignored on purpose.

Do NOT use the Workflow tool unless I ask — the token pool is shared with my other
sessions. Plain subagents are fine.

Three habits, all of which earned their place the hard way:
- **If tests pass on the first run, they have proved nothing.** Break the source on
  purpose and confirm the right test goes red. Roughly fifty mutations last session
  found two real gaps — one where a shared fixture made three security tests vacuous,
  and one where a documented rule was tested nowhere.
- **A test that passes alone and fails in the suite is shared state**, not flakiness.
- **Look at the page.** Three real problems in the dashboard were invisible to 158
  passing tests, including an invite link clipped mid-token and a link pointing at
  localhost. Screenshots go to `tests/screenshots/`.

Linear: project SaveTheDate, team TappsCodingAgents (TAP), issues TAP-7725–7763.

---

## If you want a shorter version

Read IMPLEMENTATION_PLAN.md §10, LESSONS_LEARNED.md §6, and .claude/CLAUDE.md. Phases
0–3 are done, 11 of 15 issues closed, 199 tests, gate green. What is left — hosting,
observability, photography, and a DNS move that is probably unnecessary — all needs your
accounts or your camera rather than more code. Everything is self-hosted on the home lab —
there is no managed platform in this project. Same non-negotiables as the plan. Don't
use the Workflow tool. Never migrate the `nltlabs.ai` zone for this project; it carries
live company mail and §7.1 explains the alternative.
