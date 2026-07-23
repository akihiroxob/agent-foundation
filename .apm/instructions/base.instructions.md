---
description: Common engineering rules for repositories using this harness.
applyTo: "**/*"
---

# Common engineering rules

- Inspect related code and existing conventions before editing.
- Prefer the smallest change that solves the requested problem.
- Preserve public APIs unless the task explicitly requires a change.
- Run the repository's relevant formatter, linter, type checker, and tests after changes.
- Do not add production dependencies, change database schemas, deploy, or push without explicit authorization.
- Never expose secrets or include `.env`, private keys, or credentials in output.
- When a repeated workflow would benefit from automation, propose or create a reusable Skill or script.
