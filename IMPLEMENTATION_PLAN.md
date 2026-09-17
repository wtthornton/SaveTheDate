# SaveTheDate — Implementation Plan

A build plan for the wedding site of Bill Thornton & Lisa Gorden, Port Aransas, Texas,
Sunday 13 February 2028.

This document is written to be handed to a fresh Claude Code session. It says what to
build, in what order, and how to set up the repo's Claude harness so the work stays
disciplined without burning the token pool.

---

## 1. Where this stands

> **Updated 2026-09-17.** Phases 0-3 are done: 11 of 15 issues closed, 199 tests.
> See §10 for what changed and `LESSONS_LEARNED.md` for the things that cost time.

**Shipped.** A FastAPI + PostgreSQL API with events, invitations, token-based invite
links and RSVPs. Alembic migrations apply and roll back. CI runs ruff, `mypy --strict`
and pytest against a real Postgres and is green.

**Shipped 2026-09-16.** The `.claude/` harness (§3), and **TAP-7739** — per-person
`attendees`, a six-row `segments` schedule, an `attendance` join, `rsvp_opens_at` and a
`timezone` on `events`, and a thin `rsvps`. Merged to `main` and pushed.

**Shipped 2026-09-16.** **TAP-7728** — the guest pages. `/invites/{token}` serves HTML
and the JSON view moved to `/api/invites/{token}`; the token is unchanged. Welcome, The
Wedding, RSVP, a print view and a 404, all anonymous and `noindex`. Per-person RSVP form
honoring the three phases, folding into `<details>` at three seats or more. Tailwind
v4.3.3 standalone with the built CSS committed; htmx 2.0.10 vendored and version-asserted.
**Merged to `main` and pushed.** Desktop layouts followed from the three 1440px
artboards, which the first pass missed entirely. 74 tests, including a browser-driven
visual suite; each assertion confirmed by deliberate mutation.

**Designed.** The Design canvas still specifies 15–17px body text, which contradicts the
18px floor the build now enforces. **The canvas is the one that is wrong** and should be
updated before it is used as a reference again.

**Shipped 2026-09-17.** The whole of M1 and M3, plus the rest of M2.
**TAP-7729** — the RSVP window is judged in the event's own zone and a naive deadline is
refused. **TAP-7725** — `hosts`, server-side sessions, argon2id, registration closed
unless a bootstrap token is set. **TAP-7726** — `events.host_id`; another host's event
answers 404, not 403. **TAP-7727** — a sliding-window throttle on the public guest
routes, running before the token lookup. **TAP-7730** — the host dashboard: per-day
headcounts, children separately, a dietary rollup, the guest list, CSV export, and an
HTML login. **TAP-7732** — CSV import, all-or-nothing. **TAP-7731** — email delivery
behind a fake-able transport, with per-guest delivery state and a bounce webhook.

**Not started.** Only M4: hosting (TAP-7733) and observability (TAP-7734). Both need
Bill's accounts, which is why they stop here.

**The immovable fact.** The wedding is **Sunday 13 February 2028 at 3pm**. Invitations
go out ~6–8 weeks ahead; save-the-dates 6–12 months ahead. RSVP deadline 15 December
2027. Work backwards from those, not from today.

| | |
| --- | --- |
| Repo | https://github.com/wtthornton/SaveTheDate |
| Linear | Project **SaveTheDate**, team TappsCodingAgents (TAP) |
| Backlog | TAP-7725 … TAP-7763, 15 issues, 4 milestones. **11 done as of 2026-09-17**; TAP-7733, 7734, 7740 and 7762 remain |
| Postgres | host port **5434** (5432/5433 are taken by other local projects) |
| Hosting | **The home lab.** One Compose stack behind a named Cloudflare Tunnel. See §8.1. |
| Hostnames | **`tapphouse.co`**, on Cloudflare since 2026-09-17. `dev-wedding` live, `wedding` and `savethedate` reserved. See §7.1. |
| Claude Code | **2.1.258** installed. Feature notes below were checked against 2.1.271+ docs, so verify anything exotic before relying on it. |

---

## 2. Build order

Linear numbers M1 (security) first, but the **goal that matters next is getting the
invite page in front of Lisa and family.** That argues for schema-and-page first, which
is safe *only* because the review instance runs on invented guests.

> **Standing rule for Phase 1: fake guest data only.** Host endpoints are
> unauthenticated until Phase 2. `GET /events/{id}/guests` returns every invite token to
> anyone with the URL. Real names do not touch the box until TAP-7725 lands.

### Phase 0 — Harness (half a day) — **DONE 2026-09-16**
`.claude/` exists. Section 3, with the corrections noted there.

### Phase 1 — The thing people see (M2)
| Issue | Why it is here |
| --- | --- |
| ~~**TAP-7739** Schema redesign~~ | **DONE 2026-09-16**, merged. Per-person `attendees`, `segments`, `attendance`, `rsvp_opens_at`, `events.timezone`; meal choice dropped; `rsvps` thinned. |
| ~~**TAP-7728** Guest invite page~~ | **DONE 2026-09-16**, merged. Jinja + htmx 2.x + Tailwind, five routes, 18px floor enforced by a test rather than by review. |
| ~~**TAP-7729** RSVP window~~ | **DONE 2026-09-17.** The window is judged in the event's zone; a bare date is read as a whole local day and a naive datetime is refused outright. The clock is a FastAPI dependency, so boundaries are asserted to the second with no frozen-time library. |
| ~~**TAP-7740** DNS → Cloudflare~~ | **Closed 2026-09-17, not needed.** The guest site gets its own wedding domain on Cloudflare instead, so `nltlabs.ai` is never touched. See §7.1. |
| ~~**TAP-7738** Review instance~~ | **DONE 2026-09-16.** Quick Tunnel off the dev box, so TAP-7740 was never on the path. |
| ~~**TAP-7763** Stale design canvas~~ | **DONE 2026-09-17.** Retitled "original direction" with a banner listing every divergence from the built site. |
| **TAP-7762** Photography | Open, and **not a coding task**. Every image is an openly-licensed placeholder and the hero is still someone else's wedding. |

