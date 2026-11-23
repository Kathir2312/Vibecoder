---
name: implement-task
description: How to implement one task: read the criteria, find similar code, write the failing test first, make the smallest change, run build, tests and linter, read your own diff. Use for every coding task and for fixing review or test findings.
---
# Implementing one task

## Steps
1. **Read the task** and the acceptance criteria it covers, then the ADR and the design file if they exist. If the task says "and" twice, report it as too big instead of doing half.
2. **Find the nearest similar code** (search by what it does, not only by name). Copy its structure, naming, error handling and test style. The project's way beats your way.
3. **Check the commands** in `docs/team/PROJECT.md`: build, run, test (all), test (one), lint, format. If one is missing, say so in Assumptions and use the usual one for the stack.
4. **Test first.** Write the test for the first acceptance criterion; run it and see it fail for the right reason. If the project has no tests, write the smallest check you can run and say so.
5. **Make it pass** with the smallest change. Then the next criterion, the same way. Handle the error and empty cases the criteria name.
6. **Run everything:** build, the whole test suite, the linter and formatter. Fix what you broke. Do not hand over a red build.
7. **Read your own diff** (`git diff`). Remove debug output, commented-out code, unrelated edits and unused imports. Check names and error messages.
8. **Report** each file with what changed, the commands you ran and their results, assumptions, and anything that blocked you.

## Rules
- Match the code that is there: style, layers, naming, logging, error handling.
- Smallest change that satisfies the criteria. No drive-by refactoring; report it instead.
- No new dependency unless the ADR names it.
- Validate input at the boundary; never build SQL, commands or HTML by joining strings with user input; never log secrets or personal data.
- Never weaken, skip or delete a test to make it pass. A test that is wrong is reported, with the reason.
- Never edit generated files, lock files or migrations by hand unless the task says so.
- Do not change an acceptance criterion because it is hard: report it as blocked.

## When fixing findings
Take the reviewer's or tester's list item by item. Fix every blocker, major and failing test. For each minor or nit say "fixed" or "left, because …". Re-run everything after the last fix.
