---
description: Start work on a Linear issue. Usage: /std-issue TAP-7739
argument-hint: TAP-7739
---
For the issue id in $ARGUMENTS:
1. Fetch it from Linear and restate its acceptance criteria in your own words.
2. Name anything in it you think is wrong or underspecified. Do not silently reinterpret.
3. Check its blockers are closed.
4. Create branch from its `gitBranchName`.
5. Write the failing test FIRST (see the tdd-workflow skill), then implement.
6. Finish with /std-gate.