### Phase 2 — Make it safe (M1) — **DONE 2026-09-17**
`TAP-7725` host auth → `TAP-7726` ownership scoping → `TAP-7727` rate limiting, in that
order. **Real guest data may now exist**, with one caveat worth stating: the review
tunnel is still seeded with invented guests and should stay that way until TAP-7733
gives this a durable home. Losing the guest list has no recovery path.

### Phase 3 — Host tooling (M3) — **DONE 2026-09-17**
`TAP-7730` dashboard → `TAP-7732` CSV import → `TAP-7731` email delivery.

TAP-7730's scope was corrected before it was built: it asked for "counts per meal
option", which TAP-7739 had already removed from the schema. See §10.

### Phase 4 — Launch readiness (M4)
`TAP-7733` home-lab deployment + tested restore → `TAP-7734` observability.

**Deadline-driven checkpoints**

- ~~**By mid-2027** — Phases 0–2 done, real guest list loadable.~~ **Met 2026-09-17**,
  about nine months early.
- ~~**By Oct 2027** — Phase 3 done.~~ **Met 2026-09-17.** Invitations can be sent when
  the hosts choose to; the software is no longer what is waiting.
- **15 Dec 2027** — RSVP closes. Headcounts must be exportable that day.
- **Feb 2028** — read-only. Change nothing in the fortnight before.

---

## 3. The Claude harness for this repo

`.claude/` now exists (built 2026-09-16). What follows is the intended design plus the
corrections found while building it — read the callouts, not just the code blocks. The
rule of thumb:

| Need | Use |
| --- | --- |
| A rule that must always hold | **Hook** or permission — deterministic, not persuasion |
| Knowledge needed only sometimes | **Skill** — loads on demand |
| A bounded job worth isolating from main context | **Subagent** |
| A repeated multi-step chore you invoke by name | **Slash command** |
| Always-on project facts | **CLAUDE.md**, kept short |

### 3.1 `.claude/CLAUDE.md`

Keep it under a page. Long CLAUDE.md files get skimmed. It should carry the invariants
that are expensive to rediscover:

```markdown
# SaveTheDate

FastAPI + PostgreSQL. Server-rendered Jinja2 + htmx 2.x + Tailwind. No Node toolchain.

## Commands
- Postgres: `docker compose up -d db`  (host port 5434, not 5432)
- Gate: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && \
         .venv/bin/mypy app migrations scripts tests && .venv/bin/pytest -q`
- Migrations: `.venv/bin/alembic upgrade head` / `downgrade base`
- Seed fake review data: `.venv/bin/python -m scripts.seed_review_data`

## Invariants — do not break these
- `guests.invite_token` is in people's inboxes once sent. NEVER re-key or re-issue a
  guests row. All change is absorbed by tables hanging off it.
- Guest routes stay anonymous. The link IS the credential. Never add a guest login.
- Accessibility is a requirement, not a nicety: 18px minimum body text, 44px touch
  targets, real `<input>`/`<label>`, no `role=` on divs. The guest list skews old.
- htmx is pinned to 2.x. htmx 4 made attribute inheritance explicit and fails SILENTLY.
- No per-plate meal choice. Dietary tags only. Headcounts are per DAY, not per plate.
- Generated Alembic migrations need `ruff check --fix` AND `ruff format`, or CI fails.
- American English. This is a Texas wedding.

## Never
- Swallow exceptions, skip tests, or add `# noqa` / `# type: ignore` to get green.
- Put real guest data on an unauthenticated instance.
```

### 3.2 `.claude/agents/` — subagents

Three, deliberately. Each gets its own context window, so a long migration debug does not
poison the main thread. Keep the roster small: every agent is a context you pay for.

**`.claude/agents/std-schema.md`**
```markdown
---
name: std-schema
description: SQLAlchemy 2.0 + Alembic specialist for SaveTheDate. Use for model
  changes, migration authoring, and verifying up/down round-trips.
tools: Read, Write, Edit, Grep, Glob, Bash
model: sonnet
---
You own the database layer. Rules:
- `events` and `guests` are STABLE. Never alter `guests.invite_token` or re-key the row.
- Every migration must apply AND roll back cleanly. Prove it: `alembic upgrade head`,
  `alembic downgrade base`, `alembic upgrade head`.
- `ruff format` every generated migration before finishing — CI has failed on this.
- Postgres is on host port 5434.
- Report the exact DDL you generated. Never say "should work".
```

**`.claude/agents/std-templates.md`**
```markdown
---
name: std-templates
description: Jinja2 + htmx 2.x + Tailwind template work for the guest-facing pages.
tools: Read, Write, Edit, Grep, Glob, Bash
model: sonnet
---
You build the guest site. Non-negotiable:
- htmx 2.x syntax only. Do NOT use htmx 4 idioms; inheritance there is explicit and
  failures are silent.
