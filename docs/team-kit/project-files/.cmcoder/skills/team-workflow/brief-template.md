# Brief for a teammate

Copy this into the Task prompt and fill every line. A teammate has not seen the conversation.

```
Goal: one sentence: what must exist or be true when you are done.
Slug: <slug>
Read first: docs/team/PROJECT.md, then <PRD path>, <ADR path>, <task file and the task number>
Do: the steps, in order, in plain words (what to build, check or write).
Acceptance criteria covered: AC1, AC3 (copy their text, do not only name them)
Write to: the exact file path(s) for your result
Do not: what is out of bounds for this task (files, refactors, new dependencies)
Done means: how you will judge the result (tests passing, a file exists, a verdict given)
Previous findings: the reviewer's or tester's list, when this is a fix round (paste it)
Commands: build, test and lint commands from PROJECT.md, if the role runs them
```

Rules for a good brief:
- One task per brief. If it needs "and", split it.
- Paste text the teammate needs (a finding list, an acceptance criterion) instead of pointing at the conversation.
- Say what "done" looks like. A teammate that does not know when to stop either stops too early or never.
