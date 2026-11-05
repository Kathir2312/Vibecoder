# Testing a web app with cmcoder's browser

cmcoder can open your web app in a real browser, look at the page, click and type, and
report what is broken, like a person testing by hand. It is the `Browser` tool, built on
[Playwright](https://playwright.dev/python/).

## What you need

- **Google Chrome or Microsoft Edge** on the PC. Windows always has Edge. cmcoder uses it
  as it is: nothing is downloaded.
- To use another browser program, set it in `~/.cmcoder/settings.json`:

  ```json
  { "browser": { "executablePath": "C:\\Tools\\chrome\\chrome.exe" } }
  ```

`cmcoder doctor` shows which browser it found. To turn the tool off: `"browser": {"enabled": false}`.

## Use it

Ask in plain words, for example:

> Start the app with `npm run dev`, open it, add a todo called "Buy milk" and check it shows up.

cmcoder starts the server (Bash, in the background), opens `http://localhost:3000`, reads the
page, fills the form, clicks Add, and checks the page and the console. It stops with a
report of what worked and what failed (JavaScript errors, failed requests, 404s).

With `"browser": {"headless": false}` you can watch the window.

## In VS Code: the browser opens inside VS Code

VS Code has its own **Integrated Browser** (a browser tab inside the editor) and tools to drive
it: the ones GitHub Copilot's agent uses. When the cmcoder extension finds them, cmcoder uses
**those instead of starting Chrome or Edge**: the page opens in a VS Code tab and you watch it
there; its own `Browser` tool steps aside. The tools are cmcoder's `ide_…` tools (for example
`ide_open_browser_page`); opening a page, clicking and typing ask first like any command, and
reading a page or taking a screenshot does not. Screenshots appear in the chat as before.

- **Needs** VS Code 1.110 or newer with the setting `workbench.browser.enableChatTools` on (VS
  Code's default). Without the tools cmcoder opens its own browser window as before; the
  extension's log (**cmcoder: Show Log**) says which it found.
- **Look at what your VS Code offers:** the command **cmcoder: Show the IDE's Browser Tools**
  lists the tools and their inputs. If your VS Code names them differently from what the
  extension expects, put the names in the setting `cmcoder.integratedBrowserTools`.
- **Turn it off:** the setting `cmcoder.useIntegratedBrowser` (VS Code) or
  `"browser": {"useIdeBrowser": false}` (settings file): cmcoder then uses its own browser.
- VS Code only: Visual Studio, Eclipse and NetBeans have no browser of this kind, and keep
  the separate window.
- The tool names and inputs are VS Code's, and VS Code may change them: cmcoder passes on
  whatever the tools say about themselves.

## `/webtest`: test the app in one command

```
/webtest                          find how to start the app, start it, test its main flows
/webtest add a todo and tick it   the same, testing this
/webtest http://localhost:3000    the app already runs there: just test it
```

cmcoder looks for the start command (the `dev`, `start`, `serve` and `preview` scripts in
`package.json`, `manage.py`) and tells the model. The model starts the server in the
background with Bash (which asks, as usual), waits until it answers, tests it in the browser,
takes screenshots, stops the server it started, and reports what worked and what failed, with
where in the code the cause probably is. It doesn't change any file unless you ask afterwards.

## Screenshots in the chat

The `screenshot` action shows the picture in the chat panel (VS Code, Visual Studio, Eclipse,
NetBeans, IntelliJ), small under the tool's line; click it to enlarge. The model never receives
the picture (it may not see images): it reads pages with `snapshot`. In the terminal, the
tool's answer gives the file's path while the session is open. The files are deleted when the
browser closes.

## What the agent can do

| Action | Does |
|---|---|
| `open` | go to an address |
| `snapshot` | read the page as text, with its buttons, links and fields numbered |
| `click`, `type`, `press` | act on a numbered element, or press a key |
| `wait_for` | wait until some text appears |
| `console` | JavaScript errors, warnings and failed requests since the last check |
| `screenshot` | a picture for you, shown in the chat (the model reads pages with `snapshot`) |

## Safety

- **Localhost opens freely.** `localhost`, `127.0.0.1` and `[::1]` need no question.
- **Any other site asks first**, once per site. "Always allow" saves a rule for that one
  site only, e.g. `Browser(docs.example.com)`. You can also write the rule yourself in
  `permissions.allow` or `permissions.deny`.
- **A page can't lead the agent somewhere else.** A link to a site you haven't allowed isn't
  clicked (the page keeps its state), and a navigation the page makes by itself is blocked; either
  way the agent is told. It can ask to `open` that site, which asks you.
- **A clean browser every session**: no cookies, no saved logins, no downloads, no camera or
  location prompts. Nothing is kept after the session.
- **Page text is data, not orders.** Every snapshot says so to the model.
- **Plan mode** doesn't allow the browser; `bypassPermissions` skips the questions.
- Only `http`, `https` and project files open. `javascript:`, `chrome:` and the like are refused.

## If it doesn't start

| You see | Do |
|---|---|
| *No browser found* | Install Chrome or Edge, or set `browser.executablePath` |
| *The browser did not start* | Run `cmcoder doctor`; on Linux in a container the browser needs its usual system libraries |
| The page works by hand but not here | Ask the agent for a `console` check; it lists errors and failed requests |

## For IT: what the standalone program contains

- **Playwright and its Node.js driver are inside the program** (`packaging/build.py` collects
  them), so a PC needs no Python, Node.js, npm or download. Playwright starts its own copy of
  Node to talk to the browser; nothing else is installed.
- **The browser is not inside.** cmcoder uses the PC's Chrome or Edge, so there is no 150 MB
  Chromium to ship or update, and the browser gets the company's normal security updates.
- **Size** (measured on a Linux build): the unpacked folder grows from 160 MB to 299 MB
  (the Node driver is 122 MB of it) and the release zip from 65 MB to 112 MB (+47 MB).
  Startup is unchanged (about 0.6 s), because Playwright is loaded only when the browser is
  first used.
- **A slim build without it:** `uv run --with pyinstaller python packaging/build.py --without-browser`.
  The Browser tool is then not offered to the model, and `cmcoder doctor` says so.
- **Checked in the release build:** `packaging/smoke_browser.py <program>` runs the built
  program with Python and uv hidden. A scripted model opens a local page, reads it and clicks a
  button through the bundled Playwright, in the runner's Chrome or Edge.