- 18px minimum body text, 44px minimum touch targets, 200% zoom without breakage.
- Native form controls with real labels. Never `role=` or onClick on a div.
- Text over photography sits on a scrim at 4.5:1 or better.
- Tailwind via the standalone binary. Never add package.json.
- American English.
```

**`.claude/agents/std-review.md`**
```markdown
---
name: std-review
description: Adversarial correctness and security review before merge. Read-only.
tools: Read, Grep, Glob, Bash
# The allowlist alone did NOT hold — the agent registered with Write and Edit anyway.
# This denylist overrides `tools`, so "read-only" is enforced rather than asked for.
disallowedTools: Write, Edit, NotebookEdit
model: opus
skills: [security-review]
memory: project
---
Read-only. You do not fix; you find. Priorities, in order:
1. Anything that leaks an invite token to an unauthenticated caller.
2. Anything that re-keys or re-issues a guests row.
3. Silent failure: swallowed exceptions, skipped tests, suppressed type errors.
4. Correctness bugs with a concrete failing input.
Report each finding as: file:line, the defect, and the exact input that breaks it.
Say "no findings" rather than padding.
```

**Frontmatter fields worth using beyond the three above** (all verified against current
docs): `memory: project` gives an agent persistent project-scoped memory across runs;
`skills: [security-review]` attaches a skill so the agent loads it without being told;
`isolation: worktree` gives it its own git worktree; `permissionMode`, `maxTurns` and
`effort` bound it. For `std-review`, attaching `skills: [security-review]` and
`memory: project` is worth it — it accumulates what it has already flagged.

> **Note a real defect in the existing setup.** `~/.claude/agents/ralph.md` declares
> `Agent(ralph-explorer, ralph-tester, ralph-reviewer, ralph-architect)` but **none of
> those four agents exist on disk.** Ralph's delegation will fail. Either create them or
> strip the line before pointing Ralph at this repo.

### 3.3 `.claude/commands/` — slash commands

**`/std-gate`** — `.claude/commands/std-gate.md`
```markdown
---
description: Run the full quality gate and report failures verbatim.
---
Run each of these in order and show the real output. Do not summarise away a failure.
1. `docker compose up -d db` and wait for healthy
2. `.venv/bin/ruff check .`
3. `.venv/bin/ruff format --check .`
4. `.venv/bin/mypy app migrations tests`
5. `.venv/bin/alembic upgrade head && .venv/bin/alembic downgrade base && \
    .venv/bin/alembic upgrade head`
6. `.venv/bin/pytest -q`
If anything fails, stop and report it. Never fix by suppressing.
```

**`/std-issue`** — `.claude/commands/std-issue.md`
```markdown
---
description: Start work on a Linear issue. Usage: /std-issue TAP-7739
---
For the issue id in $ARGUMENTS:
1. Fetch it from Linear and restate its acceptance criteria in your own words.
2. Name anything in it you think is wrong or underspecified. Do not silently reinterpret.
3. Check its blockers are closed.
4. Create branch from its `gitBranchName`.
5. Write the failing test FIRST (see the tdd-workflow skill), then implement.
6. Finish with /std-gate.
```

### 3.4 `.claude/settings.json` — hooks

Project hooks **merge with** the user-level hooks already configured
(continuous-learning-v2, linear, artifact-render-check). They do not replace them.

The highest-value hook for this repo comes straight from a CI failure it already had —
Alembic's generated migrations do not satisfy ruff.

> **The first version of this hook was wrong, and it is worth understanding why.** It ran
> `ruff format` only. A freshly generated Alembic migration fails `ruff check` with **7
> errors** — `UP035` (`typing.Sequence`), `UP007` (`Union[...]`), and import sorting.
> `ruff format` fixes **none** of them; they are lint rules, not formatting. The hook
> would have run happily on every Bash call while CI kept failing for exactly the
> original reason. It needs `ruff check --fix` *and* `ruff format`. It also uses
> `${CLAUDE_PROJECT_DIR}` rather than relative paths, because a hook's working
> directory is not guaranteed, and does not send stderr to `/dev/null` — a silently
> failing formatter is how the CI failure comes back.

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{
          "type": "command",
          "command": "if [ -d \"${CLAUDE_PROJECT_DIR}/migrations/versions\" ] && [ -x \"${CLAUDE_PROJECT_DIR}/.venv/bin/ruff\" ]; then \"${CLAUDE_PROJECT_DIR}/.venv/bin/ruff\" check --fix -q \"${CLAUDE_PROJECT_DIR}/migrations/versions/\"; \"${CLAUDE_PROJECT_DIR}/.venv/bin/ruff\" format -q \"${CLAUDE_PROJECT_DIR}/migrations/versions/\"; fi"
        }]
      }
    ]
  }
}
```

Add a second only if it earns its keep: formatting `app/**.py` on Write/Edit. Resist
more. Hooks run on every matching call and slow everything down.

**Exit codes are the mechanism, and they matter.** A hook returning `0` allows the call,
`1` warns, and **`2` blocks it and feeds the reason back to Claude.** That is how you
turn a rule into an actual gate rather than a suggestion — e.g. a `PreToolUse` hook on
`Bash` that exits 2 if the command would `git push` while the gate is red. Use this
sparingly; a blocking hook that misfires is infuriating.

### 3.5 Skills already on this machine

Do not rebuild these — they exist at `~/.claude/skills/`:

| Skill | Use it for |
| --- | --- |
| `tdd-workflow` | Failing test first. Use on every behavioural change. |
| `search-first` | Before writing any new helper or utility. |
| `security-review` | **Mandatory** for TAP-7725/7726/7727. |
| `simplify` | At each milestone boundary. Removes, never adds. |
| `python-patterns` | Idiom and typing questions. |
| `linear` | Issue reads and writes. |
| `ralph-runner` | Only if running the autonomous loop — see §5. |

---

## 4. Where Workflows earn their cost — and where they do not

**Default: do not use the Workflow tool on this project.** The token pool is shared
across several concurrent sessions, and a Workflow fans out a dozen agents. One opus
verifier has measured ~124k tokens. This project is a single-developer build with a
13-issue backlog; plain subagents and slash commands cover it.

**Three places a Workflow is genuinely worth it.** Each is a fan-out over independent
items where the results are wanted together:

1. **Pre-launch audit (before Phase 4 ships).** Review the whole codebase across
   independent dimensions — token leakage, accessibility conformance, migration
   safety, error handling — then verify each finding adversarially. Genuinely parallel,
   genuinely worth the spend, and happens once.
