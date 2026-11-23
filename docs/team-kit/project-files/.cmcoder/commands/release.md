---
description: Prepare a release: security review, version, notes, checklist and the commands you run
argument-hint: [version]
allowed-tools: Edit(docs/team/**)
---
Act as the tech lead of the software team. Load the skill `team-workflow` and run its **release path**. Version, if I gave one: $ARGUMENTS (otherwise let `devops` propose it with a reason).

Start `security` on the changes since the last release. Then `devops` with the skill `release`, then `writer` for the changelog. Agents prepare everything and run nothing that publishes: no tag, no push, no deploy. Stop at **Gate E**: show me the release file `docs/team/releases/<version>.md`, the open findings, and the exact commands I must run, each with its undo command.
