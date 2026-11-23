---
name: release
description: How to prepare a release: choose the version, write the release notes from what was merged, run the checks, list configuration and migration steps, and give a person the exact commands and the way back. Agents prepare; a person pushes and deploys.
---
# Preparing a release

Write the result to `docs/team/releases/<version>.md`. Use `checklist.md` in this skill as the checklist inside it. You prepare; you never tag, push, publish or deploy.

## Steps
1. **Find the last release:** the latest tag (`git describe --tags --abbrev=0`) or the previous file in `docs/team/releases/`. If there is none, say so and use the first commit.
2. **List what changed** since then: `git log <last>..HEAD --oneline`, the merged work in `docs/team/status/` and `docs/team/prd/`. Group it: new, changed, fixed, removed, security.
3. **Choose the version** (semantic versioning): a breaking change to a public API, stored data or configuration → major; new features that stay compatible → minor; only fixes → patch. Give the reason in one line. If `docs/team/PROJECT.md` names another scheme, use that one.
4. **Check the code:** run build, the whole test suite and the linter with the commands in PROJECT.md. Record the results. A red result stops the release: report it.
5. **Check the security review:** there must be a report for these changes and no open critical or high finding.
6. **List what a person must do beyond deploying:** new or changed settings and environment variables (names, never values), database migrations (in order, with the command), data to back up, services to restart.
7. **Write the release notes** for users: what is new and what to do about it, in plain words. Breaking changes first, with the steps to adapt.
8. **Write the exact commands** a person runs, in order (tag, push, build artifacts, deploy, migrate), and next to each the command that undoes it.
9. **Write the smoke test:** three to five things to check right after the release, with the expected result, and when to roll back.

## Output
`docs/team/releases/<version>.md` with: version and reason, notes for users, the checklist with its results, the person's commands with their undo commands, the smoke test.
