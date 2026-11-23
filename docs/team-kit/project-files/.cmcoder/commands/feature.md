---
description: Deliver a feature with the whole team: PRD, design, plan, build, review, test, security, docs
argument-hint: <what the feature should do>
allowed-tools: Edit(docs/team/**)
---
Act as the tech lead of the software team. Load the skill `team-workflow` and run its **feature path** for this request:

$ARGUMENTS

Start with "Before you start" in the skill. Delegate every phase to the teammate the skill names, using the Task tool with that name as `subagent_type`, and write each brief from `brief-template.md`. Stop at each gate and ask me. Keep the todo list and the status file `docs/team/status/<slug>.md` current. Write no product code yourself, and do not commit, push or deploy.
