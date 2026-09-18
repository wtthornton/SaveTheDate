# Lessons learned

Things that cost time on SaveTheDate, written down so the next session does not pay for
them twice. Newest section first within each heading.

This file is for judgment calls and traps. Mechanical facts belong in
`IMPLEMENTATION_PLAN.md`; always-on rules belong in `.claude/CLAUDE.md`.

---

## 1. The Claude harness

### `tools:` on a subagent is not a hard allowlist

`.claude/agents/std-review.md` declared `tools: Read, Grep, Glob, Bash`. It registered
with **`Write` and `Edit` as well**. Its entire contract is *"Read-only. You do not fix;
you find."* — and that contract was prose, not a guarantee.

`disallowedTools` is documented as a denylist that **overrides** `tools`, so the fix is:

```yaml
tools: Read, Grep, Glob, Bash
disallowedTools: Write, Edit, NotebookEdit
```

**The general lesson** is the one already in the plan's own rule table: a rule that must
always hold needs a mechanism, not a description. An allowlist you did not verify is a
description. Check what an agent actually registered with before trusting its contract.

### A hook can run happily and still not do its job

The plan specified a `PostToolUse` hook running `ruff format` on `migrations/versions/`,
to fix a CI failure Alembic had already caused once. It would not have worked. A freshly
generated Alembic migration fails `ruff check` with **7 errors** — `UP035`
(`typing.Sequence`), `UP007` (`Union[...]`), and import sorting. **`ruff format` fixes
none of them**; they are lint rules, not formatting. The hook would have fired on every
Bash call, reported success, and CI would have kept failing for exactly the original
reason.

It needs `ruff check --fix` *and* `ruff format`. Two related points:

- Use `${CLAUDE_PROJECT_DIR}`, not relative paths. A hook's working directory is not
  guaranteed, and a hook that silently no-ops from the wrong directory is worse than no
  hook.
- Do not send the formatter's stderr to `/dev/null`. The plan's snippet had
  `2>/dev/null`. A silently failing formatter is precisely how the original CI failure
  comes back.

### Files written to `.claude/` register on a delay

The first `/std-issue` call failed with `Unknown skill: std-issue` moments after the file
was written; it worked a few minutes later. The same applied to the subagents. **Do not
conclude the harness is broken** — and do not rewrite a working file because it has not
appeared yet. Assume a short delay, and verify before claiming either way.

---

## 2. Database and migrations

### Tests must run the migration when DDL lives outside SQLAlchemy metadata

TAP-7739 enforces an invariant with a Postgres **constraint trigger**. Triggers are
invisible to SQLAlchemy's metadata, so `Base.metadata.create_all(engine)` produces a
schema **without them**. The suite would have been testing a schema that production does
not have, and a test could pass while the real database rejected the same write.

`tests/conftest.py` now drops the schema and runs `alembic upgrade head` once per
session. It is slightly slower and entirely worth it — it also means every test run
exercises the migration.

**Generalize this:** the moment any schema object is created outside the ORM — trigger,
function, view, partial index, extension, grant — `create_all` and the migration have
diverged, and only one of them is what ships.

### A cross-row invariant needs a *deferred* constraint trigger

The rule "`attendees.attending` must agree with the per-segment `attendance` rows" cannot
be a `CHECK` — it spans rows. A plain `AFTER` trigger does not work either: a single RSVP
writes the attendee first and its segments immediately after, so the pair is inconsistent
*mid-transaction* and only correct at `COMMIT`.

```sql
CREATE CONSTRAINT TRIGGER ...
AFTER INSERT OR UPDATE ON attendees
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION ...;
```

Also: on a `DELETE` trigger, `NEW` is unassigned and referencing `NEW.field` raises.
Branch on `TG_OP` rather than relying on `COALESCE(NEW.x, OLD.x)`.

### A new invariant can make old data unrepresentable

The trigger and the backfill fought each other. Backfilling an existing accepted RSVP
creates attendees with `attending = true`, but no `segments` exist at migration time, so
every one of them would violate "attending means attending *something*". The options were
to lie about the old data (backfill everyone as declined) or to narrow the invariant.

The invariant was narrowed: it is **suspended for an event with no segments**, where
`attending` stands alone and has nothing to disagree with. That is defensible, but it is
a real narrowing and it is written down here because it will not be obvious from the
trigger source alone.

**The general lesson:** when adding a constraint, check it against the data that already
exists *and* the data the migration itself creates, before writing the backfill.

### `timestamptz` does not store a time zone

It stores UTC and discards the zone. If you need to render or reason in local time — and
an event with a schedule always does — keep the IANA name in its own column. Hence
`events.timezone` (`America/Chicago`) alongside `rsvp_opens_at` and `rsvp_deadline`.

Corollary worth remembering when reading raw JSON: the Friday 6pm welcome party
serializes as `2028-02-12T00:00:00Z`, which *looks* like Saturday. It is correct.

---

## 3. Process

### Read the issue for self-contradiction before building it

TAP-7739's description had a `Revised 2026-09-16` block appended to the top, and the
older "Proposed schema" section below it was never updated. They disagreed on two
substantial points: whether `meal_options` existed at all, and whether the `segments`
table was in scope now or deferred. The title still names meal options, which were cut.

Half a day could have gone into building the wrong half. **When a description has a
revision block, assume the sections below it are stale**, say which half you are
building, and ask rather than silently picking.

### Tests that pass on the first run have proved nothing

The TAP-7739 tests and implementation were written together and passed immediately. That
is not evidence the tests work — only that nothing fails yet. The fix is to break the
thing on purpose and confirm the right test goes red.

**And the first attempt at that was itself wrong.** Dropping the triggers directly in the
test database changed nothing, because the session fixture drops the schema and re-runs
migrations before any test. The test passed and looked like a genuine result. Only
mutating the **migration source** — the thing the fixture rebuilds *from* — produced a
real red.

**Generalize:** when mutation-testing, mutate the source of truth, not a downstream copy
that a fixture will regenerate. And confirm the negative control still passes, or you
have only proved that everything fails.

### A failing test can still be a *vacuous* test

TAP-7728's suite was written before any implementation, so 15 of 19 failed on the first
run — which looked like proof the tests worked. The other **four passed, and all four
were worthless.** Two accessibility tests found no unlabelled controls because the page
was still JSON and had no controls at all. Two stylesheet tests scanned `/static/app.css`,
got a 404 body, found no `font-size` declarations in it, and concluded nothing was too
small.

Every one of them would have gone on passing after a regression deleted the thing it
was meant to guard.

**The rule:** a test that scans a collection must first assert the collection is not
empty. `assert page.form_controls()` before checking they are labelled;
`assert response.status_code == 200` before parsing what came back. "I found no
violations" and "I found nothing" are the same result to an `assert not offenders`, and
only one of them is good news.

### A parser you wrote to check your work needs checking too

