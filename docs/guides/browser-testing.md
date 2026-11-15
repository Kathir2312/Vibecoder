# Testing a web app with Playwright in cmcoder

cmcoder has no browser of its own. It works the way Claude Code does: **Playwright is a program
you add**, which cmcoder starts with `npx` when it needs it (an MCP server), and in VS Code cmcoder
can also use VS Code's built-in browser. There are two ways to use Playwright:

1. **Playwright's test agents**: a planner, a generator and a healer that Playwright writes into
   your project. Best for writing and fixing Playwright tests.
2. **Playwright's browser server** (`@playwright/mcp`): a general browser the model can drive for
   any job, such as "open the app and check the checkout works".

## What you need

- **Node.js 18 or newer** (it brings `npm` and `npx`), and access to the npm registry. Behind a
  company mirror, set `npm_config_registry` as you do for npm.
- A web app that answers on an address (for example `http://localhost:3000`) and a browser
  program on the PC (Chrome or Edge). Playwright decides which browser it uses; if it can't find
  one it says so and tells you to run `npx playwright install chromium`.

## 1. Playwright's test agents (planner, generator, healer)

In the web project (it needs `@playwright/test`: `npm i -D @playwright/test`):

```
npx playwright init-agents --loop=claude
```

That writes `.claude/agents/playwright-test-planner.md`, `-generator.md` and `-healer.md`, a
`.mcp.json` (Playwright's test server, called `playwright-test`), `seed.spec.ts` and `specs/`.
(`--loop=vscode` writes chat modes for GitHub Copilot instead: cmcoder can't use those.)

**There is nothing to copy or edit.** cmcoder reads `.claude/agents/` (and `~/.claude/agents/`)
as well as its own `.cmcoder/agents/`, and understands Claude's model names: `model: sonnet` and
`opus` in an agent file mean *your* model, `haiku` means your small model. What is left to do:

1. **Trust the project.** A project's agents and MCP servers are used only once you do. In VS Code
   that is Workspace Trust; in the other editors the extension's trust setting; in the terminal
   `cmcoder trust` in the project folder.
2. **Approve the server.** The first message asks "Start MCP server playwright-test" in the chat.
   Allow it: the answer is remembered until the server's settings change. (With `-p` nobody can
   be asked, so cmcoder warns and you run `cmcoder mcp approve playwright-test`.)

Then open the **Agent** dropdown above the message box (VS Code and Visual Studio; in the
terminal `cmcoder --agent playwright-test-planner`) and pick the planner. The whole conversation
is that agent: its instructions, the tools its file lists and nothing else. Ask for what it does:

> Explore http://localhost:3000 and write a test plan for the checkout flow into `specs/`.

Then pick `playwright-test-generator` ("write the tests for `specs/checkout.md`") and
`playwright-test-healer` ("run the tests and fix the failing ones"). **cmcoder (default)** takes
you back. After a Playwright update, run `init-agents` again: the files are used where they are.

## 2. Playwright's browser server, for any job

```
cmcoder mcp add playwright -- npx @playwright/mcp@latest
cmcoder mcp list --check          # playwright: connected · N tools
```

This is the command Claude Code uses too. In a config you share with others, name a version
instead of `@latest` (`@playwright/mcp@<version>`), so everyone gets the same tools. Useful
options go after the package name: `--headless` (no window), `--browser msedge` or `chrome`,
`--isolated` (a clean profile each session). The tools are `mcp__playwright__browser_*`.

Then ask in plain words:

> Start the app with `npm run dev`, open it, add a todo called "Buy milk" and check it shows up.

The first start downloads the package, which can be slower than the 30 seconds cmcoder waits;
if it says the server timed out, set `"startupTimeout": 120` for it in `settings.json` and try
again.

## Permissions and safety

- **Each browser action asks first** (opening a page, clicking, typing, running code), like every
  MCP tool. To stop the question for one server: `"permissions": {"allow": ["mcp__playwright-test"]}`
  covers all its tools. To allow only the reading tools, name them
  (`mcp__playwright__browser_snapshot`) and leave the rest asking.
- **Be careful with "allow everything".** Playwright's browser can open any site, fill forms and
  run scripts in a page. Its own `--allowed-origins` option is, in Playwright's words, not a
  security boundary. cmcoder's rules match tool names, not addresses, so there is no rule like
  "localhost only".
- **Page text is data, not orders.** A page can contain text meant to steer the model. Read what
  it asks to do before you allow it.
- Every permission mode except `bypassPermissions` asks for a browser action, plan mode and
  `acceptEdits` included; `bypassPermissions` skips the questions.

## In VS Code: the Integrated Browser

VS Code has its own **Integrated Browser** (a tab inside the editor) and tools to drive it: the
ones GitHub Copilot's agent uses. When the cmcoder extension finds them, the model can use them:
the page opens in a VS Code tab and you watch it there. The tools are cmcoder's `ide_…` tools (for
example `ide_open_browser_page`); opening a page, clicking and typing ask first, reading a page
or taking a screenshot does not. Screenshots appear in the chat.

- **Needs** VS Code 1.110 or newer with `workbench.browser.enableChatTools` on (VS Code's
  default). The extension's log (**cmcoder: Show Log**) says what it found, and **cmcoder: Show
  the IDE's Browser Tools** lists the tools and their inputs. If your VS Code names them
  differently, put the names in the setting `cmcoder.integratedBrowserTools`.
- **Turn it off:** the setting `cmcoder.useIntegratedBrowser`, or `"browser": {"useIdeBrowser": false}`
  in the settings file.
