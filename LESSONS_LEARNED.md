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

Researched 2026-09-16 across `~/code`, chiefly `NLTWeb`. Full detail in plan §7.1 and
§8.1.

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

### Render does not install your dependencies

NLTWeb learned this in production: a new `import` broke the live site while GitHub
Actions stayed green, because CI runs `uv sync` and Render did not. **The build command
must install dependencies itself** (`uv sync --frozen && …`). Carry this into TAP-7733.

The corollary for this repo's front end: the Tailwind v4 standalone binary lives at
`~/.local/bin/tailwindcss`, **outside the repo** (it is 110MB and this repo is public),
and the built `app/static/app.css` is **committed**. That is deliberate — it means a
deploy never has to run the build, so the NLTWeb failure cannot repeat here. The cost
is remembering to rebuild and commit after editing the source; the gate catches it,
because the 18px test reads the built file rather than the source.

Related, from the same repo: `render.yaml` is **documentation, not a control surface** in
this account — there are no Blueprints and every service is dashboard-managed, so editing
the file changes nothing about a live deploy. NLTWeb keeps a drift checker to stop the
file lying. Do not assume committed infrastructure YAML is what is running.

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

### Say what you cannot verify, rather than guessing at it

The bounce webhook verifies HMAC-SHA256 over the raw body in constant time. Resend
actually signs through Svix, whose signed payload is `{id}.{timestamp}.{body}` — not the
bare body — and that cannot be confirmed without an account. The function is the right
shape and the right comparison, the bounce path is real and tested, and the docstring
says in capitals that the exact payload must be checked against the provider's
documentation before pointing anything at it.

That is better than either a stub or a guess dressed up as an implementation.
