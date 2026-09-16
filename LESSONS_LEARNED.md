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
