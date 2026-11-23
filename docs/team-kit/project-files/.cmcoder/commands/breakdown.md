---
description: From an approved PRD to a design and an ordered task list (phases 2 and 3)
argument-hint: <PRD path or its slug>
allowed-tools: Edit(docs/team/**)
---
Act as the tech lead of the software team. Load the skill `team-workflow` and do only its **phase 2, Design** and **phase 3, Plan** for the approved PRD:

$ARGUMENTS

Read the PRD first. Start `architect` (and `designer` when users see a change, in the same reply). If the ADR lists items under "Needs a person's sign-off", stop at **Gate B** and ask me. Then plan the work with the skill `plan-work`: write `docs/team/tasks/<slug>.md`, fill the todo list, and check that every acceptance criterion is covered. Show me the task list and stop. Do not start the build.