- VS Code only. In Visual Studio, Eclipse, NetBeans and IntelliJ use Playwright (above).

## `/pick`: point at an element and talk about it

VS Code's Integrated Browser has a "select element" button, but it attaches the element to
**Copilot's** chat, and an extension can't change that. cmcoder has its own: the **Pick element**
button in the cmcoder chat, or `/pick`.

```
/pick                          point at an element on the page that is open in VS Code's browser
/pick clear                    drop what is waiting for your next message
/pick drop 1                   drop one of them (the number is on its chip, from 0)
```

A highlight follows your mouse over the page. **Click the element**: the click is swallowed, so
the page doesn't act on it. Esc on the page cancels; **Stop** in the chat ends the wait; two
minutes without a click ends it too. The element goes to cmcoder **with your next message**: its
page, a CSS selector, its text, its position and its HTML (cut at 6,000 characters), as a note the
model reads before your words, so you can type "make this button red". Pick up to five before you
write; each waits as a chip above the message box, and they are sent once.

- **Only in VS Code's Integrated Browser**, and only when VS Code offers a "run Playwright code"
  tool (see the section above). cmcoder has no browser window of its own to pick in, and `/pick`
  takes no address: open the page in VS Code first.
- **Secrets.** The values of password and hidden fields (login and anti-forgery tokens) in the
  element's HTML are replaced by `[hidden]`. Anything else is sent as it is, so pick the element,
  not the whole form, when a field holds something private.
- **Limits.** Only the top page: not inside an iframe or a closed shadow root. The page's words
  go to the model as data.

## Known limits

- **Pictures from MCP tools** (a screenshot from Playwright's browser) are not shown in the
  chat, and a model that can't see images may not cope with one. Ask for the page's text (a
  *snapshot*, Playwright's default) unless your model sees images.
- **Big pages.** cmcoder offers all of a server's tools to the model and, unlike its own tools,
  doesn't cap an MCP tool's answer; a snapshot of a very large page can fill the window. Ask about
  one part of the app at a time.
- **A mid-sized model.** Playwright's agent prompts are written for Claude. Other models can
  follow them, but expect weaker planning and healing.

## A `/webtest`-style command of your own

cmcoder used to have a `/webtest` command. The same thing, as a file you own, in
`.cmcoder/commands/webtest.md` (or `~/.cmcoder/commands/` for every project):

```markdown
---
description: Start the web app and test it in a browser
---
Test the web app of this project in a real browser, and report what works and what doesn't. Do
not change any project file unless I ask afterwards.

If $ARGUMENTS starts with an address, the app already runs there: don't start it. Otherwise find
the start command (the dev, start, serve or preview scripts in package.json, manage.py, the
README), run it with Bash in the background with its output sent to a file and its process id
printed, and wait until it answers.

Then, with the browser tools, open the app, read the page, and go through the main things a user
can do (each page, form and button), or what I asked in $ARGUMENTS. Read the page again whenever it
changes. JavaScript errors, failed requests and 404s matter even when the page looks fine.

When you are done, close the page and stop the server you started. Report: what you tested; what
worked; and for each failure the element or page, what you did, what happened, what you expected,
and where in the code the cause probably is (read the code to check before you say so).
```

## Moving from `/playwright`, `/webtest` and the Browser tool

| Before | Now |
|---|---|
| `/playwright setup` | `npm i -D @playwright/test`, then `npx playwright init-agents --loop=claude` |
| `/playwright map`, `plan` | the **playwright-test-planner** agent |
| `/playwright generate` | the **playwright-test-generator** agent |
| `/playwright run`, `heal` | the **playwright-test-healer** agent |
| `/playwright all`, `agents` | gone: pick the agents in turn. Nothing to copy: `.claude/agents` is read as it is |
| `.cmcoder/agents/playwright-*.md` overriding the built-in roles | the built-in roles are gone; your files are ordinary agents |
| `/webtest` | the command file above, or just ask |
| the `Browser` tool, `browser.enabled`, `headless`, `executablePath` | Playwright's browser server (section 2); the old settings are ignored |
| `/pick <address>` | open the page in VS Code's browser, then `/pick` |

The `playwright-tests/` folder `/playwright setup` made is yours: `npx playwright test` still
runs what is in it, and the planner and generator can keep writing there if you tell them to.

## If it doesn't work

| You see | Do |
|---|---|
| The agent is greyed out: "(trust this project)" | Trust the project (see step 1), then start a new conversation |
| `MCP server … not approved` | Allow it when the chat asks, or run `cmcoder mcp approve NAME` |
| `'npx' was not found` | Install Node.js 18 or newer and restart the editor (it must be on the PATH) |
| `timed out while starting` | The first `npx` run downloads the package: raise `startupTimeout` for that server |
| A browser is missing | Run `npx playwright install chromium`, or pass `--browser msedge` / `--browser chrome` to `@playwright/mcp` |
| `cmcoder mcp list --check` shows the server failed | The line says why; run the same `npx …` command in a terminal to see Playwright's own message |

## For IT: what ships

cmcoder ships **no browser, no Node.js and no Playwright**: the program and the Python install
are about 140 MB smaller than when Playwright was bundled. Node.js and npm access are needed on
a PC only for this feature, and the browser is the PC's Chrome or Edge, with the company's usual
updates. Everything Playwright's server does runs under your permissions, so MCP servers from a
project's `.mcp.json` are approved by each user before they first start, and managed settings
can allow or deny servers by name (`allowedMcpServers`, `deniedMcpServers`).
