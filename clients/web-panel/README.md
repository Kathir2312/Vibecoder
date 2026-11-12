# The shared chat panel

cmcoder's chat panel and Agent Navigator, the same files in every IDE (VS Code,
Visual Studio, Eclipse, NetBeans). The IDE side starts `cmcoder --protocol
stdio`, shows these pages in its embedded browser and relays messages between
them; the pages never talk to cmcoder directly.

```
npm ci            # installs marked (Markdown), esbuild, TypeScript, Playwright
npm run build     # -> dist/
npm test          # every test once per way an IDE connects (needs a Chromium:
                  #    npx playwright install chromium)
npm run generate  # src/protocol.ts from cmcoder's protocol (CI checks it's current)
```

(The VS Code extension's `npm ci` installs this package too, and its build
copies `dist/` into the extension.)

## Files an IDE ships (from `dist/`)

| File | What |
|---|---|
| `chat.js`, `chat.css` | the chat panel (a tool window / side panel) |
| `navigator.js`, `navigator.css` | the Agent Navigator (an editor tab) |
| `tasks.js`, `tasks.css` | the Tasks tab (an editor tab): the agent's whole todo list |
| `test-driver.js` | **tests only, never shipped**: see below |

Each page is this HTML, served from the plugin itself (never `file://` from a
folder others can write, never remote):

```html
<!DOCTYPE html>
<html lang="en"><head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src SOURCE; img-src SOURCE data:; script-src 'nonce-NONCE'">
  <link href="SOURCE/chat.css" rel="stylesheet">
</head>
<body data-product="cmcoder" data-icon="SOURCE/icon.png" data-host="function">
  <div id="app"></div>
  <script nonce="NONCE" src="SOURCE/chat.js"></script>
</body></html>
```

`SOURCE` is where the IDE serves the plugin's files; `NONCE` a new random value
per page. `data-product` is the brand name (`branding/brand.json`),
`data-icon` the brand icon's address; `data-host="function"` only where the
IDE adds `cmcoderHostPost` (Eclipse, NetBeans).

`data-features` lists what the IDE side does beyond the base messages, separated by
spaces; an IDE that leaves it out gets the base panel:

| Feature | The IDE forwards | The panel then shows |
|---|---|---|
| `stopTool` | `{"kind": "stopTool", "toolUseId"}` as a `stop_tool` message | **Stop this command** on a running Bash card |
| `sessionRules` | `rememberSession: true` of a `permission` message as `remember_session` | **For this session** under **Always allow…** |
| `agents` | `{"kind": "setAgent", "agent": name \| null}` as a `set_agent` message | the **Agent** dropdown above the input: the agents in `agent_list`, a project's untrusted ones greyed out; choosing one makes the whole conversation that agent |
| `tasksTab` | `{"kind": "openTasks"}`: open an editor tab with the page `tasks.js`/`tasks.css`, send it `{"kind": "todos", "todos": [...]}` on every `todo_update` event (and again when it says `ready`), `{"kind": "reset"}` for a new conversation; `{"kind": "openChat"}` from it means "show the chat" | **↗** at the end of the Tasks line, which opens the tab (the line itself opens the list inside the chat, with or without the feature) |

A control also needs cmcoder to have the feature (`system_init.features`), so an
older cmcoder with a newer IDE, or the other way round, just shows less.

## The chat's layout

From top to bottom: the header, **the dock**, the conversation, the progress line, the input.
Nothing scrolls the page: the conversation and each open list have their own scrollbar.

- **The dock** (`src/chat/dock.ts`) holds what the agent keeps track of: its tasks, its
  subagents. Each panel is one line (a title, a summary, buttons) that opens to a list. Open lists
  take what their content needs, up to a third of the window between them (`chat.css`), and the
  conversation keeps at least 28% of it. Scrolling the conversation down folds every panel
  (the wheel, the keys, the scrollbar; output that scrolls by itself doesn't count); a panel the
  user folded doesn't open by itself again in that conversation.
- **A new kind of information** goes into the dock as one more panel, not into a new strip of
  its own: `const p = dock.add("id", "Title", { onChange, hint })` in `src/chat/main.ts`, then
  fill `p.summary` (the line), `p.actions` (buttons) and `p.body` (the list), `p.show(true)`
  when there is something to show, and `p.autoOpen()` if it should open by itself. Sharing the
  height and folding on scroll then work for it too.
- **The conversation** follows new output until the user scrolls up; **↓ Latest** resumes it.
- **Starters** (`updateStarters` in `src/chat/main.ts`): on an empty conversation, a row of buttons for
  ready-made commands (now the `/playwright` ones), shown only when `command_list` has the command; the
  panel asks for the list once after `system_init` (`listCommands`). A button fills the input, or sends
  the command when it needs nothing.
- **Agent dropdown** (`renderAgentSelect`): agents are grouped by origin (This project, Yours, Built in);
  an agent with its own `group` (the Playwright roles) gets that heading instead. `agent_changed` carries a
  `reason`: `""` is the user's choice (a note), `"step"` a command switching agent for one request (a note
  saying why), `"restore"` the switch back afterwards (silent, the dropdown just moves).
