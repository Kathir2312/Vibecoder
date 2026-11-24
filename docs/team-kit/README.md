# The software team kit

A complete software team as files you copy into a project: a **tech lead** who runs the work, **ten teammates** (product manager, architect, designer, developer, reviewer, tester, security, DevOps, writer, debugger), **twelve skills** that hold the know-how and the templates, and **six commands** to start the work. The team follows one workflow from idea to release, leaves its work as files in `docs/team/`, and stops at five gates where you decide.

The agents are generic. Everything about *your* project (stack, commands, rules) lives in one file you fill in once: `docs/team/PROJECT.md`.

## Put it in a project

Copy everything inside `project-files/` into the project's root, so that the project has `.cmcoder/` and `docs/team/`.

```bash
cp -r docs/team-kit/project-files/. /path/to/your/project/
```
```powershell
Copy-Item -Recurse -Force docs\team-kit\project-files\* C:\src\YourProject\
```

Then, once per project:

1. **Fill `docs/team/PROJECT.md`.** Replace each `<<…>>`. Finished examples to copy the layout from: `examples/PROJECT.aspnet-mvc.md` and `examples/PROJECT.node-typescript.md`. The tech lead refuses to start while one is left.
2. **Trust the project.** A project's agents are used only once it is trusted: VS Code's Workspace Trust, the extension's trust setting in other editors, or `cmcoder trust` in the terminal.
3. **Optional:** paste `CMCODER-snippet.md` into the project's `CMCODER.md` so the everyday agent knows the team exists. See also "Settings worth adding" below.

**For every project at once**, copy `.cmcoder/agents`, `.cmcoder/skills` and `.cmcoder/commands` into `~/.cmcoder/` instead (they apply everywhere, with no trust step). `docs/team/PROJECT.md` is still per project.

**Claude Code:** the files are in Claude Code's format. Rename `.cmcoder` to `.claude` (cmcoder itself reads `.cmcoder/agents` and `.claude/agents`, but skills and commands only from `.cmcoder`). The format is the same; I have not run the kit in Claude Code.

## How the team works

```
idea ─► 1 Define ─A─► 2 Design ─B*─► 3 Plan ─► 4 Build ─C*─► 5 Harden ─D*─► 6 Document ─► 7 Ship ─E─► you push
         pm            architect       tech-lead  ┌─────────────┐  security       writer       tech-lead
                       designer*                  │ developer   │  devops
                                                  │ reviewer ↺2 │
                                                  │ tester   ↺3 │   per task, in order
                                                  └─────────────┘
A–E: a gate, where the lead stops and asks you.   *: only when needed.   ↺n: at most n rounds.
```

| Gate | You decide | When |
|---|---|---|
| A | approve the PRD | after Define |
| B | sign-off items in the design (a new dependency, a schema change, a public API change, permissions, a cost, deleting data) | after Design, only if there are any |
| C | what to do when a review or test loop hits its limit | when it happens |
| D | an open critical or high security finding | after Harden, only if one is open |
| E | the summary and the exact commands to run | before anything leaves the machine |

Agents never commit, push, deploy or publish. They prepare, and say what you must run.

## What is in the kit

### Agents (`.cmcoder/agents/`)

| Name | Job | May edit |
|---|---|---|
| `tech-lead` | runs the workflow, delegates, holds the gates; **the one you talk to** | `docs/team/status`, `docs/team/tasks` |
| `pm` | requirements and acceptance criteria | `docs/team/prd` |
| `architect` | the design decision (ADR) | `docs/team/adr` |
| `designer` | screens, states, exact text, accessibility | `docs/team/design` |
| `developer` | one task, test first | source and tests |
| `reviewer` | reviews the diff, gives a verdict | `docs/team/reviews` |
| `tester` | proves the acceptance criteria, runs the suite | test files |
| `security` | reviews the whole change | `docs/team/reviews` |
| `devops` | build, CI, deployment files, release | build and deploy files, `docs/team/releases` |
| `writer` | README, docs, changelog | documentation |
| `debugger` | reproduces and fixes a bug | source and tests |

Each has a short prompt (one job, a lane, ground rules) and ends with the same **Handoff** block. All use `model: inherit`, your own model.

### Skills (`.cmcoder/skills/`)

| Skill | Used by | Has |
|---|---|---|
| `team-workflow` | tech-lead | phases, gates, limits, `brief-template.md`, `status-template.md` |
| `write-prd` | pm | `template.md` |
| `design-solution` | architect | `adr-template.md` |
| `design-ui` | designer | `template.md` |
| `plan-work` | tech-lead | `task-template.md` |
| `implement-task` | developer | the test-first loop and the rules |
| `review-code` | reviewer | checklist, severities, `report-template.md` |
| `test-feature` | tester | criteria-to-tests table, negative and boundary cases |
| `security-review` | security | checklist, severities, `report-template.md` |
| `fix-bug` | debugger | reproduce, root cause, regression test |
| `release` | devops | version rules, `checklist.md` |
| `document-change` | writer | what to update, how to verify it |

