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

## What the agent can do

| Action | Does |
|---|---|
| `open` | go to an address |
| `snapshot` | read the page as text, with its buttons, links and fields numbered |
| `click`, `type`, `press` | act on a numbered element, or press a key |
| `wait_for` | wait until some text appears |
| `console` | JavaScript errors, warnings and failed requests since the last check |
| `screenshot` | save a picture (for you; the model reads pages with `snapshot`) |

## Safety

- **Localhost opens freely.** `localhost`, `127.0.0.1` and `[::1]` need no question.
- **Any other site asks first**, once per site. "Always allow" saves a rule for that one
  site only, e.g. `Browser(docs.example.com)`. You can also write the rule yourself in
  `permissions.allow` or `permissions.deny`.
- **A page can't lead the agent somewhere else.** A click that goes to a site you haven't
  allowed is blocked, and the agent is told. It can ask to `open` that site, which asks you.
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