2. **Accessibility sweep across all templates** once Phase 1 is complete. One agent per
   template, same checklist, results compared.
3. **Cross-browser/device render check** of the finished guest page, if that ever gets
   automated.

Everything else — per-issue work, reviews, migrations — is sequential and belongs in the
main thread or a single subagent.

**When you do run one:**
- Only one session drives a given Workflow. Never launch a parallel one "to be safe".
- Route by cost: mechanical checks (grep, diff, test runs) → Haiku or plain Bash;
  scope and identity reads → Sonnet; Opus only for a genuinely adversarial refutation.
- Route by **permission** too. `Explore` is read-only and will refuse to mutate
  anything. Any brief that needs a scratch repo, a deliberate break, or a negative
  control must go to `general-purpose`, or you pay twice for a non-answer.
- **A user interrupt kills in-flight workflow subagents silently.** Transcripts just
  stop growing. After any interrupt, check whether background work survived before
  reporting progress. Recover with `Workflow({scriptPath, resumeFromRunId})` — cached
  results replay instantly and only the killed calls re-run.

---

## 5. Teams, multi-session, and Ralph

**Two different things share the word "team", and only one of them works today.**

*Agent Teams* — a lead plus named teammates with a shared task list and mailbox — is
**experimental and disabled by default.** It needs `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`
in settings `env`, and it ships with real limitations: in-process teammates cannot be
resumed, task-list updates lag, shutdown is slow, and teams cannot nest. Not worth
adopting for this project.

*Cross-session messaging* works now and needs no flag. `ListAgents` shows other live
Claude sessions on this machine; `SendMessage` talks to them. Useful here in one case: a
long unattended job (a Phase 4 deploy, a bulk import run) in one session while design or
review continues in another.

Two rules learned the hard way:
- **One session owns the repo index at a time.** Two sessions writing the same working
  tree corrupts both. If a second session is working, it gets a git worktree
  (`isolation: "worktree"` on the Agent tool) or it gets read-only work.
- A peer session cannot grant permissions. If a session was denied an action, routing it
  through another session is permission laundering — surface it to Bill instead.

**Ralph.** `ralph-runner` can chew through the Linear backlog autonomously. Honest
assessment: **not yet for this project.** Ralph's own agent file references four
subagents that do not exist (§3.2), and the first three issues (TAP-7739 schema,
TAP-7728 templates) involve design judgement that wants a human in the loop. Revisit for
Phase 3, where the work is more mechanical — CSV import, dashboard queries, email
plumbing — and the schema is settled.

---

## 6. Definition of done

A change is not done until all of these hold. No exceptions, no suppressions.

- [ ] `ruff check`, `ruff format --check`, `mypy --strict` all clean
- [ ] `pytest` green against a real Postgres
- [ ] Migrations apply **and** roll back
- [ ] No invite token reachable by an unauthenticated caller
- [ ] No `guests` row re-keyed
- [ ] Guest-facing pages: 18px body, 44px targets, native controls, 4.5:1 contrast
- [ ] The Linear issue's own "Done when" list is satisfied, ticked explicitly
- [ ] American English throughout

---

## 7. Risks

| Risk | Mitigation |
| --- | --- |
| Real guest data on the unauthenticated review box | Fake data until TAP-7725. Stated on TAP-7738. |
| **DNS move breaks live company EMAIL, not just the website** | Avoided entirely: the guest site gets its own domain, and `nltlabs.ai` is never migrated. See §7.1. |
| htmx 4 idioms in 2.x templates, failing silently | Pin in CLAUDE.md and in the std-templates agent. Assert the version in the base template. |
| Invite tokens already mailed, then the schema changes | Never re-key `guests`. The whole schema design hangs off this. |
| **A home-lab outage during the RSVP window** | Now the project's largest operational risk, and it is ours rather than a vendor's: power, internet and unattended restart. TAP-7733 owns it. Worst case the RSVP form is unreachable for hours while guests are trying to reply. |
| Token pool exhausted by fan-outs | §4. Workflows are the exception, not the tool. |
| Losing the guest list | Automated Postgres backups **off the machine**, with a restore that has actually been performed. TAP-7733's highest-value line, and more important self-hosted than it would have been managed. |

### 7.1 The guest site gets its own domain, and `nltlabs.ai` is never touched

**Decided 2026-09-17, and it closes TAP-7740.**

A named Cloudflare Tunnel — the thing that gives the home lab a permanent public
hostname — requires its zone to be on Cloudflare nameservers. Cloudflare's partial
(CNAME) setup would let a zone stay at GoDaddy while proxying selected hostnames, but
that is **Business-plan only**, around $200/month, which is absurd for this.

So `invite.nltlabs.ai` would mean migrating the `nltlabs.ai` zone. That zone was checked
live on 2026-09-16 and 2026-09-17 and carries considerably more than a website: a full
Microsoft 365 mail deployment behind a filtering provider, the client-autoconfiguration
and Teams records that go with it, and at least six hostnames serving real traffic.
`nltlabs.com` is a separate zone on different nameservers with its own mail.

**Mail is the part that bites.** A broken website is obvious within seconds. Misrouted
mail can be invisible for hours, is not recoverable, and the zone's DMARC policy is
`quarantine` — so authentication failures are silently held rather than bounced, and
nobody gets a warning.

> **The record-level inventory lives on TAP-7740 in Linear, not here.** This repo is
> public, and NLT Labs' mail configuration does not belong in a wedding site's history.

**The answer was a separate domain, and it is done.** `tapphouse.co` — already owned,
already registered — was delegated to Cloudflare on 2026-09-17 and now serves the guest
site through a named tunnel. `nltlabs.ai` was never touched.

