# Paste this into your project's CMCODER.md (or CLAUDE.md)

It tells the everyday agent that the team exists, so it uses it for real work instead of doing everything itself. Six lines; the rest of the kit works without it.

```markdown
## Software team
This project has a software team in `.cmcoder/` (agents, skills, commands). Read `docs/team/PROJECT.md` before any work: it has the stack, the commands and the rules.
For anything bigger than a small edit, load the skill `team-workflow` and hand the steps to the team's agents with the Task tool (`pm`, `architect`, `designer`, `developer`, `reviewer`, `tester`, `security`, `devops`, `writer`, `debugger`) instead of doing it all yourself.
Commands: `/kickoff <idea>`, `/feature <what>`, `/breakdown <prd>`, `/bugfix <report>`, `/release [version]`, `/team-status`. Never commit, push or deploy: a person does that.
```
