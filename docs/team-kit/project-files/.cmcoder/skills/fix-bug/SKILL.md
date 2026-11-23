---
name: fix-bug
description: How to fix a bug properly: reproduce it, write a failing test, find the root cause, make the smallest fix, add a regression test, check nearby code. Use for any bug report or failing test.
---
# Fixing a bug

## Steps
1. **Restate the bug** in one line: what was done, what happened, what was expected. If the report lacks the steps, find them in the logs, the tests or the code; if you cannot, stop and report what is missing.
2. **Reproduce it.** Run the exact steps and see the wrong result yourself. Write down the command or the input. A bug you cannot reproduce is not fixed by a guess: report "cannot reproduce" with what you tried.
3. **Write a failing test** that captures the bug, at the lowest level that shows it. Run it and see it fail with the symptom from the report. No test setup in the project: write the smallest repeatable check and say so.
4. **Find the root cause.** Follow the data from the symptom backwards: read the code path, add temporary logging, narrow with a smaller input, check recent changes (`git log -p` on the file, `git blame`). State the cause in one sentence with `path:line`. The cause is the first place where the value or the state becomes wrong, not where it is noticed.
5. **Fix the cause** with the smallest change. Do not add a catch that hides the error, a special case for the reported input, or a sleep. Remove your temporary logging.
6. **Prove it:** the new test passes; run the whole suite, the build and the linter with the commands in `docs/team/PROJECT.md`.
7. **Look around:** is the same mistake made in sibling code (the same pattern, a copy of the function)? List it. Fix it only if it is the same bug and the fix is the same line; otherwise report it as a follow-up.
8. **Judge the risk:** what else could this change affect? Say what a person should check by hand.

## Report (before the Handoff)
- **Root cause:** one sentence with `path:line`.
- **Fix:** what changed and why that is the cause.
- **Regression test:** the test's name and where it is.
- **Related:** the same mistake elsewhere, if any.
- **Risk:** what else could be affected.
