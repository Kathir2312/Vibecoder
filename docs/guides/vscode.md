# cmcoder in VS Code: install and set up

For developers. You need **VS Code 1.90 or newer** and the file `cmcoder-<platform>.vsix`
that your team gives you (`cmcoder-win32-x64.vsix` on Windows). No Python, Node.js or
administrator rights are needed: VS Code installs extensions for you alone.
On Windows, install [Git for Windows](https://git-scm.com/download/win) too (cmcoder's
Bash tool uses it).

> **About the pictures.** The four screenshots below are the real cmcoder chat panel,
> showing what cmcoder itself sent for these steps, with VS Code's dark colours. They
> are not photographs of VS Code's own windows (Extensions view, file picker), which look
> a little different between VS Code versions; those steps are described in words.

## 1. Check the file (optional, 30 seconds)

Your team gives you the file's SHA256 (the Release build prints it and lists it in the
checksums file). Compare:

```powershell
Get-FileHash .\cmcoder-win32-x64.vsix -Algorithm SHA256
```

If it differs, don't install it: ask your team for the file again.

A file from a download or a zip may be marked "from the internet", which Windows can
use to block or warn. Remove the mark once, after the check above:

```powershell
Unblock-File .\cmcoder-win32-x64.vsix
```

## 2. Install: do not double-click the file

