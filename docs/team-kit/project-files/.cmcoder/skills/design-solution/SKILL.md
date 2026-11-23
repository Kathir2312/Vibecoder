---
name: design-solution
description: How to decide a technical design and record it as an ADR: options, the decision, data and API changes, risks, tests, rollback, and what needs a person's sign-off. Use after a PRD is approved and before tasks are planned.
---
# Designing a solution

Use `adr-template.md` in this skill. Write the file to `docs/team/adr/NNNN-<slug>.md` (NNNN: the next free number in `docs/team/adr/`, starting at 0001).

## Steps
1. **Read the PRD** and list what the design must satisfy: the acceptance criteria, the non-functional needs, the out-of-scope list.
2. **Read the code.** Find how the project already does similar things: where the code for this kind of feature lives, how data is stored, how errors and permissions are handled, which libraries are in use. The default decision is to do it the way the project already does it.
3. **List at least two options.** One may be "extend what exists". For each: how it works in a few lines, what it costs, what it risks. Do not pad with options nobody would choose.
4. **Decide**, and give the reasons in terms of the PRD, not taste. Say what you gave up.
5. **Describe the change:**
   - components touched (files or modules, by path);
   - data: new or changed tables, fields, indexes, and how existing data is migrated;
   - interfaces: new or changed endpoints, messages, function signatures others call, with an example request and response;
   - errors and permissions: who may do it, what happens on failure;
   - configuration: new settings and their defaults.
6. **Map every acceptance criterion** to the part of the design that satisfies it. A criterion with no home is a gap.
7. **Needs a person's sign-off** — list any of these, each with your recommendation:
   a new dependency; a schema change or migration; a change to a public API or contract; a change to authentication or permissions; a recurring cost; deleting or rewriting data.
8. **Plan the proof:** which tests will show it works (unit, integration, end to end), and what the risky cases are.
9. **Plan the way back:** how to roll the change back, and what cannot be undone.

## Quality check
- Would a developer who did not read the PRD know what to build?
- Is each decision explained by a requirement or a constraint of the existing code?
- Is the design the smallest that satisfies the criteria?
