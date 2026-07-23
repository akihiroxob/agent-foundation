---
name: investigate-bug
description: Reproduce, isolate, fix, and verify a software defect with minimal unrelated changes.
---

# Investigate a bug

1. Read the error, reproduction steps, and nearby implementation.
2. Reproduce the failure using the smallest relevant command.
3. Form a concrete root-cause hypothesis and verify it against the code.
4. Add or update a regression test when practical.
5. Apply the smallest safe fix.
6. Run the focused test first, then the repository's broader verification command.
7. Report the root cause, changed files, and verification results.

Ask before changing a public API, database schema, production dependency, deployment configuration, or remote state.