The accessibility assertions run against a small hand-rolled DOM built on
`html.parser`, so the rules are checked against rendered markup rather than template
source. The first version stored an element's own text separately from its children,
and reassembled them parent-text-first. `Please reply by <strong>December 15</strong>.`
came back as `"Please reply by . December 15"`.

Every assertion still passed, because they were all substring checks that happened not
to straddle a tag. The bug only surfaced when a real page was printed and read. Text
and child elements now live in one ordered list.

**Generalize:** a test helper is untested code in the most dangerous position — the
place you look to find out whether everything else is right. Print its output against
something real before trusting what it tells you.

### Two documents specified the same thing and disagreed

The design canvas sets body text at 15–17px throughout. TAP-7728's own accessibility
section mandates **18px minimum**, in bold, with the reasoning (the guest list skews
old). Building the design as drawn would have violated the issue that commissioned it.

The invariant won, and the exemption for tracked uppercase eyebrow labels was made
*mechanical* rather than promised: a test scans the built stylesheet for any
`font-size` below 18px and only skips selectors containing the literal string
`eyebrow`. A subagent hit this immediately — it had styled a photo caption at 12px as
`.photo-slot-label`, and rather than widening the exemption it renamed the class to
`.photo-slot-eyebrow` and said so.

**Generalize:** when two artifacts specify the same property, decide which is normative
*before* building, say so out loud, and flag the other for correction — the canvas is
now wrong and needs updating. And prefer an exemption a test can see over one a
reviewer has to remember.

### Check the repository against the handover, not the handover against itself

The session brief said TAP-7739 was "code-complete, NOT committed and NOT merged", and
that all 13 Linear issues were still in Backlog. In fact the work was committed,
merged, **and pushed to `origin/main`**, TAP-7739 was marked Done and TAP-7728 had been
moved to Todo. The brief was one session stale.

Acting on it would have meant trying to commit a clean tree and puzzling over the
result. Thirty seconds of `git log` and one Linear query settled it.

**Generalize:** a handover describes the world as of when it was written. Verify the
parts you are about to act on, especially the ones that say "not done yet".

### The project's own quality gate destroys the dev database

The definition of done requires migrations to roll back, so `/std-gate` runs
`alembic upgrade head && downgrade base && upgrade head`. `migrations/env.py` resolves
its URL from `get_settings().database_url` — which defaults to the **dev** database, not
the test one. CI never noticed because CI sets `DATABASE_URL` to the test database for
the whole job.

Locally it means the gate drops every table. That was theoretical until the TAP-7738
review instance was live and publicly reachable: running the gate wiped the seeded
event, **re-keyed every invite token**, and turned five just-published links into 404s
within a minute of verifying they worked.

Nothing real was lost — the guests are invented and re-seeding is instant — and that is
the only reason this was a nuisance rather than the project's founding invariant being
broken in public. A gate that silently destroys data is a gate nobody can run while
anything is depending on that data.

`/std-gate` and `.claude/CLAUDE.md` now pin the round trip to `TEST_DATABASE_URL`.

**Generalize:** a command that is safe in CI is not automatically safe on a developer's
box, because CI's environment is part of what makes it safe. Before running anything
destructive locally, check what it resolves its target from — and check twice when
something is actively serving from that target.

### A whole breakpoint can be missing and every test still pass

The design canvas has nine artboards. Four are mobile, three are desktop at 1440px, and
the first build read only the mobile ones — because the brief handed to the subagent
named only those. So there was no desktop layout at all: at 1440px every page rendered
as a ~670px phone column centred in empty space, with 96px thumbnails where the design
has 470px images.

Forty-five tests were green. Not one of them knew what a page looks like. The gap was
found by a person opening the site and saying "this looks nothing like the design",
which is the most expensive possible detector.

The fix was a browser-driven suite (`tests/test_visual.py`) that measures **rendered
geometry** at phone and desktop widths and writes full-page screenshots to
`tests/screenshots/` for a human to look at. On its first run it failed five ways: the
desktop hero height, guests not side by side, the desktop rendering as a phone column,
beats not alternating, and — an accessibility requirement that had been *claimed as
met* — the RSVP form scrolling sideways at 200% zoom.

Three things this changed for good:

- **Measure computed styles, not the stylesheet.** A CSS scan cannot see the cascade,
  inheritance, or which Tailwind utilities actually won. The browser test caught three
  text links rendering at 29px that the stylesheet scan called fine.
- **Screenshots are part of the deliverable.** An assertion only catches what someone
  thought to assert. A person glancing at ten PNGs catches what nobody thought of.
- **Check every artboard is accounted for before building.** Count them, and say out
  loud which ones are in scope.

### A test that passes is not a test that works, part two

Writing the above, the first version of the "the couple is not named twice" test
**passed against a page that plainly showed the name twice.** It scanned
`h1, h2, .hero-names, [class*="wordmark"]`, and the side panel uses
`<p class="rsvp-side-names">`, so it counted one and went green.

Exactly the failure the lessons above describe, committed again while writing the test
meant to prevent it. The rule holds and is easy to forget: **a new test must be seen
failing against the actual defect** before it is trusted. Not against a mutation, not
in principle — against the real broken thing in front of you.

### Jinja reloads templates. It does not reload Python.

A helper was added to `app/templating.py` and called from a template. The whole suite
went green — and the published review instance started returning **HTTP 500** on The
Wedding page, because the running uvicorn had imported that module before the helper
existed. Jinja re-reads templates from disk on every request; a Python module is
imported once.

The tests could not catch it, and never will: they start a fresh server. Only the
long-lived process was wrong, and only the person reloading the public URL would have
found out.

`scripts/review-instance.sh reload` now restarts the app **on the same port**, so the
tunnel — and therefore the URL, and every invite token already sent — survives. Use it
after any change under `app/*.py`. `up` would also work and would re-key every link.

**And the fix itself broke the site a second time**, which is the more useful half of
the story. The first version of `reload` parsed the port out of `/proc/<pid>/cmdline`
with `tr ' ' '\n' | tr -d '\0'` — but that file is NUL-separated, not space-separated,
so the whole command line collapsed into one token and uvicorn was handed a garbage
port. Running it took the live instance down. `shellcheck` was clean throughout,
because the bug was in what the pipeline *meant*, not in its syntax.

**Generalize:** a command that restarts a service is itself a thing that has to be run
before it is trusted, ideally while watching what it does to the service. "It passed
shellcheck" says nothing about whether it works.

### Search for the place, not the subject

Four schedule cards needed photographs. Searching freely-licensed stock for the subject
gave nothing usable: "beach house" returned Californian surfers, "restaurant" returned
Red Lobster storefronts and Disneyland, "fishing boat" returned tropical harbours. The
conclusion looked like "there are no good free photographs of these things".

