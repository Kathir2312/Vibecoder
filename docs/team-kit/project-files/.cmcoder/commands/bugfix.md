---
description: Fix a bug with the team: reproduce, fix with a regression test, review, test
argument-hint: <what goes wrong, and how to see it>
allowed-tools: Edit(docs/team/**)
---
Act as the tech lead of the software team. Load the skill `team-workflow` and run its **bug fix path** for this report:

$ARGUMENTS

Start `debugger` with a brief from `brief-template.md` (the report as I wrote it, the commands from `docs/team/PROJECT.md`). If it cannot reproduce the bug, stop and tell me what is missing. Then `reviewer`, then `tester`, with the loop limits of the skill. Keep a status file in `docs/team/status/`. Write no product code yourself, and do not commit, push or deploy. End with the root cause, the fix, the regression test, and what I should check by hand.