**The .vsix is not signed, and double-clicking it can fail.** On a PC that also has
Visual Studio, Windows often hands `.vsix` files to Visual Studio's own *VSIX
Installer*, which is for Visual Studio extensions and rejects this one with an error.
Install from inside VS Code instead: it installs a `.vsix` file you give it directly,
and doesn't need cmcoder's file to be signed (extension signatures are a Marketplace
matter, and cmcoder isn't distributed through the Marketplace).

**Way A: from the Extensions view (point and click)**

1. Open VS Code.
2. Press **Ctrl+Shift+X** to open the **Extensions** view.
3. Click the **`…`** (*Views and More Actions*) at the top right of that view.
4. Choose **Install from VSIX…**
5. Pick `cmcoder-win32-x64.vsix` and click **Install**.
6. A notification says the extension was installed. If it offers **Reload**, click it.

**Way B: from the Command Palette**

Press **Ctrl+Shift+P**, type `Extensions: Install from VSIX…`, press Enter and pick the file.

**Way C: from a terminal**

```powershell
code --install-extension "C:\path\to\cmcoder-win32-x64.vsix"
```

If `code` isn't found, use its full path:
`"$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd"`.
(On macOS or Linux, use the file for your platform: `cmcoder-darwin-arm64.vsix` or
`cmcoder-linux-x64.vsix`.)

<!-- Add your own screenshots here when you take them (see the team's notes):
![Install from VSIX in the Extensions view](img/01-install-from-vsix.png)
![The extension in the Extensions list](img/02-extension-installed.png)
-->

To check: the Extensions view lists **cmcoder**, and the activity bar (the icon column
at the left) now has a **cmcoder** icon.

## 3. Open a project and the chat

1. **File → Open Folder…** and choose your project's folder. If VS Code asks whether you
   trust its authors, choose **Yes, I trust the authors** for your own projects.
2. Click the **cmcoder** icon in the activity bar, or press **Ctrl+Alt+K**.

## 4. First start: the gateway and your key

The first time, cmcoder doesn't know your model gateway yet. The panel says so:

![The first start: cmcoder says it isn't set up](img/05-not-set-up.png)

This is expected. Your team may already have set it up for you: some builds put a ready
`~/.cmcoder/settings.json` on first use, and then you only need your API key (step 4b).
Otherwise:

### 4a. The gateway and model

**Quickest:** in a terminal (VS Code's own: **Ctrl+`**) run cmcoder's setup. It asks for
the gateway address, your key and the models, and writes the file for you:

```powershell
& "$env:USERPROFILE\.cmcoder\bin\cmcoder.exe" setup
```

> That path exists after cmcoder has started once from the extension, because it copies
> itself there. If it isn't there yet, run the copy inside the extension instead; the
> folder name starts with your extension's publisher and name:
>
> ```powershell
> $exe = Get-ChildItem "$env:USERPROFILE\.vscode\extensions" -Recurse -Filter cmcoder.exe |
>        Select-Object -First 1 -ExpandProperty FullName
> & $exe setup
> ```

**Or edit the file yourself:** press **Ctrl+Shift+P**, choose **cmcoder: Open Settings
File** and put in your gateway (the example shows the main settings):

```json
{
  "providers": {
    "corp": {
      "baseUrl": "https://ai-gateway.example.com/v1",
      "auth": { "type": "apiKey" },
      "caCertPath": null
    }
  },
  "model": "corp:qwen3-27b",
  "smallFastModel": "corp:qwen3-7b"
}
```

- `baseUrl` is your gateway's OpenAI-compatible address (LiteLLM or Open WebUI).
- `caCertPath`: if your company's gateway uses an internal certificate authority, put the
  path to its `.pem` file here (or install the CA in Windows, which cmcoder also trusts).
- `smallFastModel` is optional: a smaller model for titles and summaries.
- Every setting is listed in `~/.cmcoder/settings.example.json`.

### 4b. Your API key

Store it in the Windows credential store (not in a file), in a terminal:

```powershell
& "$env:USERPROFILE\.cmcoder\bin\cmcoder.exe" login
```

It asks for the key without showing it. (For a scripted setup, the `CMCODER_API_KEY`
environment variable works too.)

### 4c. Check it

```powershell
& "$env:USERPROFILE\.cmcoder\bin\cmcoder.exe" doctor
```

`doctor` checks the gateway address, the certificate, the proxy, your key, the model and
that the model can call tools, and says what to fix. When it is all fine, go back to the
panel and click **Restart** (or reload the window: **Ctrl+Shift+P → Developer: Reload
Window**).

## 5. Use it

The panel is ready: it shows the model's name at the top and the box to type in.

![The panel, ready](img/06-ready.png)

Type a request and press **Enter** (**Shift+Enter** for a new line). For example:
*Fix the bug in add() in app.py*.

cmcoder reads files and, **before it changes anything, asks you**. For an edit the card
shows what would change as a diff, with line numbers and the lines around the change; the
small tag says **why** you are asked:

![cmcoder asks before editing a file](img/09-consent-edit.png)

| Button | Does |
|---|---|
| **Allow** | makes this change, this once |
| **Always allow…** | shows the rule it would add, then asks where it should hold (below) |
| **Deny** | refuses; the turn stops |
| **Deny with a note…** | refuses and tells cmcoder what to do instead |
| **Open in diff editor** | opens VS Code's diff editor for the proposed change |

**Always allow…** never saves anything by surprise. It shows the rule
(for example `Bash(pytest:*)`) and offers **For this session** (until you close the
conversation, nothing is written), **In this project** (saved in the project's settings, as
the CLI's "Always" does) or **Cancel**:

![Always allow shows the rule and where it holds](img/11-always-allow.png)

For a command the card shows the command with its folder; flags, strings and variables are
coloured:

![cmcoder asks before running a command](img/10-consent-command.png)

A command that can do real harm (recursive delete, `sudo`, a download piped into a shell,
a force-push…) is shown in red, with the dangerous part underlined. It can be allowed
**once** only: the rule is never offered, whatever you answered before:

![A high-risk command](img/12-consent-high-risk.png)

If the chat is not in view when cmcoder needs your answer (another side-bar view is open),
VS Code shows a notification with a button that opens it. `cmcoder.notifyWhenHidden` turns
that off.

### Commands that run

Each command cmcoder runs gets its own card: the command, how it ended (exit code and
time) and the end of its output. Output appears **while the command runs**, with
**Stop this command** under it: it ends that command only, and cmcoder carries on with
what the command printed. Long output is folded to its last lines: **Show all** opens it,
**Copy output** copies it.

![A failed, a passed and a stopped command](img/13-bash-card.png)

After you allow it, cmcoder finishes and tells you what it did:

![The change is made and cmcoder reports](img/08-done.png)

### Talk to one of your agents

The **Agent** dropdown above the message box lists the default agent and your own: the files in
`.cmcoder/agents/` of a trusted project, in `~/.cmcoder/agents/`, and the built-in `explore`
(read-only search) and `general-purpose`. Pick one and the **whole conversation is that agent**: its
instructions, the tools its file lists and its model replace the default's, until you pick
**cmcoder (default)** again. A project's agents are greyed out as "(trust this project)" until you
run `cmcoder trust`; the dropdown is disabled while a turn runs. Playwright's planner, generator and
healer work this way: see [playwright-agents.md](playwright-agents.md).

### A long task list

When cmcoder plans a job with many steps, the chat shows **one line**: how many are done
(`3/20`), a small meter and the task it is on. Click it (or the **Tasks** button in the
chat's title bar, or **cmcoder: Open Tasks**) to open a **Tasks** tab beside the chat with the
whole list, numbered and grouped as *In progress*, *To do* and *Done*, following the agent
live. A filter box appears for a long list: type words, or a task's number.

![One line in the chat, the whole list in a tab](img/14-tasks.png)

### Handy things

| Do | How |
|---|---|
| Ask about code | select it, then **Ctrl+Alt+L** (or right-click → **Ask cmcoder About Selection**) |
| Mention a file | the **@** button |
| Stop | **Esc** or **Stop** |
| Earlier conversations | **History** at the top; **Continue Last Conversation** in the view's `…` menu |
| Safer or freer | the mode picker at the top (`default` asks before edits and commands) |
| A second opinion | the **Critique** box makes a reviewer check each answer first |
| Look at the agents | **cmcoder: Open Agent Navigator** |
| Use the terminal version | **cmcoder: Open in Terminal** |
| Search code by meaning | **cmcoder: Set Up Code Search** (needs an embedding model on the gateway) |

## If something goes wrong

| You see | Do |
|---|---|
| Double-click gives an error | Don't double-click: install as in step 2 |
| *No model configured. Run `cmcoder setup`* | Step 4a |
| The panel says it can't reach the gateway, or a certificate error | `cmcoder doctor` (step 4c); the certificate: `caCertPath` (step 4a) |
| *"…" was not found on PATH. Set cmcoder.executable* | Leave `cmcoder.executable` empty (the bundled program is used); remove the setting if you set it |
| Nothing happens after installing | **Ctrl+Shift+P → Developer: Reload Window** |
| Anything else | **Ctrl+Shift+P → cmcoder: Show Log**, and send the log to your team |

## Update or remove

- **Update:** install the new `.vsix` the same way (step 2). Your settings and
  conversations are kept.
- **Remove:** Extensions view → **cmcoder** → **Uninstall**. Your settings and
  conversations stay in `%USERPROFILE%\.cmcoder` until you delete that folder.