Searching for **Port Aransas** and **Mustang Island** directly returned real photographs
of the actual island, including the Tarpon Inn porch and the ferry at sunset — the same
ferry the welcome page's story is about. A guest who knows the place will recognise both.

**Generalize:** when a search for a generic subject returns generic rubbish, try the
specific proper noun before concluding the material does not exist. The index is often
organised around names, not categories.

A licensing note that came with it: five of the photographs are CC BY rather than CC0,
and CC BY wants the credit visible to a reader, not filed in the repository. The names
live next to the mapping that uses them, so a swapped photograph cannot leave its credit
behind. That rule immediately caught a real slip — a photograph was committed and
credited while appearing on no page at all.

### A green CI is not the same as the definition of done

The project's stated definition of done required migrations to apply **and roll back**.
CI only ran `alembic upgrade head`. The `downgrade` path — the harder half, and the one
that matters in an incident — had never been exercised by automation. Similarly, `mypy`
covered `app migrations tests` but not `scripts`, so a new directory would have been an
unchecked corner.

Read the definition of done against what CI actually runs, not against what it is named.

---

## 4. Deployment and this machine

Researched 2026-09-16 and re-checked 2026-09-17 across `~/code`, chiefly `NLTWeb`.

> **This project now self-hosts on the home lab** (decided 2026-09-17), so the
> managed-platform specifics below are kept as transferable lessons rather than as
> instructions. Plan §8.1 is the current deployment plan; §7.1 covers why the guest site
> gets its own domain.

### Never put a credential in this repo

`github.com/wtthornton/SaveTheDate` is **public**. Real secrets live in untracked `.env`
files and in the relevant dashboards. Record *where* a credential lives, never its value.
This applies to anything written into the plan, this file, or a commit message.

### A domain is not just a website — check what else lives in the zone

TAP-7740 was written as "moving DNS might break the company site". `nltlabs.ai` also
carries a live Microsoft 365 mail deployment and its client-autoconfiguration records.
A broken website is obvious in seconds; **misrouted mail is invisible for hours and is
not recoverable**, and the zone's DMARC policy silently quarantines rather than bounces,
so nobody gets a warning either.

Before touching any zone, enumerate it live and ask what breaks for each record — not
just the one you came for. The details are on TAP-7740 in Linear; they are deliberately
not in this public repo.

### No single document lists all the hostnames

`NLTWeb/docs/DEPLOY.md` documents five hostnames. The `render.yaml` files across `~/code`
declare eleven across the nltlabs zones. Any zone work needs a **live record export** as
its inventory, not a doc — and certificate transparency logs will surface hostnames that
no internal document mentions at all.

### A build step you do not control will not install your dependencies

NLTWeb learned this in production: a new `import` broke its live site while GitHub
Actions stayed green, because CI ran `uv sync` and the host did not. Nothing in
`pyproject.toml` reaches a build environment unless the build command puts it there.

**SaveTheDate is not exposed to that**, because it ships to a home lab it controls
end to end — but the same shape recurs wherever two environments are assumed to install
the same things and only one actually does.

The version of it that *does* apply here: the Tailwind v4 standalone binary lives at
`~/.local/bin/tailwindcss`, **outside the repo** (110MB, and this repo is public), and
the built `app/static/app.css` is **committed**. That is deliberate — it means no deploy
needs the binary present at all. The cost is remembering to rebuild and commit after
editing the source, and §6's stylesheet lesson is what happens when that is forgotten.

### Committed infrastructure YAML is not necessarily what is running

Also from NLTWeb, and worth keeping even though this project no longer deploys there:
its `render.yaml` is **documentation, not a control surface** — no Blueprints exist in
that account, every service is dashboard-managed, and editing the file changes nothing
about a live deploy. It had already drifted from live before anyone checked.

The habit worth copying is its drift checker, which compares the file against the live
API and **exits non-zero when it could not look**, rather than reporting success. A
check that passes when it failed to run is worse than no check. That principle applies
to the home lab's Compose file just as well: assume the committed file and the running
system have diverged until something proves otherwise.

---

## 5. Carry forward

- `continuous-learning-v2` is already installed at user level and registered this project
  as `2ec864647abf`. It observes tool patterns via hooks; it does not capture judgment
  calls, which is what this file is for. Both are worth having.
- The four subagents `~/.claude/agents/ralph.md` delegates to
  (`ralph-explorer`/`-tester`/`-reviewer`/`-architect`) still **do not exist on disk**.
  Harmless while Ralph is unused; it will fail the moment it is pointed at this repo.

---

## 6. The session of 2026-09-17

Blocks A, B and C of the implementation plan: TAP-7763, 7729, 7725, 7726, 7727, 7730,
7732, 7731. The suite went from 74 tests to 199. What follows is only the part that
cost something to learn.

### Mutation testing found two defects that the tests themselves had

This is the session's most useful result, and it happened twice.

**A shared fixture made three security tests vacuous.** `client` was originally derived
from `anonymous_client`, so they were *the same `TestClient` object*. Signing in through
one signed in the other, and `test_listing_guests_anonymously_is_401` was quietly
asserting that a 200 was a 401 — except it was not asserting anything, because the
request carried a session cookie. All three "unauthenticated" tests passed for a reason
that had nothing to do with the code under test. They are now two separate clients with
separate cookie jars, and the docstring says why.

**A behavior asserted only in a docstring.** `SlidingWindowLimiter.retry_after` says "a
blocked call does NOT extend the window", which matters because otherwise one impatient
guest reloading a throttled page pushes their own unlock further away. A mutation that
made blocked calls count as hits passed the *entire* file. The rule was documented and
tested nowhere.

The pattern behind both: **a test can be green for a reason unrelated to the thing it
names.** Watching it fail against a deliberate break is the only way to find that out.
Every issue in this session was mutated after its tests went green; two of the roughly
fifty mutations survived, and both were real gaps.

### A committed build artifact goes stale in perfect silence

`app/static/app.css` is committed on purpose so a deploy never runs Tailwind. The cost
is that it can fall behind the templates with nothing failing. `h-[160px]` and
`lg:h-[260px]` on the ferry photograph were never in the committed CSS, so that image
had no height cap and rendered at its natural 900x599 aspect — most of a phone screen
where 160px was intended.

Every test was green, **including the browser-driven visual suite**, because the page
was entirely valid. It just was not the page anyone had designed. Bill found it on a
phone.

`tests/test_stylesheet.py` now checks that every Tailwind-shaped class a template names
resolves to a rule. It needs no Tailwind binary, so it runs in CI. The scope — tokens
carrying a variant or an arbitrary value — is stated rather than an exemption list,
because bare names like `.beat` are hooks `test_visual.py` selects on and are *supposed*
to have no rule.

### An issue can contradict the schema it was written against

