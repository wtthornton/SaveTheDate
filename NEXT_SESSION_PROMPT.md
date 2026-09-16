# Prompt for the next session

Copy everything between the rules into a fresh Claude Code session started in
`/home/wtthornton/code/SaveTheDate`. Update the "Where things stand" block as work
lands, or it will start lying.

---

Picking up SaveTheDate — the wedding site for Bill Thornton & Lisa Gorden,
Port Aransas, Texas, Sunday 13 February 2028.

Read these three first, in order: IMPLEMENTATION_PLAN.md (build order, harness,
definition of done — note §7.1 and §8.1 on DNS and deployment), LESSONS_LEARNED.md
(traps already paid for — read it before touching migrations, hooks or tests), and
.claude/CLAUDE.md (always-on invariants).

Where things stand as of 2026-09-16:
- Phase 0 is done. `.claude/` exists: CLAUDE.md, three subagents (std-schema,
  std-templates, std-review), two slash commands (/std-gate, /std-issue), and the
  migration-format hook.
- TAP-7739 (schema redesign) is code-complete on branch
  `tap-7739-schema-redesign-per-person-attendees-meal-options-rsvp`. Gate is green:
  ruff, ruff format, mypy --strict over app/migrations/scripts/tests, migrations
  up→down→up, 17 tests passing. It is NOT committed and NOT merged.
- Every one of the 13 Linear issues is still in Backlog. Nothing has been moved.

Do this, in order:
1. Confirm the gate is still green, then commit the TAP-7739 work and tell me what
   you would put in the commit message before you push anything.
2. Start TAP-7728 (guest invite page) with /std-issue TAP-7728. It is the goal that
   matters most — getting the invite page in front of Lisa and family — and TAP-7739
   unblocked it. Use the std-templates subagent for template work.
3. TAP-7729 (RSVP window) is mostly already built: the API enforces both ends of the
   window and reports a `phase` of before_open / open / closed. What remains is
   template behavior. Tell me whether to fold it into TAP-7728 rather than assuming.

Useful context for TAP-7728: `.venv/bin/python -m scripts.seed_review_data` creates the
event, the six real schedule segments and five invented guests, and prints an invite
link for each. `GET /invites/{token}` already returns the event, the guest, the
schedule, the phase and any existing answer — the page has a real payload to render.
The design is in the Design canvas: https://claude.ai/artifact/XqYdDkthQNiBbf2LhUhBwy

Non-negotiable:
- guests.invite_token is in people's inboxes once sent. NEVER re-key a guests row.
- Guest routes stay anonymous. The link IS the credential. No guest login, ever.
- Fake guest data only until TAP-7725 (host auth) lands.
- htmx 2.x only — htmx 4 changed attribute inheritance and fails silently.
- 18px body text, 44px touch targets, native form controls. The guest list skews old.
- No # noqa, no # type: ignore, no skipped tests, no swallowed exceptions.
  If the right fix is out of scope, stop and tell me.
- American English. Postgres is on host port 5434, not 5432.
- This repo is PUBLIC on GitHub. Never write a credential into it — record where a
  secret lives, never its value.

Do NOT use the Workflow tool unless I ask — the token pool is shared with my other
sessions. Plain subagents are fine.

Ask me before TAP-7740 (DNS move). It is worse than the backlog implies: the
nltlabs.ai zone carries live Microsoft 365 email behind Proofpoint, and eleven
hostnames across my Render services. See plan §7.1. For TAP-7738, plan on the
Cloudflare Quick Tunnel route, which needs no DNS change at all — note `cloudflared`
is not installed on this box yet.

Two habits I want kept, both learned the hard way:
- If tests pass on the first run, they have proved nothing. Break the thing on purpose
  and confirm the right test goes red — and mutate the source of truth, not a copy a
  fixture will regenerate.
- Verify what a hook or subagent actually does, rather than trusting its declaration.
  `tools:` on a subagent turned out not to be a hard allowlist.

Linear: project SaveTheDate, team TappsCodingAgents (TAP), issues TAP-7725–7740.

---

## If you want a shorter version

Read IMPLEMENTATION_PLAN.md, LESSONS_LEARNED.md and .claude/CLAUDE.md. Phase 0 and
TAP-7739 are done; TAP-7739 is green but uncommitted on its branch, and all 13 Linear
issues are still Backlog. Commit TAP-7739, then start TAP-7728 with /std-issue TAP-7728.
Same non-negotiables as the plan. Don't use the Workflow tool. Ask before TAP-7740 —
that zone carries live company email.
