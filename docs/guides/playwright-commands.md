# Playwright tests for your web app: the `/playwright` commands

cmcoder can set up Playwright tests for any web app (the app only has to answer on a web
address), plan what to test, write the tests, run them and fix the ones that are wrong. It is
one command, `/playwright`, and it tells you the next step itself: type just `/playwright` to see
where you are.

## Start in five commands

In the VS Code and Visual Studio chats an empty conversation shows buttons for the first steps
(**What's next?**, **Set up**, **Plan the tests**, **Do it all**): a button puts the command in the
message box for you to finish, so you don't have to remember it.

Start your app first (or give its start command, below), then in the chat:

| # | Type | What happens |
|---|---|---|
| 1 | `/playwright setup http://localhost:5080` | creates the `playwright-tests/` folder (config, seed test) and installs Playwright's test runner |
| 2 | `/playwright map` | browses the running app and lists every page it can reach, grouped into areas |
| 3 | `/playwright plan` | writes a test plan for the next area (repeat for each area) |
| 4 | `/playwright generate` | writes the Playwright tests for that plan |
| 5 | `/playwright run`, then `/playwright heal` | runs them; `heal` fixes the tests that are wrong |
| or | `/playwright all` | steps 2 to 5 for every area, in one go |

**All of it in one go:** after `setup`, type `/playwright all` (or `/playwright all Orders` for one
area). It maps the app if needed, then area by area plans, writes, runs and heals the tests, each
part as its own helper (you see them in the Subagents line) with its own budget of model calls, and
ends with a report. Run it again after a stop and it carries on where it stopped: an area with a plan
or tests doesn't get them again. Check the result with `/playwright run`.

`plan` and `generate` without a name take the next area themselves; give a name to redo one
(`/playwright plan Orders`). `/playwright run Orders` runs one area.

To have Playwright start the app for you: `/playwright setup http://localhost:5080 -- npm run dev`
(write the command as you would type it in the project folder).

## Which agent does each step

The chat's **Agent** dropdown has a **Playwright** heading with one agent per job, and each
`/playwright` step switches to the right one by itself, then puts your own choice back:

| Step | Runs as | Tools it gets |
|---|---|---|
| `map` | `playwright-mapper` | read, browse the app, write the site map |
| `plan` | `playwright-planner` | read, browse the app, write plans |
| `generate` | `playwright-generator` | read, browse, write and edit tests, list them |
| `run` | `playwright-runner` | read, browse, run the tests |
| `heal` | `playwright-healer` | read, browse, edit tests, run them |

While a step runs you see its agent in the dropdown (and a note, "This step runs as the
playwright-planner agent"); when it ends the agent you had before is back, so the next question
isn't answered by a planner that can't edit your files. `setup` and `all` stay on the agent you
chose (`all` starts the five roles itself, each with its own budget). The choice follows the
command you type, not the words of a free-text question: to ask something of one of these agents
yourself, pick it in the dropdown. A file of your own with the same name in `.cmcoder/agents/`
replaces the built-in role (below).

## You need

- **Node.js** on this PC (the people who use the app don't need it). `/playwright` says so if it
  is missing.
- **Microsoft Edge or Google Chrome** (Windows always has Edge): the tests use it, nothing is
  downloaded.
- Access to npm's registry once, for `@playwright/test` (setup asks before it installs).

## What it does with a failing test

`run` and `heal` decide, for each failure, who is wrong:

- **The test** (it can't find an element that is there, doesn't wait, expects too much): `heal`
  fixes the test.
- **The app** (an error page, a console error, a broken link, wrong data): `heal` does **not** hide
  it. The test becomes `test.fixme('...')` with a `// APP: what happens instead` comment, and the
  report lists it (page, steps, expected, actual). `/playwright` counts these.

`/playwright` never changes your app, and stays in `playwright-tests/`. To fix what it found, ask
cmcoder in the chat as usual ("fix the problems the Playwright run listed").

## Files

```
playwright-tests/
  playwright.config.ts   address, browser, (optional) how to start the app
  specs/site-map.md      the pages, by area            (map)
  specs/<area>.md        the test plan of an area      (plan)
  tests/seed.spec.ts     the starting point; log in here if the app needs it
  tests/<area>/*.spec.ts the tests                     (generate)
```

Commit the folder; `node_modules/` and the reports are ignored. Run the tests yourself with
`npm --prefix playwright-tests test`.

## Good to know

- **Questions.** Writing inside `playwright-tests/` and looking at your app need no question. Running
  the tests (`npm test`) runs the folder's own scripts, so cmcoder allows it unasked only in a
  project you trust (`cmcoder trust`, or the IDE's trust setting); otherwise it asks each time.
- **Long jobs.** Each step is one request. If one stops with "Stopped after 50 model calls", click
  **Continue** or type `/turns 200`.
- **Where it looks.** The steps use cmcoder's browser tool (in VS Code, VS Code's own browser),
  which can't hover, scroll or enter iframes; the tests themselves can.
- **Settings** (`playwright` in `~/.cmcoder/settings.json`): `dir` (folder name), `appUrl`,
  `maxHealRounds` (3), `enabled`.
- **Playwright's own agents** (planner, generator, healer from `npx playwright init-agents`) still
  work, if you prefer them: `/playwright agents` copies them from `.claude/agents` to
  `.cmcoder/agents` with `model: inherit`, and [playwright-agents.md](playwright-agents.md) says
  the rest. Your own `.cmcoder/agents/playwright-planner.md`, `playwright-generator.md`,
  `playwright-runner.md`, `playwright-mapper.md` or `playwright-healer.md` replace the built-in
  helper of that name in `/playwright all`.