TAP-7730 asked for "counts per meal option". TAP-7739 had cut meal options months
earlier, and `.claude/CLAUDE.md` says "No per-plate meal choice. Dietary tags only."
Building the issue as written would have meant inventing a column that was deliberately
removed.

**Read an issue against the code before building it, not just for internal
consistency.** The issue was corrected in Linear first, then built. This is the second
time this project has hit it — §3's "Read the issue for self-contradiction" was the
first — and the general form is: the backlog ages, and the schema is the thing that is
true.

### Process-global state has to be reset between tests

The rate limiter counts in process memory, and every `TestClient` request arrives from
the same address. So the whole suite shared one bucket: enough guest-page tests ran
inside a minute to spend the allowance, and a later test got a 429 it never asked for.

The tell was the shape of the failure — **it passed alone and failed in the full run**.
Any test that passes in isolation and fails in the suite is shared state, not flakiness,
and the fix is a reset fixture rather than a retry. `conftest` now drops the counters
per test, and the suite gives the same answer twice in a row.

### Watch for the second place the same value is written

Flipping the couple's name order looked like three headings. The test that checked it
named **nine** elements, because `event.title` and `host_name` are rendered into
`<title>` and into headings and live in the database, not the templates. The test found
the seed data and the fixtures; reading the templates had not.

Worth generalising: when changing a value that appears in copy, grep for it rather than
editing where you remember it being.

### Looking at the page is still not optional

Three problems in the host dashboard were invisible to 158 passing tests:

- The invite link rendered inside a table column, showing 476px of the 826px it needs
  and cutting off mid-token. The value was correct and the markup valid.
- The link was built from `public_base_url`, so a host on the review tunnel would have
  copied a **localhost** link and sent it to a guest. It now comes from the request.
- The new upload form pushed the page 30px past a 390px viewport, because a file input
  carries a wide intrinsic minimum and flex children will not shrink below it.

Only the third was caught by a test, and only because the overflow test had been written
an hour earlier. The other two needed a screenshot and a pair of eyes.

### An unconfigured integration should be loud, not silent

`ConsoleTransport` is the default mail transport, and that is a deliberate choice rather
than a placeholder. An unconfigured deployment that prints is obvious and harmless; one
that silently succeeds is a lie; one that mails real guests by accident is worse than
both. `ResendTransport` stays inert without an API key, and a test asserts that
`EMAIL_PROVIDER=resend` with no key does **not** produce a live transport.

The same shape applies to `HOST_REGISTRATION_TOKEN` and `EMAIL_WEBHOOK_SECRET`: unset
means closed, so forgetting to configure either fails safe rather than wide open.

### A recommendation that rests on an assumption has to name the assumption

Twice in one day I argued that TAP-7740 — migrating the `nltlabs.ai` zone to Cloudflare
— was unnecessary. The second time I backed it with live evidence: six hostnames on that
zone already served from a managed platform over plain CNAMEs from GoDaddy, so the
wedding site could have a hostname without moving anything.

Hours later the decision changed to self-hosting everything, and the argument
evaporated. A *named* Cloudflare Tunnel does require its zone on Cloudflare, and the
partial/CNAME setup that would avoid that is Business-plan only. Every fact I had
gathered was true and correctly verified; the conclusion still stopped holding, because
it silently depended on "we are deploying to a managed platform" — a premise I never
wrote down, because at the time it did not feel like a premise.

The conclusion survived by luck: a separate wedding domain closes TAP-7740 anyway, for
an unrelated reason. That is not a defence.

**Write the load-bearing assumption into the recommendation itself**, so that when it
changes the recommendation visibly expires instead of quietly going stale. "X is
unnecessary" ages badly; "X is unnecessary *while we deploy to Y*" fails loudly the
moment Y changes.

### Say what you cannot verify, rather than guessing at it

The bounce webhook verifies HMAC-SHA256 over the raw body in constant time. Resend
actually signs through Svix, whose signed payload is `{id}.{timestamp}.{body}` — not the
bare body — and that cannot be confirmed without an account. The function is the right
shape and the right comparison, the bounce path is real and tested, and the docstring
says in capitals that the exact payload must be checked against the provider's
documentation before pointing anything at it.

That is better than either a stub or a guess dressed up as an implementation.

### You cannot enumerate a zone from outside, so "essentially empty" is a guess

I checked `tapphouse.co` before recommending it and reported the zone as essentially
empty: no MX, no SPF, no DKIM, no DMARC, no autodiscover. All true, and it is the right
category to check first, because mail is where a DNS mistake is silent and unrecoverable.

Then Cloudflare's import scan showed `home.tapphouse.co` — a live CNAME to Home Assistant
Cloud. A record I would never have found, because **subdomains cannot be enumerated from
outside without a zone transfer**, and `home` was not a name I thought to probe. If it had
been dropped, or carried across proxied, remote access to the house would have broken.

Two things follow. **Say "no mail, and I cannot see subdomains from here" rather than
"essentially empty"** — the second is a claim the method cannot support. And **the
registrar's own record list is the inventory**, not a guess assembled from outside; the
scan is best-effort and its own docs say so.

The pre-flight that did work is worth repeating: query the *new* nameservers directly
before cutting over, so a missing record is caught while the old ones are still
authoritative. Checking for DS records belongs in the same pass — a stale DS with new
nameservers takes a domain completely dark for validating resolvers, and it is invisible
until someone with a validating resolver complains.

### Say what the thing is, or everyone has to go and find out

Asked whether this was a multi-tenant product or one wedding's app, I could not answer
from the documentation. The README's subtitle says "a service for weddings and events",
which is product language. The templates hard-code one couple's story. Nothing stated
the split, so answering meant reading the schema, the routers, the tests and the
templates.

The answer turned out to be worth having: the data layer is genuinely multi-tenant —
`hosts`, `events.host_id`, ownership-scoped queries, nine cross-host tests — while the
presentation layer is deliberately one wedding, with exactly seven values reaching the
templates from the database. That is a good place to be, because the expensive-to-change
parts are general and the cheap-to-change parts are specific.

But it was an *implicit* good place, and implicit architecture gets "fixed" by the next
person. Someone reading only the README would reasonably start generalising the
templates toward a product that has no customer.

**Write the tenancy model down**, including the parts that are deliberately not general
and why. Recorded as plan §12. The rule generalises: if answering "what is this?"
requires reading the code, the documentation has a hole in it, and the hole is where
someone else's well-meant refactor goes.

---

## 7. The public front door, 2026-09-17

TAP-7775 and TAP-7781: the two pages anyone can reach without a token. Most of what
follows is about the gap between *a test passing* and *a person being able to use the
page*, which turned out to be wider than the suite could see.

### A cached stylesheet makes a correct server look broken

Bill opened the new card and reported it looked wrong — "is it missing css or
animations?" The server was serving the right file. It had been the whole time. The
stylesheet fetched over the tunnel was byte-identical to the local build, all eleven
new rules present.

