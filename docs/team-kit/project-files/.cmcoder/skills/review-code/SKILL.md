---
name: review-code
description: How to review a change: the checklist, severity labels, the verdict rule and the report format. Use when reviewing a developer's diff before testing, or a pull request.
---
# Reviewing code

Write the report with `report-template.md` to `docs/team/reviews/<slug>-review-<n>.md`.

## Steps
1. **Get the change:** `git status` and `git diff` (or the range the task names). Read each changed file in full when the diff does not show enough context.
2. **Know what it should do:** the task and its acceptance criteria. A change can be clean and still miss a criterion.
3. **Go through the checklist.** For each problem write one finding.
4. **Run the build and tests** if a claim needs proof ("all tests pass"). Change nothing.
5. **Give a verdict.**

## Checklist
- **Correct:** does it satisfy every acceptance criterion it claims? Check the unusual inputs: empty, null, zero, one, many, very long, wrong type, repeated, concurrent.
- **Errors:** are failures handled, reported with a useful message, and never swallowed silently? Is cleanup done on every path?
- **Security basics:** user input validated; no string-built SQL, commands or HTML; permissions checked on the server for every action; no secrets or personal data in code or logs.
- **Tests:** is there a test for each criterion, for an error case and for a boundary? Do the tests assert results, not only that code ran? Is any test skipped, weakened or deleted?
- **Fit:** same style, layers and naming as the surrounding code; no copy-paste of something that already exists; no unrelated edits.
- **Simplicity:** the smallest change that works; no dead code, debug output or speculative options.
- **Performance traps:** a query or call inside a loop, unbounded results, loading everything into memory, blocking calls in async code.
- **Compatibility:** public API, stored data, configuration and migrations still work for existing users and data.
- **Readability:** names say what things are; comments say why, not what.

## Severity
- **blocker:** wrong result, data loss, security hole, broken build or test, a missed acceptance criterion.
- **major:** likely bug in an unusual case, missing error handling, missing test for a criterion, a design that will hurt soon.
- **minor:** worth fixing, not urgent: naming, small duplication.
- **nit:** taste. Say it once, briefly, or leave it out.

## Verdict
`changes-requested` when there is any blocker, or three or more majors. Otherwise `approve`, with the minors and nits listed.

## Every finding has
severity, `path:line`, what is wrong (one sentence), and the fix (one sentence or a short snippet). No finding without a place and a fix.

## Do not
- Rewrite the code yourself. Describe the fix.
- Review what the task did not change, except where the change breaks it.
- Pad. Five exact findings beat twenty vague ones.
