# Playwright's test agents in cmcoder

Playwright can write three agents for your project: a **planner** (explores the app and writes a
test plan), a **generator** (turns the plan into Playwright tests) and a **healer** (runs the tests
and fixes the ones that fail). They are ordinary agent files, and cmcoder can use them: you pick one
in the chat's **Agent** dropdown and talk to it.

## Set them up (once per project)

In the web project (it needs `@playwright/test`):

```powershell
npx playwright init-agents --loop=claude
```

That writes `.claude/agents/playwright-test-*.md`, `.mcp.json` (Playwright's test MCP server) and a
`seed.spec.ts`. (`--loop=vscode` writes Copilot chat modes: cmcoder can't use those.) cmcoder reads
agents from `.cmcoder/agents/`, so copy them there and fix the model line:

```powershell
New-Item -ItemType Directory -Force .cmcoder\agents | Out-Null
Get-ChildItem .claude\agents\playwright-test-*.md | ForEach-Object {
  $t = (Get-Content $_.FullName -Raw) -replace '(?m)^model: .*$','model: inherit' -replace '(?m)^color: .*\r?\n',''
  Set-Content -Path (Join-Path .cmcoder\agents $_.Name) -Value $t -NoNewline
}
```

```bash
mkdir -p .cmcoder/agents
for f in .claude/agents/playwright-test-*.md; do
  sed 's/^model: .*/model: inherit/; /^color: /d' "$f" > ".cmcoder/agents/$(basename "$f")"
done
```

`model: sonnet` is a Claude model name; `inherit` uses your own model (or write
`provider:model` to give one agent a model of its own). Then trust the project (a project's agents
and MCP servers are used only once you do) and approve its MCP server:

```
cmcoder trust --yes
cmcoder mcp approve playwright-test
cmcoder mcp list --check        # playwright-test (project): connected · 39 tools
```

To stop the permission question on every Playwright tool, add one rule to the settings:
`"permissions": {"allow": ["mcp__playwright-test"]}` (it covers all tools of that server).

## Use them

In VS Code or Visual Studio, open the **Agent** dropdown above the message box and pick
`playwright-test-planner`. The note "Now talking to the playwright-test-planner agent" appears and
the whole conversation is that agent: its instructions, its tools (only what its file lists) and
its model. Ask it for what it does:

> Explore http://localhost:3000 and write a test plan for the checkout flow into `specs/`.

Then pick `playwright-test-generator` ("write the tests for `specs/checkout.md`"), and
`playwright-test-healer` ("run the tests and fix the failing ones"). Choose **cmcoder (default)** to
go back. In the terminal: `cmcoder --agent playwright-test-planner`.

If an agent is greyed out with "(trust this project)", run `cmcoder trust` in the project folder
and start a new conversation.

## Good to know

- **The agent is for the whole conversation**, not one message: what it reads and the pages it
  opens stay in the chat, so use **History** / a new conversation for a different job. You can
  switch agents in the middle: the conversation goes on, with the new agent's instructions.
- **You can't switch during a turn.** The dropdown is disabled while the agent works.
- **Playwright's own browser.** These agents drive the browser of Playwright's MCP server (a
  separate window, headed by default; add `"--headless"` to `args` in `.mcp.json` to hide it), not
  VS Code's Integrated Browser. The first run may need `npx playwright install chromium`.
- **Updating:** when you update Playwright, run `npx playwright init-agents --loop=claude` again
  and repeat the copy.
- **Model quality:** the prompts are written for Claude. A mid-sized model can follow them, but
  expect weaker planning and healing than with a large one.