The response carried `cache-control: max-age=14400`. Cloudflare returns `/static/*`
with a four-hour browser TTL and caches it at its own edge, and `/static/app.css` never
changed its URL when its contents changed. He had loaded the page while the file was
being rebuilt — a mutation sweep rewrote it a dozen times and one rebuild failed
outright, leaving broken CSS up for a few seconds — and his browser then held that
version.

**Nothing about the page could have told him.** No error, no console message, no
version anywhere. A stale stylesheet renders as a plausible design, which is the worst
possible failure mode: it looks like a bug in the work rather than a bug in delivery.

The fix is that **a changed file lives at a changed address**: `static_url()` appends a
content hash, so a rebuild moves the URL and the cache cannot answer for it. The HTML
itself is uncached (`cf-cache-status: DYNAMIC`), so the new address is seen at once.

A cache header would have been the wrong fix. The goal is not to cache less — caching a
stylesheet for four hours is correct and desirable. The goal is that a different file
should have a different name. `host_base.html` had the same bug; a grep found it, and a
test now fails on any template that hard-codes the path.

### An element at `opacity: 0` is still hit-tested

The envelope's panels end their animation invisible, and stay exactly where they are in
the layout. Invisible is not intangible: `elementFromPoint` still returns them, and a
tap still lands on them.

This bit twice, and the second time was worse than the first. Once with the pocket
resting across the bottom edge of the link to the wedding site, taking the taps that
landed there. Then again after the envelope was rebuilt, when the new back panel lay
across the *entire* card — every line of it, not just the link.

Anything that fades out but stays in the flow needs `pointer-events: none`. It happens
to inherit through `display: contents`, so one declaration on the group covers all of
it.

### A `z-index` tie is settled by document order

The rebuilt back panel and the card both had `z-index: 1`. The envelope comes after the
card in the markup, so the paper won — and sat on top of the thing it was supposed to be
behind.

Nothing about `z-index: 1` looks wrong when you read it. The card's own rule said 1, the
panel's said 1, and each was individually defensible. **A stacking order is a total
order, and every element in it has to be written down as one** — the fix was a five-line
comment listing all five layers and their numbers, so the next edit can see the whole
ladder instead of one rung.

### Waiting for the wrong thing made every screenshot a lie

The visual tests wait for the reveal to finish before measuring and shooting. The wait
watched the flap. The flap finishes 900ms before the pocket does.

So every screenshot was taken with half the card still inside the envelope, and **not
one assertion noticed**, because none of them was looking at the pocket. The tests were
green, the geometry checks all passed, and the picture on disk showed a card with its
date and its link obscured. Only looking at the file caught it.

The wait now asks the browser which animations are still running, rather than naming an
element and hoping it is the last one:

```js
document.getAnimations()
  .filter(a => a.effect.getComputedTiming().iterations !== Infinity)
  .every(a => a.playState === 'finished')
```

**Ask the system for the condition, do not re-derive it from a part you happened to
think of.** The derived version was wrong the moment a second animation existed.

### Sampling the centre of an element is not sampling the element

The obstruction test hit-tested the middle of each line of the card. It passed with the
pocket lying across the bottom edge of the link, because the pocket cleared the link's
centre and not its lower half.

Worse than a missed bug: the test's own docstring and a CSS comment both claimed the
pocket "swallows every tap meant for the link", which the evidence did not support. It
was covering part of it. A claim written into a comment gets believed later, so an
overstated one is a small lie left in the source.

Five points per element — centre and four inset corners — catches it, and the comment
now says what is actually true. **When a mutation you expect to be caught is not, the
test is sampling too thin; and when you write what a bug does, write what you measured
it doing.**

### A mutation that does not apply has proved nothing

Two of the roughly twenty mutations reported "NOT CAUGHT" and both were wrong:

- One removed a line and a comment terminator together, breaking the CSS. The Tailwind
  build failed, the *old* `app.css` stayed on disk, and the tests ran against unmutated
  code.
- One passed `-k 'javascript or envelope'` through an unquoted shell variable, which
  split into four arguments and selected zero tests. "1 warning in 0.00s" is not a pass.

Mutation testing is a test of the tests, which makes it a thing that can itself silently
pass. **Assert that the mutation applied** — the script now fails if the replacement
did not change the file, and prints the test count so an empty selection is visible.

### `SIGHUP` does not reload this cloudflared, and the documented risk was the wrong one

Two things were wrong in the same five minutes, both of them written down beforehand as
if known.

I said SIGHUP would reload the tunnel's ingress in place without dropping connections.
It terminated the process. `Restart=always` brought it back with a new PID in about
three seconds, so the effect was a restart — and the unit has no `ExecReload` either
(`CanReload=no`), which was checkable in advance and which I checked only afterwards.

And the risk I had asked Bill to weigh — that restarting cloudflared might disturb Home
Assistant — did not exist. `home.tapphouse.co` is a CNAME straight to Nabu Casa and
never touches this tunnel. One `dig` would have shown it, and the plan and the handoff
prompt had both been carrying the wrong caution since the hosting session.

**A caution aimed at the wrong risk is worse than none**: it spends the reader's care on
nothing while the real cost goes unmentioned. Both documents now say what a tunnel
restart actually costs, which is a few seconds of the review instance.

### The `reload` path has to carry the same environment as the `up` path

`scripts/review-instance.sh` needed a new environment variable so one hostname serves
the card. Setting it in `up` alone would have meant that a `reload` — the command the
project tells you to run after every Python change — quietly dropped that hostname back
to the wrong page.

That is the same shape as the bug `reload` exists to prevent, one level up. **Every
place a process is started is a place its environment is declared**, and they drift
apart silently because only one of them is exercised on the day you write it.

### A reference beats a description

The first envelope was, structurally, an envelope: a flap, a pocket, a seal. It read as
a card with two rectangles fading off it, and no assertion could have told me so.

Bill sent a link to Greenvelope's animated save-the-dates. Reading what they actually
do — a closed envelope with the names and a stamp on the front, a flap that opens to
show a *lined* interior, the card drawn up out of it — named the three things mine was
missing in one go. The liner in particular is the detail that makes an envelope look
chosen rather than generated, and no amount of describing "an envelope opening" would
have produced it.

**When taste is the specification, ask for the reference rather than more adjectives** —
and then go and read the reference rather than guessing at what is on the page.

### Two public pages, one hostname each

The card was first served at `/save-the-date` on every hostname, so that it could be
reviewed without a DNS entry. That also put it on `dev-wedding.tapphouse.co`, and Bill
rejected it the moment he saw the URL.

He was right, and the reasoning generalises: **a page reachable under two names is one
that gets linked to by the wrong one.** A convenience for the developer became a second
public address for a page that should have exactly one.