### Commands (`.cmcoder/commands/`)

| Command | Does |
|---|---|
| `/kickoff <idea>` | Define only: a PRD, then Gate A |
| `/feature <what>` | the whole feature path |
| `/breakdown <prd>` | Design and Plan for an approved PRD |
| `/bugfix <report>` | the bug fix path |
| `/release [version]` | the release path |
| `/team-status [slug]` | where each piece of work stands (read-only) |

### Project files

`docs/team/PROJECT.md` (fill once) and `docs/team/README.md` (what each folder holds). The agents create `prd/`, `adr/`, `design/`, `tasks/`, `status/`, `reviews/` and `releases/` as they work: commit them.

## First run

1. `/agents` lists the eleven (with cmcoder's own `explore` and `general-purpose`); `/help` lists the six commands; `cmcoder doctor` shows all of them under "Commands, agents and skills".
2. `/team-status` says there is no work yet.
3. Try something small:

```
/feature add an "export to CSV" button to the orders list
```

What happens: the lead checks `PROJECT.md`, creates `docs/team/status/orders-csv-export.md` and a todo list, and starts `pm`. You get **Gate A** with the acceptance criteria. After you approve: `architect` writes `docs/team/adr/0001-orders-csv-export.md` and `designer` the screen; the lead writes `docs/team/tasks/orders-csv-export.md`; each task goes through `developer`, `reviewer` and `tester`; `security` and `devops` look at the whole change; `writer` updates the docs; the lead stops at **Gate E** with the commands you run.

To work in the team's voice for the whole conversation, pick **tech-lead** in the Agent dropdown (VS Code and Visual Studio), or start with `cmcoder --agent tech-lead`, and type the request.

## With cmcoder's built-in `/team`

`/team <feature>` is a fixed pipeline (PM, developer, tester with a retry loop, DevOps). It uses the agents named `pm`, `developer`, `tester` and `devops` if the project has them, so this kit's versions take over those four roles with no setting. The other seven only work through the tech lead.

`/team` runs at most once per request. A tester that can't run any test (no test project, or it runs out of steps) ends the run as **UNTESTED** and says so, instead of sending the developer back to fix nothing; a real failing test still goes back to the developer, up to `team.maxRounds` times.

## Settings worth adding

In the project's `.cmcoder/settings.json` (it takes effect once the project is trusted):

```json
{
  "permissions": { "allow": ["Edit(docs/team/**)"] },
  "verify": { "enabled": true, "commands": ["<your build command>", "<your test command>"] }
}
```

The first lets the team write its files without asking each time (the commands already do this for their own turn). The second makes cmcoder run your build and tests itself after any request that changed code, on top of what the agents run.

## Make it yours

- **Models.** Every agent is `model: inherit`. Give a cheap job a smaller model by editing its file: `model: small` uses your small model (`smallFastModel`); `provider:name` names any model.
- **Rounds and gates.** Change the numbers in `docs/team/PROJECT.md` ("Team settings") and in the skill `team-workflow`.
- **A role you do not need.** Delete its agent file and its line in the skill's table. A project without a UI can drop `designer` and `design-ui`.
- **A role you need twice.** Copy `developer.md` to `frontend-developer.md`, change `name`, and add a line about its stack. Add it to the table in `team-workflow`.
- **Your rules.** Add them to `PROJECT.md` ("Rules for this project", "Definition of done"). Everyone reads that file first.
- **Tool lists.** `tools:` in an agent file limits what it can use. A lane ("You may edit…") is an instruction, not a lock: for a hard limit, add a deny rule under `permissions` in the settings.

## Limits worth knowing

- **Teammates cannot ask you anything**, and a teammate cannot start another. All questions go through the lead, which is why every brief must be complete.
- **A teammate starts empty.** It knows only its brief, `PROJECT.md` and the files. Good files in `docs/team/` are what make later steps good.
- **Mid-sized models need small steps.** The prompts are short and one job each on purpose. When a step goes wrong, make the task smaller before changing the prompt.
- **A teammate that runs out of model calls** ends its step early (`subagentMaxTurns`, 100 by default). Ask the lead to split the task.
- **The kit finds problems; it does not remove the need to read.** You approve the PRD, the sign-off items and the commands you run.

## Keeping the kit current

Copy the new files over the old ones, after comparing any file you edited. Your `PROJECT.md` and everything under `docs/team/` is yours: do not overwrite it.
