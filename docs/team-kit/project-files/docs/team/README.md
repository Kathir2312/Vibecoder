# docs/team

The software team's working files. The agents write here, so work survives a restart and every step can be checked. **Commit these files**: they are the project's memory.

| Folder or file | Written by | Holds |
|---|---|---|
| `PROJECT.md` | you, once | stack, commands, conventions, rules: read by every teammate first |
| `prd/<slug>.md` | `pm` | what to build and the acceptance criteria |
| `adr/NNNN-<slug>.md` | `architect` | the design decision, its options and its risks |
| `design/<slug>.md` | `designer` | screens, states, exact text, accessibility |
| `tasks/<slug>.md` | `tech-lead` | the ordered tasks and which criteria each covers |
| `status/<slug>.md` | `tech-lead` | the phase, what it waits for, decisions made, what a person must run |
| `reviews/<slug>-review-<n>.md` | `reviewer` | findings and a verdict |
| `reviews/<slug>-security.md` | `security` | security findings by severity |
| `releases/<version>.md` | `devops` | release notes, checklist, the commands a person runs |

The folders appear when the first file is written. `<slug>` is a short kebab-case name for the piece of work, the same in every file of it.

How the team works is in the skill `team-workflow` (`.cmcoder/skills/team-workflow/SKILL.md`).