The replacement costs nothing: browsers resolve every `*.localhost` name to loopback, so
`savethedate.localhost` gives local review a second hostname with no DNS, no hosts file
and no second process — and the visual tests now drive the real Host-header dispatch in
a real browser instead of a path that only existed for them.

---

## 8. Production, 2026-09-17

TAP-7733: the production stack, the tunnel cutover, and the backup pipeline. Most of
what follows is about verification that looked like it worked and did not.

### Three attempts to prove the restart policy, and the first two proved nothing

`restart: unless-stopped` is the line that decides whether the site returns after a
power cut with nobody logged in. It is worth actually testing, and testing it took
three goes.

**`docker kill` proved the opposite of what I read into it.** The container went to
`exited`, `RestartCount` stayed 0, and for a moment that looked like a broken restart
policy. It is not: Docker treats a CLI-issued kill as an *operator* stop, and
`unless-stopped` means exactly "do not restart something a human stopped". The test
was wrong, not the config.

**`docker exec ... kill -9 1` failed to run at all.** `kill` is not in
`python:3.12-slim`. The exec errored, nothing died, and the very next line of my own
script printed "app recovered on its own after 1s" — because the app had never gone
down. That is the §7 lesson repeating verbatim: **a mutation that does not apply has
proved nothing**, and the check that catches it is watching a counter move, not
watching a request succeed.

**`os.kill(1, SIGKILL)` from inside the container silently did nothing either.** The
kernel protects the init process of a PID namespace from signals it has no handler
for, including SIGKILL, when they come from inside that namespace. `RestartCount`
stayed 0 and the state stayed `running`.

SIGTERM worked, because uvicorn installs a handler for it, so the signal is delivered
and the process exits. `RestartCount` went **0 → 1** and the app answered again in
about a second. The proof is the counter moving; every earlier run had a plausible
success message and a counter that had not moved.

### A drill that cries wolf on an empty database gets ignored

The restore drill failed if the restored database had no guests, which is right: a
backup that restores an empty guest list is the catastrophe this whole exercise
exists to catch.

Then production was truncated back to empty — correctly, since the invented data had
served its purpose — and the weekly drill would have failed every week until the real
guest list is loaded, which could be months.

That is worse than no drill. **A check that is red for a known and acceptable reason
teaches whoever reads it to skip the whole class of message**, and the day it goes red
for a real reason, nobody looks.

The rule is now split by where the claim comes from. The manifest is read from the
*live* database moments before the dump, so a backup that held 40 guests and restored
0 is still a shortfall and still fails. A backup that held 0 and restored 0 is a
faithful backup of an empty database: it warns loudly that it proved nothing about
guest data, and passes. It becomes a real check by itself the day guests exist.

### Compose substitutes nothing for an unset variable

`${POSTGRES_PASSWORD}` with no `--env-file` does not fail. It expands to the empty
string, and Postgres starts with a blank password. The failure is silent, and it
produces a *running* stack, which is the worst kind.

Two defences, because either alone is thin: `:?` guards in the compose file so an
unset variable is an error, and `scripts/prod.sh` as the only supported way in, so
the flag cannot be forgotten. The script also rejects a `POSTGRES_PASSWORD=` line
with nothing after it — the `:?` guard catches *unset*, not *empty*, and an example
file that was copied but never filled in produces exactly the empty case.

### Production settings belong where they can be reviewed

The handoff listed the production environment as things to put in `.env.prod`:
`SAVE_THE_DATE_HOSTS`, `TRUSTED_CLIENT_IP_HEADER`, `REVIEW_INSTANCE`, and the rest.
It called them "where the easy-to-miss items are", which is the argument against
putting them there.

A gitignored file cannot be reviewed, cannot be diffed, and cannot be asserted. Every
one of those values has a specific failure — the card on the wedding hostname, one
rate-limit bucket for the whole world, a draft banner on a real invitation — and all
of them are invisible until a guest hits them.

They went into `docker-compose.prod.yml` instead, with only true secrets left in
`.env.prod`, and `tests/test_production_stack.py` asserts each one against the
breakage it causes. Fourteen tests, each mutated and confirmed red. **The test could
only be written because the value was committed**, which is the actual argument: a
setting you cannot test is a setting you are hoping about.

### An ephemeral port is fine for a throwaway and not for production

The review instance picks a free port by binding port 0, which lands somewhere in
32768–60999. That is `/proc/sys/net/ipv4/ip_local_port_range` — the range the kernel
draws *outbound* source ports from. A long-lived listener there can collide with an
outbound connection after a restart.

Survivable for an instance whose URL changes anyway. Not for the one behind a
hostname printed in an ingress file. Production is on 8100, deliberately outside the
range.

### Checking the method before trusting the result

Two small ones, both the same shape as §6's "watch what your test client actually
sends".

The RSVP endpoint is `PUT /api/invites/{token}/rsvp`. I posted to it, got 405, and
only then read the router. A 405 is a cheap way to find out; a 200 against the wrong
assumption would not have been.

And `rclone` printed `Config file not found - using defaults` on every call, which is
accurate and harmless and would have made up most of the timer's journal. Silenced
by pointing `RCLONE_CONFIG` at `/dev/null`, because the remote is configured entirely
from environment variables. **A log that is mostly noise is a log nobody reads**, and
these two units are the only warning that the guest list is not recoverable.

### Proving a restore needs something to restore

The drill compares restored row counts against a manifest. Against an empty
production database it compared 0 to 0 and passed — the exact vacuous green §6
warned about.

So production was temporarily seeded by restoring the dev database into it, which had
the side benefit of exercising the disaster-recovery path itself, and the drill then
read back 5 guests, 2 RSVPs and 3 attendees. Production was truncated afterwards and
verified empty, table by table.

It also settled a claim §12 makes and nothing had tested: both public pages render on
an empty production database. They do — they hard-code the couple, the date and the
place, so they never touch a row. That is the state production is in right now, and
it is the state it will be in on its first day.

---

## 9. Moving the date, and two visual asks, 2026-09-17

### A value repeated nine times is a value that will be changed eight times

The wedding moved a week. The seed script expressed its schedule as nine absolute day
numbers — `_at(11, 18)` for the welcome party, `_at(13, 15)` for the ceremony, and so
on — so shifting it meant nine correct edits with no test that would notice a missed
one. A welcome party left on the wrong Friday renders perfectly.

It is now one constant and eight offsets: `_at(-2, 18)` is the Friday, `_at(0, 15)` the
ceremony. The next move is a one-line change, and a partial move is not expressible.

**The general form:** when a change requires the same edit in N places, the bug is not
the change, it is the N. Fix the N first, while you are already in the file and the
correct values are in front of you.

### Shift the data, do not re-seed it

The dev database had to show the new date. The documented way to change seeded data is
to re-seed — and `seed()` deletes the event and mints new invite tokens, which would
have invalidated every review link already sent.