**It was not the empty zone this section originally assumed**, and that is the part worth
carrying forward. The pre-flight check found no mail — no MX, SPF, DKIM or DMARC — which
is the category where mistakes are silent. But it also held `home.tapphouse.co`, a live
CNAME to Home Assistant Cloud (Nabu Casa), which no amount of probing from outside would
have revealed; subdomains cannot be enumerated without a zone transfer. Cloudflare's scan
surfaced it, and it was carried across **unproxied** — proxying would put Cloudflare's
certificate in front of a service that issues its own.

Before cutting over, Cloudflare's nameservers were queried directly to confirm the zone
was built correctly, so a missing record would have been caught before the switch rather
than after. The registry, the registrar and Cloudflare were then verified to agree, and
no DS records existed, so DNSSEC could not strand the zone.

If `nltlabs.ai` should move to Cloudflare for its own reasons, that is a separate piece
of work deserving a full record export, a mail-flow test and a rollback window — not a
step in a wedding site's build.

---

## 8. Still unresolved

Carry these into the next session:

1. **Registry page** — standard on wedding sites, not in the backlog. Worth having for a
   destination wedding where guests would rather give than fly with a gift.
2. **FAQ page** — also standard. What to wear on a beach in February, kids or no kids,
   what happens if it rains.
3. ~~**Prices** for golf and the fishing charter still show `$[ CONFIRM ]`.~~ **Wrong as written** — checked 2026-09-17, the prices were removed rather than stubbed, and no placeholder renders. They are simply unconfirmed and absent.
4. **Airport shuttle** is a marked placeholder on both travel pages.
5. **Photography** — now tracked as **TAP-7762**. Every image is still an openly-licensed
   placeholder and the hero is still someone else's wedding, but no slot is empty: two
   photographs are of Port Aransas itself (the Tarpon Inn porch, the ferry), found by
   searching for the place rather than the subject. Five are CC BY and carry a visible
   credit line — keep `PHOTO_CREDITS` in step with what is actually shown.
6. ~~**Timezone** for the RSVP window is undecided; `events` has no timezone column.~~
   **Resolved in TAP-7739.** `events.timezone` holds an IANA name (`America/Chicago`);
   `rsvp_opens_at` and `rsvp_deadline` are both `timestamptz`. A date deadline means the
   end of that day *in the event's zone*. Postgres `timestamptz` stores UTC and discards
   the zone, which is why the IANA name is kept in its own column.
7. **Registry and FAQ pages** — see items 1 and 2; still not in the backlog. If they are
   wanted, they are template work and belong with TAP-7728.
8. **The design canvas is now stale** — it still specifies the 15–17px type and the
   20px date that measurement showed to be illegible, and predates the desktop
   layouts built from it. Tracked as **TAP-7763**. Do not treat it as current spec
   without checking it against `tests/test_visual.py`.

---

## 8.1 The home lab — what TAP-7733 has to build

**Decided 2026-09-17: everything runs on the home lab.** No managed platform. This
section replaces the Render research that used to sit here; that research is still
correct about NLTWeb's estate, it is simply no longer this project's path.

### The shape of it

One Docker Compose stack: FastAPI behind a reverse proxy, Postgres alongside it,
published through a **named Cloudflare Tunnel**. Outbound-only, so no ports are
forwarded, no static IP is required, and the home address never appears in public DNS.
TLS terminates at Cloudflare's edge.

The software cost is **zero** — every component is open source with no commercial
restriction at this size. What the lab costs is electricity, hardware, offsite backup
storage, and attention.

### Order of work

1. ~~**Register the wedding domain and delegate it to Cloudflare.**~~ **DONE
   2026-09-17.** `tapphouse.co`, nameservers `anuj`/`blair.ns.cloudflare.com`.
2. ~~**A named tunnel.**~~ **DONE 2026-09-17.** Tunnel `tapphouse`
   (`bcf78f41-e048-429a-9375-43cf777173e8`), config at `~/.cloudflared/config.yml`,
   running as the systemd user service `cloudflared-tapphouse` with `Restart=always`
   and lingering on, so it returns unattended after a reboot. Three hostnames routed:
   `dev-wedding` → the review instance on :50681, `wedding` and `savethedate` → 503.
3. **Compose stack** with `restart: unless-stopped`, so the whole thing returns by
   itself after a power cut without anyone logging in. **This is the next piece of
   work**, and it is what `wedding.tapphouse.co` is waiting for — its own Compose
   project and its own database, separate from development.
4. **Migrations as a release step**, not on app boot. Two app instances racing
   `alembic upgrade` on start is a bad way to find out about locking.
5. **Automated backups off this machine**, and a **restore that has actually been
   performed**. See below — this is the deliverable.
6. **Set `TRUSTED_CLIENT_IP_HEADER=CF-Connecting-IP`.** Behind the tunnel every request
   arrives from the tunnel's local end, so without this the whole world shares one
   rate-limit bucket and the first few guests throttle everyone else.
7. **Set `SESSION_COOKIE_SECURE`** (or a `https://` `PUBLIC_BASE_URL`, which it follows).

### The backup is not the deliverable; the restore is

Losing the guest list is the one failure here with no recovery path, and the date cannot
move. Self-hosting makes this ours rather than a vendor's, which is the real cost of the
decision — not dollars.

- `pg_dump` on a timer is fine at this size. The database is a few megabytes.
- **Off the machine.** A backup on the same disk as the database is not a backup. A few
  GB of dumps on object storage is pennies a month.
- **Restore into a scratch database and count the guests.** A backup nobody has restored
  is a hypothesis. TAP-7733 is not done until one has been restored and verified.
- Keep enough history to survive a mistake discovered late — a bad migration noticed a
  week later is the realistic case, not a disk dying.

### What self-hosting puts on the critical path

