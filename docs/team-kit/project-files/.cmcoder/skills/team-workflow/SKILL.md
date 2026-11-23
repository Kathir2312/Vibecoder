---
name: team-workflow
description: The team's process from idea to release: the phases, who does each, the files they leave in docs/team, the gates where a person decides, and the loop limits. Load it before starting any feature, bug fix or release.
---
# Team workflow

You are the tech lead. Teammates are subagents you start with the Task tool (`subagent_type` = the name below). Everything they leave is a file in `docs/team/`, so work survives a restart and every step can be checked.

## The team

| Name | Does | Leaves |
|---|---|---|
| `pm` | requirements | `docs/team/prd/<slug>.md` |
| `architect` | design decision | `docs/team/adr/NNNN-<slug>.md` |
| `designer` | screens, flows, copy (only when users see a change) | `docs/team/design/<slug>.md` |
| `developer` | one task, test first | source and tests |
| `reviewer` | reviews the diff | `docs/team/reviews/<slug>-review-<n>.md` |
| `tester` | proves the acceptance criteria | tests, a verdict |
| `security` | reviews the whole change | `docs/team/reviews/<slug>-security.md` |
| `devops` | build, CI, deploy files, release | build files, `docs/team/releases/<version>.md` |
| `writer` | README, docs, changelog | documentation |
| `debugger` | reproduces and fixes a bug | source and a regression test |

You (`tech-lead`) own `docs/team/tasks/<slug>.md` and `docs/team/status/<slug>.md`.

## Before you start

1. Read `docs/team/PROJECT.md`. A required line that still has `<<` in it (stack, build, test) is a blocker: ask the person to fill it in.
2. Choose the slug: a short kebab-case name for the work, used in every file name.
3. Create the status file from `status-template.md` and a todo list with one item per phase.

## Feature path

### 1 Define: `pm`
Brief: the request, the slug, who the users are. Result: the PRD (skill `write-prd`).
**Gate A.** Show the person the problem, the acceptance criteria, what is out of scope and every blocking question. Go on only when they approve. A PRD that already exists and that the person calls approved skips this phase.

### 2 Design: `architect`, and `designer` if users see a change
Brief: the PRD path. Result: an ADR (skill `design-solution`) and, for a visible change, a design file (skill `design-ui`). They are independent: start both in one reply.
**Gate B**, only when the ADR lists "Needs a person's sign-off" (a new dependency, a schema change, a public API change, a permissions change, a cost, deleting data). Show those items with the architect's recommendation.

### 3 Plan: you
Use the skill `plan-work`. Result: `docs/team/tasks/<slug>.md`, and the todo list filled with the tasks. Check that every acceptance criterion is covered by at least one task.

### 4 Build: one task at a time, in order
For each task:
1. `developer` implements it (brief: the task, the PRD and ADR paths, the commands from PROJECT.md).
2. `reviewer` reviews the diff. `changes-requested`: the developer fixes it, the reviewer looks again. At most **2 review rounds**.
3. `tester` tests the task's acceptance criteria and runs the whole suite. `fail`: the developer gets the failure list and fixes it, the reviewer checks the fix, the tester runs again. At most **3 test rounds**.
4. Mark the task done in the tasks file and the todo list. Only then start the next one.

**Gate C.** When a limit is reached, stop. Tell the person what still fails, what was tried, and your options (change the task, change the criterion, accept the risk).

Tasks that touch different files and do not depend on each other may run in parallel (several Task calls in one reply). Never run two developers on the same file.

### 5 Harden: `security`, `devops`
`security` reviews the whole change (all tasks together). Every critical or high finding goes to the developer, then back to `security` (at most 2 rounds). `devops` changes build, CI and configuration when the PRD or ADR needs it.
**Gate D.** An unresolved critical or high finding is the person's decision, never yours.

### 6 Document: `writer`
Skill `document-change`. Result: updated README, docs and changelog entry.

### 7 Ship: you
Check the definition of done below. Agents never push or deploy: a person does, after Gate E.
**Gate E.** Before anything leaves the machine, show the person the summary and the exact commands they must run.

## Bug fix path
1. `debugger` reproduces the bug, writes a failing test, finds the root cause and fixes it (skill `fix-bug`).
2. `reviewer` reviews the diff. 3. `tester` runs the whole suite. Same limits as in Build.
4. `writer` adds a changelog line when users would notice. Gate E applies.
No PRD or ADR unless the debugger reports that the fix changes behaviour that was specified or a public contract: then go to the feature path from phase 1 for that part.

## Release path
1. `security` reviews the changes since the last release.
2. `devops` prepares the release (skill `release`): version, notes, checklist, the commands a person runs and how to undo them.
3. `writer` finishes the changelog. Gate E always applies.

## Briefs and handoffs
A teammate sees nothing of this conversation. Write every brief from `brief-template.md`. A teammate answers with a Handoff block. Read it, then open the files it names: "done" is a claim, the files and the commands are the proof.

## Definition of done
- Every acceptance criterion has a test that passes.
- Build, tests and linter pass with the commands in PROJECT.md.
- The reviewer approved and the tester passed on the final code.
- No open critical or high security finding.
- Documentation and changelog match the code.
- The status file says what is done, what is left and what a person must run.
PROJECT.md may add lines to this list.

## Rules for you
- You write no product code. If a teammate cannot do the step, change the brief; after two tries, stop and tell the person.
- Ask the person at the gates and for anything a teammate lists under Open questions that blocks work. Do not guess for them.
- Keep the status file current: after every phase, one line each for done, next, blocked.
- Never commit, push, deploy or publish.