An `UPDATE … + interval '7 days'` on the event and its segments did the same job and
never touched a `guests` row. The check that it worked was the md5 of every invite
token, taken before and after: identical.

The project's hardest invariant is that a `guests` row is never re-keyed. That rule is
written about production, but the review instance has real links in real inboxes too.
**Ask what a "refresh" destroys before reaching for it**, and prefer the narrow write.

### "Too skinny" meant "too wide", and the measurement said so

Asked whether the welcome page's paragraphs were the right width, the obvious response
is to widen them. The measurement said the opposite: at 18px in a 632px column the two
body paragraphs were already running **85 and 95 characters** a line, well past the ~75
where the eye starts losing its place on the return sweep. Widening would have made the
real problem worse while appearing to address the complaint.

The thin *look* came from the type size, not the column. 20px brought all three
paragraphs to 76-78 characters. **18px is the floor and was never a target** — the same
mistake the "eyebrow" loophole made in the other direction.

Two things worth keeping. **Readability is measured in characters, not pixels**: the
same column is right at 20px and wrong at 16px, so a test that asserts a column width
asserts the wrong thing. And **when a complaint names a cause, measure the cause before
acting on it** — the complaint was accurate about the symptom and wrong about the
reason, which is the normal case.

### The numbers said it fit; the picture showed a collision

Shortening the welcome hero from 702px to 340px made the page fit a laptop exactly:
`scrollHeight` 900, viewport 900, zero overflow. Every assertion passed.

The monogram was sitting on top of "Lisa and Bill". `.hero-mark` is absolutely
positioned at `top: 40px` and `.hero-copy` is pinned to the bottom; in a 702px hero
they never met, and in a 340px one they overlapped. No height measurement can see that,
because both elements are exactly where they were told to be.

**Two elements that do not collide are not "safe" — they are untested.** Absolute
positioning from opposite edges of a container has a collision height, and shortening
the container is the operation that finds it.

The fix was already in the codebase: `welcome.html` carries `hero-mark lg:hidden`,
because the monogram is a mobile-only element that the hero nav replaces at desktop.
The public welcome has no nav and had quietly kept it at every width. **Before
inventing a fix, check what the sibling page does** — a design system that already
answered the question is cheaper and more consistent than a new rule.

### Say which viewport you fixed

The welcome page now fits a 1440x900 laptop with nothing to spare, and still scrolls
about 190px on a 390px phone — down from 414px, but still scrolling. Its text alone is
roughly 610px in a phone column and the 18px floor is not negotiable, so closing the
rest means cutting copy, which is a content decision rather than a CSS one.

Reporting "it fits now" would have been true of the screen it was checked on and false
of the one most guests will use. **A layout claim without a viewport attached is not a
claim.**

### Making something bigger is a way of finding the bugs it already had

Enlarging the save-the-date card produced two complaints in one sentence from Bill —
"the automation is a mess" and "maybe it is too big" — and they were two different
problems that the same change had surfaced.

**Too big** was a proportion problem. At 680px the stage measured 680x708: a square,
which is not a shape an envelope comes in, with the addressee and stamp adrift in a
large empty expanse of paper. The near-square ratio had always been there; scale is
what made it legible as wrong.

**The mess** was a genuine defect, and older than the change. The card rises from
`translateY(46%)` — nearly half its height below the envelope — and the front panel
stops exactly at the envelope's bottom edge, so nothing ever covered the overhang. For
the whole rise, the date, the place and the link were visible hanging out underneath
the paper. Enlarging it only made the overhang taller and moved it up into the middle
of the screen.

**Every assertion measured the end state**, where the transform is `none` and nothing
overhangs, so the suite was green for the bug's entire life — including the tests
written specifically to check the envelope gets out of the card's way.

Two techniques came out of it, both worth keeping.

**Scrub the animation; do not wait for it.** Screenshotting at wall-clock offsets drifts
badly, because each screenshot costs a few hundred milliseconds and the error
accumulates — by the fifth frame the picture is nowhere near the time it claims. Pausing
every finite animation and setting `currentTime` asks the browser to *be* at a moment
rather than hoping it is. This is the same lesson as `document.getAnimations()` for the
settle-wait, one step further on.

**Hit-test, do not measure, when the question is "is this painted?"** A clipped element
still reports its full `getBoundingClientRect`, so no amount of geometry could tell
whether the overhang was visible. `elementFromPoint` respects the clip.

The general form: **a visual bug that only exists mid-transition is invisible to a suite
that only measures rest states**, and rest states are what everything naturally asserts,
because they are the only moment that holds still.

### A computed value is not a used value, and CSS has properties that veto other properties

The envelope's flap has two faces — cream paper outside, a teal striped liner inside —
with `backface-visibility: hidden` on both, so the liner turns toward the reader as the
flap passes vertical. That liner is the detail the envelope was rebuilt for after Bill
pointed at Greenvelope; §7 records it as "the detail that makes an envelope look chosen
rather than generated".

**It was never once visible.** The flap showed its cream outer face for the whole
reveal, from the day it was built.

`opacity` is a *grouping* property: an element that carries one is forced to
`transform-style: flat`, whatever its own rule says. The flap's open animation faded it
out, which flattened its 3D context, which disabled `backface-visibility` on both
faces — so the outer face never hid and the liner was painted nowhere. Three properties,
each individually correct, and the third quietly cancelled by the first.

Nothing could have caught it by inspection. `getComputedStyle` reports `preserve-3d`
throughout, because **the computed value is not the used value** — the flattening is
applied later and is not visible anywhere in the CSSOM. Every screenshot showed a
perfectly plausible cream flap, which is what an envelope flap looks like.

Finding it took a *decisive experiment* rather than more reading: rotate the flap
statically past vertical, with no animation at all, and see what is painted. The liner
appeared immediately, which named the cause in one step. Two earlier hypotheses —
`filter` on the outer face, then `clip-path` on both faces, each of which really does
force flattening — were both tested and both wrong. **Testing a hypothesis you believe
is how you find out it is the wrong one cheaply**; reasoning harder about it is not.

The fix splits the animation: the flap turns, the faces fade. A leaf can carry opacity
safely because it groups nothing.

The test asserts against the animation the browser is actually running —
`getAnimations()` and `getKeyframes()` — rather than against the stylesheet text, so it
holds however the rule is later rewritten. Confirmed by putting the fade back and
watching it go red.

**The rule worth carrying:** when a visual property "does not work" and the CSS reads
correctly, suspect a neighbouring property that changes the rules for it, and reach for
a static experiment rather than a closer read.


---

## 10. Photorealism, and four bugs that only a browser could report, 2026-09-17

The envelope was rebuilt a second time, into two doors that swing open (plan §15). Four
things went wrong on the way, and the pattern across them is the point: **every one was
found by putting a picture on the screen and looking at it.** None was reported by a
type checker, a linter, or any of the 275 assertions.

### Lighting noise is not the same as lighting a surface

