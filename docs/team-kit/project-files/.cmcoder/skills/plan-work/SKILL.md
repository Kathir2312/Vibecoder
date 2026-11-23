---
name: plan-work
description: How to break an approved PRD and design into small ordered tasks, each with its acceptance criteria, files, test plan and size. Use after design and before the build starts.
---
# Planning the work

Use `task-template.md` in this skill. Write the plan to `docs/team/tasks/<slug>.md` and put the tasks in the todo list.

## Steps
1. **Read** the PRD, the ADR and the design file (if any). List the acceptance criteria.
2. **Cut vertical slices.** A task delivers something that works end to end (a field, its storage, its screen, its test), not a layer ("write all the models"). The first task is the thinnest slice that runs: a walking skeleton.
3. **Size each task** so one developer can finish it in one focused session: about one to five files, one idea. Mark it S, M or L. Split every L.
4. **Give each task:** a one-line goal; the acceptance criteria it covers (copy their text); the files it will probably touch (by path); the tests that prove it; what it depends on.
5. **Order** by dependency first, then risk: do the uncertain task early, so surprises come while there is time.
6. **Mark independence.** Tasks that touch different files and do not depend on each other get the same "parallel group" letter. Tasks that touch the same file are never in the same group.
7. **Cover everything.** Build a table: every acceptance criterion → the tasks that cover it. A criterion with no task is a gap; a task with no criterion is scope creep. Fix both.
8. **Add the non-feature tasks** the ADR or PRD needs: migration, configuration, build or CI change (for `devops`), documentation (for `writer`). Mark them with the role.
9. **Put it in the todo list**, one item per task, in order.

## Quality check
- Does each task have a clear "done"?
- Can the first task be finished and tested alone?
- Is any task bigger than a session? Split it.
- Does the coverage table have an empty row?