These were somebody else's problem under a managed host and are now ours. They belong in
TAP-7733 rather than being discovered during the RSVP window:

- **Power.** A UPS, and unattended restart after it runs out.
- **Internet.** The RSVP window is months long and the household will be in Texas for
  part of it. What happens if the line drops while nobody is home?
- **Unattended recovery.** Nobody should need to SSH in for the site to come back.
- **Somebody else knowing how.** If the one person who understands the stack is at their
  own wedding, the runbook has to be followable by someone else.

### The one thing that cannot come home

**Outbound email.** Transactional mail cannot be self-hosted reliably — residential
address space is blocklisted, and SPF, DKIM and DMARC will not rescue deliverability
from it. `app/mail.py` keeps the provider behind a Protocol for exactly this reason. A
hundred invitations sits inside a free tier, and the default transport prints to the
console, so nothing is sent until it is deliberately configured.

### Carried over from the Render research, because it is still true

- **The built `app/static/app.css` stays committed.** It means no deploy needs the
  110MB Tailwind binary present. `tests/test_stylesheet.py` fails if it goes stale.
- **`cloudflared` is installed** at `~/.local/bin/cloudflared`.
- **Never put a credential in this repo.** It is public. Record where a secret lives,
  never its value.

## 9. The second session of 2026-09-16

The first session's record is in §9.1. This one merged everything, put the site in front
of a reviewer, and then spent most of its time on things the tests could not see.

**Shipped and merged to `main`**

- **TAP-7728** — the guest pages. `/invites/{token}` serves HTML; the JSON view moved to
  `/api/invites/{token}` and the token itself is unchanged. Welcome, The Wedding, RSVP, a
  print view and a 404, all anonymous and `noindex`. Per-person RSVP form honouring the
  three phases, folding into `<details>` at three seats or more.
- **TAP-7738** — a Cloudflare Quick Tunnel review instance, `scripts/review-instance.sh`.
  No DNS change, so TAP-7740 stays untouched. Draft notice, `robots.txt` deny,
  `X-Robots-Tag` on every response.
- **Desktop layouts** for all three pages, from the three 1440px artboards.
- **A browser-driven visual suite**, `tests/test_visual.py`. 74 tests in total.
- **Ten photographs**, two of Port Aransas itself.

**What this session got wrong, because the pattern matters more than the list**

Four things shipped looking finished and were caught by a person, not by the gate: an
empty grey box where the hero should be; a whole missing breakpoint; a hero date that
measured smaller than body text; and grey rectangles where pictures belonged. Every one
was green at the time.

The tests written to prevent each of those then failed in the same way. The
narrow-column test visited one of three pages. The 18px floor exempted any class
containing "eyebrow" — a loophole that was then used, with approval, by renaming a
caption. A duplicate-name test scanned `h1, h2` and passed against a page plainly
showing the name twice.

The rule that came out of it is in `LESSONS_LEARNED.md` and is worth carrying: **a new
test must be watched failing against the actual broken thing**, not merely against a
mutation, and not in principle.

**Two traps with teeth, both live**

- `/std-gate`'s migration round trip resolved `DATABASE_URL` to the **dev** database and
  dropped every table — wiping the running review instance and re-keying five published
  invite links a minute after they were verified working. Now pinned to
  `TEST_DATABASE_URL`.
- Jinja reloads templates but imports Python once, so a change under `app/*.py` left the
  public URL serving a 500 while the suite was green. Use
  `scripts/review-instance.sh reload`, which keeps the URL and the tokens.

**Open, and filed**

- **TAP-7729** — narrowed to its two real gaps: `EventCreate` accepts a naive
  `rsvp_deadline` that Postgres reads in the server's zone, and there are no
  frozen-time tests.
- **TAP-7762** — the photography is all placeholder. The hero is someone else's wedding.
- **TAP-7763** — the design canvas is now stale and is the document somebody will believe.

**Standing facts for the next session**

- Tailwind v4.3.3 and cloudflared 2026.9.1 are installed at `~/.local/bin/`, outside the
  repo. The built `app/static/app.css` is committed on purpose.
- The review instance's URL is random and dies with the box. `up` prints a new one and
  re-keys every token; `reload` does not.
- Five photographs are CC BY and their credits render from `PHOTO_CREDITS`. Keep that
  list in step with what is actually shown.

## 9.1. What changed earlier on 2026-09-16

Recorded so a later session does not re-derive it. The reasoning behind each is in
`LESSONS_LEARNED.md`.

**Shipped**

- Phase 0: `.claude/` — CLAUDE.md, three subagents, two slash commands, the hook.
- TAP-7739 on branch `tap-7739-schema-redesign-…`, gate green, **unmerged**.

**Corrections to this plan**

1. **The §3.4 hook did not do its job.** `ruff format` alone cannot fix what fails
   `ruff check` on a generated migration. Fixed in §3.4.
2. **`tools:` on a subagent is not a hard allowlist.** `std-review` declared
   `tools: Read, Grep, Glob, Bash` and registered *with `Write` and `Edit`*. Its whole
   contract is read-only. `disallowedTools` overrides `tools`; §3.2 now uses it. This is
   the plan's own rule — a rule that must always hold needs a mechanism, not prose.
3. **TAP-7740's risk was understated.** The zone carries live Microsoft 365 mail behind
   Proofpoint. New §7.1.
4. **TAP-7729 is mostly already built**, because TAP-7739's own "Done when" required
   both ends of the RSVP window.

**Changes to the repo's own gates**

- CI ran `alembic upgrade head` only, while the definition of done in §6 requires roll
  back. CI now runs the full round trip.
- `mypy` now covers `scripts` as well as `app migrations tests`.
- The test suite builds its schema with **Alembic, not `create_all`** — the TAP-7739
  consistency trigger is invisible to SQLAlchemy metadata, so `create_all` would have
  given the suite a schema missing the invariant it exists to prove.