The first wax seal ran `feSpecularLighting` over the turbulence field directly. That is
the obvious reading of "add highlights to the noise", and it produces thousands of tiny
independent highlights — a curdled, mottled thing that looks like meat, not wax.

What makes wax read as wax is that it is a **dome**: one broad highlight rolling off into
shadow. So the light has to fall on a dome-shaped surface, and the cheapest dome
available is the shape's own alpha channel put through `feGaussianBlur`. The blur turns a
flat polygon into a soft ramp, and the lighting filter reads a ramp as curvature.

**The rule:** in an SVG filter chain, decide what *surface* you are lighting before you
decide what light to use. Noise is a good height map for fibre, which is genuinely rough,
and a terrible one for wax, which is genuinely smooth.

### A filter's output is clipped, so the filter can pull the shape away from the clip

The two halves of the seal are one disc, cut by two complementary clip paths, so their
edges interlock the way a real fracture does. But the wax filter ends in
`feDisplacementMap`, which shoves pixels up to five units sideways — and the clip is
applied to the filter's **output**. Near the cut, the displaced wax pulled back from the
clip boundary and the cream paper showed through.

It presented as a pale zigzag crack down the middle of a **sealed** envelope, which is
both wrong and, briefly, quite convincing — it looks like a crack, which is why it
survived a first look. An undisplaced disc drawn underneath fixes it.

**The rule:** `clip-path` on a filtered group cuts the filter's result, not its input. If
the filter moves pixels, budget for the edge.

### `transform-style: preserve-3d` makes an element sort by depth, not by `z-index`

The doors vanished entirely before they opened. The shell holding them needs
`preserve-3d` so the 3D chain reaches each door — one flattening ancestor anywhere in the
chain is enough to kill the foreshortening — but `preserve-3d` also makes that element a
stacking context **and** makes it sort its children by their position in space rather
than by `z-index`. The card's `z-index: 2` then beat the entire shell, and the doors were
simply not drawn.

Coplanar children fall back to document order, which is all this ever needed: back panel,
card, doors. Every `z-index` inside the shell was deleted.

The same family of mistake, one layer down: `.std-card` was not a stacking context, so
`::before`'s `z-index: 1` escaped into the *stage's* context and painted the card's inner
rule on top of the sealed envelope. `isolation: isolate` contains it.

**The rule:** `z-index` is only meaningful relative to a stacking context, and several
innocuous-looking properties create one — `transform`, `opacity`, `filter`,
`preserve-3d`, `isolation`. When something paints in the wrong order, find the nearest
ancestor that creates a context before adjusting numbers.

### The test measured the right thing at the wrong width

A door hinged on its outer edge and turned past vertical projects **outward**. At 115°
its far edge lands about 80px beyond the hinge; on a 390px phone the envelope is already
382px wide, so both doors swung into `.std-scene`'s `overflow: hidden` and were clipped
away. The reveal played as envelope, nothing, card — with the teal liner, the entire
reason for the rebuild, invisible on the width most of the guest list will use.

The test that should have caught it ran at 1440px only, where there is room to spare. It
passed. The fix was a camera pull-back; the test is now parameterised over both widths,
like almost everything else in that file already was.

**The rule:** a viewport-dependent defect needs a viewport-parameterised test. When a
suite has a `[phone, desktop]` parameterisation available and a new test does not use it,
that is a decision, and it should be a deliberate one.

### And two of the new tests were themselves wrong

Both probes reported a bare card on an envelope that was sealed and perfectly opaque,
because **`document.elementsFromPoint` skips `pointer-events: none` elements** exactly as
`elementFromPoint` does — and the envelope is `pointer-events: none`, correctly, so it
cannot eat taps meant for the card's link. The instrument could not see the thing it was
pointed at.

The probes now lift `pointer-events` for the measurement and put it back. That is not
loosening the assertion: `pointer-events` has nothing to do with paint order, which is
the property under test, and whether the finished page eats taps is asserted separately.

**The rule, and it is the oldest one in this file:** a test that fails is information, but
so is a test that fails *in a way that contradicts what you can see on the screen*. The
screenshot said the envelope was closed. The test said the card was visible. One of them
was measuring wrong, and it was not the screenshot.

---

## 11. The card's button pointed at the wrong environment, 2026-09-18

### A URL that differs per environment is configuration, even when it looks like content

The save-the-date card's only call to action was a literal in the router:
`WEDDING_SITE_URL = "https://wedding.tapphouse.co/"`. It sat directly beneath the
couple's names, the date and the place, which really are hard-coded on purpose — this
page belongs to no event row, and the module has a docstring explaining why.

That company is what hid it. **The couple and the date are the same wedding on every
deployment; the link out is not.** One of those four constants crossed an environment
boundary and the other three did not, and nothing about the way they were written said
so. The card on `dev-savethedate` therefore sent every reviewer into production.

The tell was available and nobody looked for it: a value is per-environment exactly when
you can name two environments that need different ones. That question takes a second and
would have caught this at the moment the constant was written.

### The fix that adds a setting is usually worse than the fix that finds one

The obvious repair is a new `WEDDING_SITE_URL` environment variable, set in
`docker-compose.prod.yml` and in `scripts/review-instance.sh`. It would have worked, and
it would have left two variables that must agree — `PUBLIC_BASE_URL` and this one — with
nothing keeping them in step.

`PUBLIC_BASE_URL` already *is* the deployment's own wedding site: it builds every invite
link, and production had set it correctly since the production stack was built. Deriving
the button from it meant production was fixed by deleting the literal, with no
production change to review, deploy, or get wrong.

**Before adding a setting, check whether the value you want is a restatement of one you
already have.** Two variables that must agree are a future bug with a date on it.

### Production's hostname is a suffix of dev's, so `in` is the wrong operator

The new test asserts that a dev-configured card links nowhere near production. Written
the obvious way — `"wedding.tapphouse.co" in href` — it fails on
`https://dev-wedding.tapphouse.co/`, because the production hostname is a **suffix** of
the review instance's. The test would have called the correct link a leak.

It compares `urlparse(href).hostname` instead. This is the same trap, inverted, that
`SAVE_THE_DATE_HOSTS` is an explicit list to avoid: there, a substring match would have
served the card on the wedding hostname. **This project's four hostnames are substrings
of one another by construction, and any check that touches them belongs on parsed
hostnames.**

### Verify the deployment, not the test client

The gate went green before the review instance had been reloaded, and a green gate is
not a fixed button — the running process had imported the old module, which is a trap
this project has already paid for once. The check that ended this was
`curl -H 'Host: dev-savethedate.tapphouse.co'` against the live instance and then the
same request through the tunnel, followed by confirming the destination answers 200.

**"The tests pass" and "the thing Bill is looking at is fixed" are different claims**,
and on a project with a long-lived review instance they come apart routinely.
