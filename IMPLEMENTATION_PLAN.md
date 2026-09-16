# SaveTheDate — Implementation Plan

A build plan for the wedding site of Bill Thornton & Lisa Gorden, Port Aransas, Texas,
Sunday 13 February 2028.

This document is written to be handed to a fresh Claude Code session. It says what to
build, in what order, and how to set up the repo's Claude harness so the work stays
disciplined without burning the token pool.

---

## 1. Where this stands

**Shipped.** A FastAPI + PostgreSQL API with events, invitations, token-based invite
links and RSVPs. Alembic migrations apply and roll back. CI runs ruff, `mypy --strict`
and pytest against a real Postgres and is green.

**Designed, not built.** Three-page guest site (Welcome → The Wedding → RSVP), mobile
and web, in a Design canvas. Direction settled: server-rendered Jinja2 + htmx 2.x +
Tailwind, no Node toolchain.

**Not started.** Every template, host authentication, and the schema the real event
needs.

**The immovable fact.** The wedding is **Sunday 13 February 2028 at 3pm**. Invitations
go out ~6–8 weeks ahead; save-the-dates 6–12 months ahead. RSVP deadline 15 December
2027. Work backwards from those, not from today.

| | |
| --- | --- |
| Repo | https://github.com/wtthornton/SaveTheDate |
| Linear | Project **SaveTheDate**, team TappsCodingAgents (TAP) |
| Backlog | TAP-7725 … TAP-7740, 13 issues, 4 milestones |
| Postgres | host port **5434** (5432/5433 are taken by other local projects) |
| Hostnames | `invite.nltlabs.ai` (live), `invite-review.nltlabs.ai` (review) |
| Claude Code | **2.1.258** installed. Feature notes below were checked against 2.1.271+ docs, so verify anything exotic before relying on it. |

---

## 2. Build order

Linear numbers M1 (security) first, but the **goal that matters next is getting the
invite page in front of Lisa and family.** That argues for schema-and-page first, which
is safe *only* because the review instance runs on invented guests.

> **Standing rule for Phase 1: fake guest data only.** Host endpoints are
> unauthenticated until Phase 2. `GET /events/{id}/guests` returns every invite token to
> anyone with the URL. Real names do not touch the box until TAP-7725 lands.

### Phase 0 — Harness (half a day)
Set up `.claude/` for this repo. Section 3. Do this first; everything after is cheaper.

### Phase 1 — The thing people see (M2)
| Issue | Why it is here |
| --- | --- |
| **TAP-7739** Schema redesign | Blocks the form's shape. Per-person attendees, drop meal choice, add `rsvp_opens_at`, add day-segments. Do it before building the form twice. |
| **TAP-7728** Guest invite page | Jinja + htmx 2.x + Tailwind. Three pages. 18px body, 44px targets, native controls. |
| **TAP-7729** RSVP window | Enforce both ends: `rsvp_opens_at` and `rsvp_deadline`. |
| **TAP-7740** DNS → Cloudflare | Blocks TAP-7738. **Touches the live NLT Labs site — needs Bill's explicit go-ahead.** Quick Tunnel is the no-risk fallback. |
| **TAP-7738** Review instance | Cloudflare Tunnel off the dev box. Fake data, `noindex`, visibly a draft. |

### Phase 2 — Make it safe (M1)
`TAP-7725` host auth → `TAP-7726` ownership scoping (blocked by 7725) → `TAP-7727`
rate-limit invite lookups. **Only after this may real guest data exist anywhere.**

### Phase 3 — Host tooling (M3)
`TAP-7730` dashboard and per-day headcounts → `TAP-7732` CSV import → `TAP-7731` email
delivery and reminders.

### Phase 4 — Launch readiness (M4)
`TAP-7733` managed hosting + tested restore → `TAP-7734` observability.

**Deadline-driven checkpoints**

- **By mid-2027** — Phases 0–2 done, real guest list loadable, save-the-dates can go out.
- **By Oct 2027** — Phase 3 done, invitations sent, RSVP opens.
- **15 Dec 2027** — RSVP closes. Headcounts must be exportable that day.
- **Feb 2028** — read-only. Change nothing in the fortnight before.

---

## 3. The Claude harness for this repo

There is no `.claude/` directory yet. Create one. The rule of thumb:

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
         .venv/bin/mypy app migrations tests && .venv/bin/pytest -q`
- Migrations: `.venv/bin/alembic upgrade head` / `downgrade base`

## Invariants — do not break these
- `guests.invite_token` is in people's inboxes once sent. NEVER re-key or re-issue a
  guests row. All change is absorbed by tables hanging off it.
- Guest routes stay anonymous. The link IS the credential. Never add a guest login.
- Accessibility is a requirement, not a nicety: 18px minimum body text, 44px touch
  targets, real `<input>`/`<label>`, no `role=` on divs. The guest list skews old.
- htmx is pinned to 2.x. htmx 4 made attribute inheritance explicit and fails SILENTLY.
- No per-plate meal choice. Dietary tags only. Headcounts are per DAY, not per plate.
- Generated Alembic migrations must be `ruff format`ed or CI fails.
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
model: opus
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
Alembic's generated migrations do not satisfy ruff:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{
          "type": "command",
          "command": "if [ -d migrations/versions ]; then .venv/bin/ruff format -q migrations/versions/ 2>/dev/null; fi"
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
| DNS move breaks the live NLT Labs site | Separate issue (TAP-7740), own change window, Bill's explicit go-ahead. Quick Tunnel fallback needs no DNS change. |
| htmx 4 idioms in 2.x templates, failing silently | Pin in CLAUDE.md and in the std-templates agent. Assert the version in the base template. |
| Invite tokens already mailed, then the schema changes | Never re-key `guests`. The whole schema design hangs off this. |
| A dev-box outage during the RSVP window | Phase 4 moves to managed hosting before real invitations go out. |
| Token pool exhausted by fan-outs | §4. Workflows are the exception, not the tool. |
| Losing the guest list | Managed Postgres with a **tested** restore — TAP-7733's highest-value line. |

---

## 8. Still unresolved

Carry these into the next session:

1. **Registry page** — standard on wedding sites, not in the backlog. Worth having for a
   destination wedding where guests would rather give than fly with a gift.
2. **FAQ page** — also standard. What to wear on a beach in February, kids or no kids,
   what happens if it rains.
3. **Prices** for golf and the fishing charter still show `$[ CONFIRM ]`.
4. **Airport shuttle** is a marked placeholder on both travel pages.
5. **Photography** — every image is an openly-licensed placeholder. The hero is someone
   else's wedding. A Shore Thing's photo slot is empty.
6. **Timezone** for the RSVP window is undecided; `events` has no timezone column.