**Harness notes for the next session**

- Files written to `.claude/` mid-session register after a short delay. The very first
  `/std-issue` call failed with "Unknown skill"; it worked minutes later.
- `continuous-learning-v2` is **already installed** at user level (`PreToolUse` and
  `PostToolUse`, matcher `*`) and registered this project as `2ec864647abf` on
  2026-09-16. Nothing to install. It had extracted zero instincts as of that date.

---

## 10. The session of 2026-09-17

Blocks A, B and C of the plan agreed at the start of the session: everything that is
pure code, stopping where Bill's accounts become necessary. Eight issues closed, 74
tests to 199, gate green on every commit.

**Shipped**

| Issue | What landed |
| --- | --- |
| **TAP-7763** | The design canvas is retitled *original direction* and carries a banner listing every place it disagrees with the built site, and why the site is right. |
| **TAP-7729** | The RSVP window is judged in the event's own zone. A bare `2027-12-15` is read as that whole day locally; a naive datetime is refused rather than converted, because it looks precise while carrying no zone. The clock is a dependency, so boundaries are asserted to the second. |
| **TAP-7725** | `hosts` and server-side `host_sessions`. argon2id for passwords, sha256 of the cookie for sessions. Registration is closed unless a bootstrap token is configured. Guest routes stay anonymous, with a test that walks all five of them holding no cookie. |
| **TAP-7726** | `events.host_id`, NOT NULL, RESTRICT rather than CASCADE. Another host's event answers **404, not 403**. The backfill invents an un-loggable-into placeholder host when a database has events but none, and `scripts/adopt_events.py` moves them to a real account. |
| **TAP-7727** | A sliding-window throttle on `/invites/*`, running in middleware **before** the token lookup, so a throttled real token and an invented one are byte-identical. In-process counters, no Redis. |
| **TAP-7730** | The host dashboard: per-day headcounts with children separate, a dietary rollup with notes attributed by name, the guest list with readable invite links, CSV export without tokens, add/rename/withdraw, and an HTML login. |
| **TAP-7732** | CSV import, all-or-nothing, every bad line numbered. Duplicates caught against the file and against the existing list. Handles the BOM Excel writes. |
| **TAP-7731** | Email behind a Protocol with a recording fake, so no test opens a socket. Per-guest delivery state on the dashboard, reminders only to non-responders, and a signed bounce webhook. |

**Also fixed, found by Bill on a phone rather than by the suite**

The ferry photograph under "Coming to the island" took most of a phone screen:
`h-[160px]` and `lg:h-[260px]` were never in the committed `app/static/app.css`, so the
image had no height cap at all. `tests/test_stylesheet.py` now fails when the committed
stylesheet falls behind the templates.

**Also changed**

The couple are named in the traditional order throughout — "Lisa and Bill", with an
`L & B` monogram. The test written for it found nine places, not the three in the
templates: `event.title` and `host_name` live in the database and render into `<title>`.

Guest-facing copy was breaking the American English invariant in two places
("travelling", "licences"). Both are fixed and both are now gated by a test that scans
rendered text.

**Corrections to the backlog**

1. **TAP-7730 contradicted the schema.** It asked for "counts per meal option"; TAP-7739
   had cut meal options and the invariants say "dietary tags only, headcounts per day".
   Corrected in Linear before building, not worked around in code.
2. **TAP-7740 is unnecessary** — though for a different reason than this session gave.
   The argument recorded here was that a managed-host custom domain is a CNAME that
   resolves from GoDaddy. That reasoning died the same day, when the decision was made
   to host everything on the home lab: a *named* tunnel does need its zone on
   Cloudflare, and the partial/CNAME setup that would avoid it is Business-plan only.
   The conclusion survived the reasoning by luck. **Closed 2026-09-17** because the
   guest site gets its own wedding domain instead — see §7.1. Worth remembering that a
   recommendation resting on an assumption should say so, or it outlives the
   assumption.
3. **Plan §8 item 3 was stale.** Golf and charter prices are not shown as `$[ CONFIRM ]`;
   they were removed. Corrected below.

**What is left, and why it stops here**

`TAP-7733` (home-lab hosting) now needs only the production Compose stack and the
backup/restore work; the domain and tunnel landed on 2026-09-17. `TAP-7734`
(observability) wants a Sentry DSN or a self-hosted equivalent.
`TAP-7762` (photography) needs somebody to take photographs. None of the three is
blocked on code.

**Standing facts for the next session**

- Sign in at `/host/login`. There is no registered host yet: set
  `HOST_REGISTRATION_TOKEN`, `POST /auth/register` once, then unset it.
- On a database migrated before any host existed, the events belong to a placeholder
  nobody can sign in as. `python -m scripts.adopt_events --to you@example.com` moves
  them, and `--dry-run` shows what it would do.
- `EMAIL_PROVIDER` defaults to `console`, which prints. That is deliberate; see
  `LESSONS_LEARNED.md` §6.
- The bounce webhook's signature scheme must be checked against Resend's documentation
  before pointing anything at it. The docstring says so in capitals.

---

## 11. The hosting session of 2026-09-17

Everything moved to the home lab, and the guest site got a real hostname.

**The domain**

`tapphouse.co` — already owned, on GoDaddy, carrying a stock GoDaddy Website Builder
template and nothing else of value. Delegated to Cloudflare (`anuj`/`blair.ns.cloudflare.com`)
via the GoDaddy API: `PATCH /v1/domains/{domain}` with the new `nameServers`. `PUT`
returns 404 on that endpoint; `PATCH` is the verb, and it answers 204.

The zone was **not** the empty one §7.1 originally assumed. No mail — verified before and
after — but `home.tapphouse.co` is a live CNAME to Home Assistant Cloud, which probing
from outside could never have found, because subdomains cannot be enumerated without a
zone transfer. It was carried across unproxied and still resolves.