- **Pick element** (`updatePick`): the button next to Image sends `/pick` as a message (the engine does the
  rest: `src/cmcoder/runtime/pick.py`), shown when `command_list` has `pick`, disabled during a request. The
  element is not a panel concept: it waits in the engine's session and joins the next message as a note.
- **Design tokens** (top of `chat.css`): `--cm-space-1…6`, `--cm-radius-s/m/l`, and `--cm-ok` / `--cm-err`, the
  theme's green and red mixed toward the text colour so small text keeps its contrast. Colours otherwise
  stay the IDE's own (`theme.json`). Use a token, not a new pixel value, in new CSS.
- **Accessibility** (`test/a11y.test.ts`): axe-core runs over eight states of the panel in the dark and the light
  values of `theme.json` and must find nothing, so a new colour or control that fails it fails the build. The
  panel is one `main` landmark with one `h1` and a `log` region; a screen reader is told (an `announcer`) when work
  starts, when a question needs an answer and when it stops, and the streamed text is held back with `aria-busy`
  until it is whole. Anything clickable that is not a `<button>` goes through `activatable()` so the keyboard
  works. Keyboard focus is never moved by the panel itself: a question arriving while you type must not take
  your next keystroke.
- **The input** is as tall as its text, from two lines to about a quarter of the window.

## Messages

The types are in `src/messages.ts` (`ToWebview`/`FromWebview` for the chat,
`ToNavigator`/`FromNavigator` for the navigator); protocol events from cmcoder
are passed in unchanged inside `{"kind": "event", "event": ...}`.

- **To the page**: run `window.postMessage(<message>, "*")` in it (VS Code does
  this itself).
- **From the page** (`src/bridge.ts` picks the first that exists):

| IDE | The page calls |
|---|---|
| VS Code | `acquireVsCodeApi().postMessage(message)` |
| Visual Studio (WebView2) | `window.chrome.webview.postMessage(message)`: the IDE gets it in `WebMessageReceived` |
| Eclipse (SWT), NetBeans (JavaFX) | `window.cmcoderHostPost(jsonText)`: a function the IDE adds (`BrowserFunction`; in JavaFX an `alert()` with a per-load secret) |

Such an IDE also puts `data-host="function"` on `<body>`, so the page uses the
function even where `window.chrome.webview` exists (SWT's browser on Windows is
WebView2). The function can be added after the page loads: messages wait in the page
until it exists (the first is `{"kind": "ready"}`, which tells the IDE to send
the state). The IDE must accept messages only from its own page.

## Theme

`theme.json` lists every CSS variable the pages use (VS Code's names, which
VS Code sets itself), what each is for, and a dark and a light default. Other
IDEs set each one on `:root` from their own theme (from script, e.g.
`document.documentElement.style.setProperty(name, value)`), and again when the
theme changes. A test fails if the pages use a variable that isn't listed.

## Security rules (every IDE)

- Only the plugin's own files; no remote content, no `eval`, no inline scripts
  (the policy above). Model output is Markdown rendered after escaping and an
  allowlist of tags; links are never followed by the page: it sends
  `openLink` and the IDE opens only `http(s)` addresses in the system browser.
- Navigation away from the page, new windows, and (in release builds) dev
  tools and the default context menu are blocked by the IDE.

## Testing an IDE's panel (`test-driver.js`)

Test builds load `dist/test-driver.js` after `chat.js`. It adds
`window.__cmcoderTest.run(action, selector, value?)`, which returns JSON text,
so a test can drive the real panel through the embedded browser's "run
JavaScript" call (JavaFX `executeScript`, SWT `Browser.evaluate`, WebView2
`ExecuteScriptAsync`):

| Action | Returns |
|---|---|
| `text`, `texts` | the text of the first / every match |
| `count`, `visible`, `checked` | number, true/false |
| `click` | true if it clicked (visible and enabled) |
| `fill` (value), `press` (key) | true |
| `style` (CSS property) | the computed value (theme checks) |
