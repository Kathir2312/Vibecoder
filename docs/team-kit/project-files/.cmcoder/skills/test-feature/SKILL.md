---
name: test-feature
description: How to test a change against its acceptance criteria: a criteria-to-tests table, negative and boundary cases, running the whole suite, handling flaky tests, and reporting pass or fail with each failure. Use for every tester task.
---
# Testing a feature

## Steps
1. **List the criteria** you were given (copy their text). If none were given, take them from the PRD or the task file.
2. **Make the table:** criterion → the test that proves it → the level (unit, integration, end to end) → exists already or new. Use the levels and tools in `docs/team/PROJECT.md`; prefer the cheapest level that can prove the criterion.
3. **Find the project's test conventions:** where tests live, naming, fixtures, helpers. Follow them exactly.
4. **Write the missing tests.** For each criterion: the happy path with concrete values. Then what the criteria imply:
   - invalid input and missing input;
   - boundaries: 0, 1, the maximum, one over the maximum;
   - empty state and many items;
   - who may not do it (permissions);
   - repeating the action twice;
   - failure of something it depends on, when the code handles it.
5. **Run the whole suite** with the command from PROJECT.md, not only your tests. A change can break a test far away.
6. **A failing test:** run it once more. Fails again: a failure. Passes the second time: report it as flaky with its name; do not hide it and do not call the change failed because of it.
7. **Decide whose fault:** for each failure, say whether the test or the product is wrong, with the evidence (`file:line`). Never edit product code.

## Rules
- A test must be able to fail. Check that a new test fails when the behaviour is wrong (comment the behaviour out in your head, or run it before the change if you can).
- Assert results, not that code ran. No test without an assertion.
- Do not skip, weaken or delete a test to get a green run.
- Do not depend on the order of tests, the clock or the network unless the project's tests already do, and then in the project's way.
- Leave no test data behind.

## Report
End your answer with:
- `verdict: pass` or `verdict: fail`;
- one or two sentences on what you tested and what happened;
- for a fail, each failure: the test's name, the exact error (shortened), `file:line`, and whose fault you think it is;
- the table of criteria → tests, with ✓ for each proven criterion.