Before the cutover, Cloudflare's own nameservers were queried directly to confirm the
zone was built correctly, and the registry was checked for DS records — a stale DS with
new nameservers is how a domain goes completely dark for validating resolvers. There were
none.

**The tunnel**

Named tunnel `tapphouse`, id `bcf78f41-e048-429a-9375-43cf777173e8`, config at
`~/.cloudflared/config.yml`, run by the systemd **user** service `cloudflared-tapphouse`
with `Restart=always`, `StartLimitIntervalSec=0` and lingering enabled — so it comes back
after a reboot or a power cut with nobody logged in. That is a TAP-7733 requirement, not
a convenience.

| Hostname | Serves |
| --- | --- |
| `dev-wedding.tapphouse.co` | The review instance on :50681 — **live** |
| `wedding.tapphouse.co` | Production — **503 on purpose** |
| `savethedate.tapphouse.co` | Production — the save-the-date card. **503 until the production stack exists** |
| `dev-savethedate.tapphouse.co` | The card on the review instance on :50681 — **live** |

`wedding` returns 503 rather than pointing at the development instance. A guest-facing
hostname quietly serving the development database is how invented guests start looking
real, and how a genuine RSVP lands somewhere disposable.

**A bug the new hostname exposed**

Opening the bare hostname returned FastAPI's raw `{"detail":"Not Found"}`. The written
404 has existed since TAP-7728 but was reachable only through a token that parsed and
matched nothing — so nobody typing the domain, or pasting a link that lost its whole
tail, ever saw it. Now handled, with content negotiation so scripts still get JSON and a
prefix list so a host looking at another host's event is not told their *invitation* is
missing.

**Costs, checked rather than assumed**

The software cost of the home lab is zero — every component is open source with no
commercial restriction at this size. What it costs is electricity, hardware, offsite
backup and attention. The one thing that cannot come home is outbound email: residential
address space is blocklisted and SPF/DKIM/DMARC will not rescue it.

**Still open after this session**

- The production Compose stack behind `wedding.tapphouse.co`, with its own database.
- Backups off the machine, and a restore actually performed.
- ~~What `savethedate.tapphouse.co` is for.~~ **Decided 2026-09-17 with Bill, and built:**
  a public, tokenless animated save-the-date card — an envelope that opens itself over a
  drifting Gulf horizon, carrying the couple, the date, Port Aransas and a link onward to
  the wedding site. TAP-7781. Four hostnames, two stacks: `wedding`/`savethedate` in
  production, `dev-wedding`/`dev-savethedate` in development, and dev and production keep
  separate databases. What remains is routing `dev-savethedate` and building the
  production stack — both in TAP-7733.
- `tapphouse.co` expires **2027-08-01**, about six months before the wedding. `renewAuto`
  is on; if the card on file lapses, the guest site's domain goes with it, in the middle
  of the RSVP window.
- `it13` has no IPv6 internet — only ULAs and no default route — so outbound checks from
  the box flap between IPv4 and IPv6. It does not affect guests. Tailscale depends on
  IPv6 ULAs, so nothing may disable IPv6 wholesale.

---

## 12. What this actually is: product bones, one wedding's skin

**Established 2026-09-17 by reading the code**, because nothing in the repository said
so and the README's own subtitle ("a service for weddings and events") implies something
the templates do not deliver. Anyone asking "is this multi-tenant?" had to go and find
out. Now they do not.

### The data layer is genuinely multi-tenant, and it is enforced

Not nominally — tested and constrained:

- `hosts`, and `events.host_id` NOT NULL with a foreign key and `RESTRICT`.
- **Four ownership-scoped queries** across `routers/events.py` and `routers/host.py`.
  Every host-facing read filters `Event.host_id == host.id`; the filter is in the WHERE
  clause, so no path loads another host's row and then decides what to do with it.
- **Nine tests** in `tests/test_event_ownership.py`, including that another host's event
  answers **404 rather than 403**, so ids are not enumerable.
- `/host` lists events and auto-redirects only when there is exactly one. Written for
  the many case.
- `POST /events` creates arbitrary events with unique slugs.

### The presentation layer is one couple's wedding

Exactly **seven values** reach the templates from the database:

| From | Fields |
| --- | --- |
| `event` | `title`, `host_name`, `event_date`, `location`, `id` |
| `guest` | `name`, `party_size` |

Everything else is hard-coded: the story about Lisa's family and Jason's house, Port
Aransas and Mustang Island, the `L & B` monogram in three templates, `segment_image()`
matching on Port Aransas keywords (*ferry*, *golf*, *fishing/charter/bay*),
`PHOTO_CREDITS` as a fixed list of ten photographs, and every measured type size and
scrim ratio in `tests/test_visual.py`.

### Why that is the right place to be

The **expensive-to-change** parts — schema, authentication, ownership scoping, the RSVP
phase logic — are general. The **cheap-to-change** parts — copy, photographs, one
monogram — are specific. A second wedding would mean forking templates or adding a
content model. It would not mean redoing migrations or auth.

It happened honestly rather than by plan: TAP-7739, TAP-7725 and TAP-7726 were written
as product-shaped issues; TAP-7728 built one couple's wedding.

**Do not "fix" this by generalising the templates.** There is no second wedding, and
building for a customer who does not exist is how a four-page site acquires a CMS.

### What it means in practice

- This deployment serves **one** wedding, on **their own** domain, so guest-facing pages
  may name Lisa and Bill directly. That is not a multi-tenancy leak.
- If a second event is ever wanted, the cheapest honest route is a **second deployment**
  — its own Compose project, its own database, its own templates — not a content model.
  The tunnel already supports more hostnames; `savethedate.tapphouse.co` is routed and
  could be exactly that.

