# cmcoder quick reference: remove the old install, build, install

For a repository folder with **no git** (a download or a copy). Commands are Windows
PowerShell; the macOS/Linux versions are noted where they differ. Run everything from the
repository folder (the one with `pyproject.toml`).

| | Build machine (you) | Users' machines |
|---|---|---|
| Needs | Python is **not** needed (uv brings its own), uv, Node.js 22, Visual Studio 2022 (for the Visual Studio extension only) | **No Python, uv, Node.js or admin rights** |
| Gets | the scripts below | one file per package, each with cmcoder inside |

---

## 1. Your dev machine (Python + uv): remove the old install

Do the lines that apply; skipping one that doesn't apply is harmless.

**Close first:** VS Code, Visual Studio and any terminal running cmcoder (a running program
can't be replaced). Then:

```powershell
Get-Process cmcoder -ErrorAction SilentlyContinue | Stop-Process -Force
```

| Old install | Remove it |
|---|---|
| Terminal package (`%USERPROFILE%\.cmcoder\bin`, also the copy the IDE extensions make) | `powershell -ExecutionPolicy Bypass -File packaging\terminal\uninstall.ps1` (removes the folder and its PATH entry). macOS/Linux: `sh packaging/terminal/uninstall.sh` |
| VS Code extension | `code --uninstall-extension cmcoder.cmcoder`, then reload VS Code. (Or Extensions → cmcoder → Uninstall) |
| Visual Studio extension | Extensions → Manage Extensions → Installed → cmcoder → Uninstall, then close Visual Studio to finish. (Or `VSIXInstaller.exe /quiet /uninstall:cmcoder.visualstudio.6b1f4c0e`, in `Common7\IDE` of the Visual Studio folder) |
| Source install (`uv sync`) | `Remove-Item -Recurse -Force .venv` in the repository folder |
| `uv tool install` or `pip install` copy | `uv tool uninstall cmcoder` or `pip uninstall cmcoder` (only if you ever installed it that way) |
| Old build output in the folder | `Remove-Item -Recurse -Force dist, build -ErrorAction SilentlyContinue` |

**Settings, conversations and the API key are kept** by all of the above
(`%USERPROFILE%\.cmcoder`, Windows Credential Manager). For a completely clean start:

```powershell
uv run cmcoder logout                                   # deletes the stored API key (use --provider NAME for others)
Remove-Item -Recurse -Force "$env:USERPROFILE\.cmcoder"  # settings AND saved conversations
```

### Run from source (dev)

```powershell
uv sync
uv run pytest -q          # optional
uv run cmcoder login      # API key -> keychain
uv run cmcoder doctor     # gateway, TLS, key, tool calling
uv run cmcoder            # interactive; -c continues, -p "…" runs once
```

Your gateway goes in `%USERPROFILE%\.cmcoder\settings.json` (or run `uv run cmcoder setup`).

---

## 2. Build the packages for users (standalone, no Python needed on their PCs)

**Build each platform's files on that platform** (Windows files on Windows; macOS files on an
Apple-silicon Mac; Linux files on Linux x64). The Visual Studio extension is Windows only.

### Build machine needs

| Tool | For | Check |
|---|---|---|
| uv | every package | `uv --version` |
| Node.js 22 LTS | VS Code and Visual Studio packages | `node --version` |
| Visual Studio 2022 + the "Visual Studio extension development" workload, or Build Tools with MSBuild and the .NET Framework 4.8 targeting pack | Visual Studio package only | the script checks and says what's missing |
| Internet: PyPI, npm, nuget.org | the build's packages | behind a proxy set `HTTPS_PROXY` |

Git is **not** needed (see the next step).

### No git: name the build yourself

```powershell
$env:CMCODER_BUILD_ID = "2026-11-r1"     # letters, digits, . _ - ; at most 24
```

Set it in the same PowerShell window before any build script. The scripts then never start
git, and `cmcoder --version` shows `0.2.0 (2026-11-r1 <date>)`. Tests still run. (macOS/Linux:
`export CMCODER_BUILD_ID=2026-11-r1`.)

### Build (one command)

```powershell
# Windows: all three, cmcoder is built once
powershell -ExecutionPolicy Bypass -File packaging\package-all.ps1 -Only terminal,vscode,visualstudio
```

```bash
# macOS / Linux: the CLI and VS Code
bash packaging/package-all.sh --only terminal,vscode
```

Or one at a time:

| Package | Windows | macOS / Linux | Output |
|---|---|---|---|
| CLI (terminal) | `packaging\package-terminal.ps1` | `packaging/package-terminal.sh` | `dist\cmcoder-terminal-win32-x64.zip` |
| VS Code | `packaging\package-vscode.ps1` | `packaging/package-vscode.sh` | `vscode\cmcoder-win32-x64.vsix` |
| Visual Studio | `packaging\package-visualstudio.ps1` | n/a | `dist\cmcoder-visualstudio-win32-x64.vsix` |

(Run the `.ps1` ones as `powershell -ExecutionPolicy Bypass -File …`, the `.sh` ones with `bash`.
File names say `darwin-arm64` or `linux-x64` on those platforms.)

Each script: checks the tools, builds the standalone cmcoder (PyInstaller), runs it with no
Python in reach, builds the chat page, packs the file, checks cmcoder is inside, and prints the
**file path and SHA256**. Keep the SHA256 and hand it out with the file.

Options: `-SkipTests` (try-outs only, never for a release), `-UseBuilt` (reuse `dist\cmcoder`
from an earlier run). The Visual Studio script ends with `cmcoder inside: True`.

**Hand out only these files.** Never `vscode\cmcoder.vsix` (a test build without cmcoder inside).

**After copying in a newer version of the repository, rebuild and reinstall.** The engine is
bundled in each file, so fixes only reach users through a new build.

---

## 3. Install on regular users' machines

### They need

- **Windows:** Git for Windows (cmcoder runs shell commands in Git Bash), or `CMCODER_GIT_BASH_PATH`
  pointing at another `bash.exe`.
- Network access to your AI gateway (often the VPN).
- VS Code **1.90+** for the VS Code file; Visual Studio **2022 17.10+** and the Edge WebView2
  Runtime (part of Windows 11) for the Visual Studio file.
- **Not** needed: Python, uv, Node.js, administrator rights.

The files aren't code-signed, so SmartScreen or antivirus may warn. Tell users, or ask IT to
allow `cmcoder.exe`.

### CLI

1. Unzip `cmcoder-terminal-win32-x64.zip` anywhere.
2. Double-click **`install.cmd`** (macOS/Linux: `sh install.sh`). It installs into
   `%USERPROFILE%\.cmcoder\bin` and adds it to the user's PATH.
3. Open a **new** terminal: `cmcoder doctor`.
4. First time: `cmcoder setup` (gateway, API key, models).

### VS Code

1. `code --install-extension cmcoder-win32-x64.vsix --force`
   (or Extensions → **⋯** → **Install from VSIX…**).
2. Reload VS Code. The cmcoder icon appears in the Activity Bar.
3. Open a folder, open the chat. First time: `cmcoder setup` in a terminal
   (`"%USERPROFILE%\.cmcoder\bin\cmcoder.exe" setup` if the CLI isn't installed).

### Visual Studio

1. Close Visual Studio. Double-click `cmcoder-visualstudio-win32-x64.vsix` → **Install**
   (confirm the "unsigned" notice).
2. Start Visual Studio, open a solution or folder: **Tools → cmcoder → Open Chat**
   (**Ctrl+Shift+Alt+C**).
3. First time: `"%USERPROFILE%\.cmcoder\bin\cmcoder.exe" setup` in a terminal.

To skip the setup step for everyone, put the gateway and models in
`branding\settings.template.json` before building (optional; see
[build-package/README.md](build-package/README.md)).

### Check it worked

| Where | Look at |
|---|---|
| CLI | `cmcoder --version` shows your build name (not `unknown`); `cmcoder doctor` is green |
| VS Code | Command Palette → **cmcoder: Show Log** (which cmcoder started) |
| Visual Studio | **Tools → cmcoder → Copy Diagnostics** (bundled program and version, no API key) |

### Update and remove (for users)

| | Update | Remove |
|---|---|---|
| CLI | close terminals and IDEs, run the new `install.cmd` | `uninstall.cmd` (macOS/Linux: `sh uninstall.sh`) |
| VS Code | install the new `.vsix` the same way | Extensions → cmcoder → Uninstall |
| Visual Studio | close Visual Studio, install the new file | Extensions → Manage Extensions → Installed → cmcoder → Uninstall |

Settings, keys and conversations stay in `%USERPROFILE%\.cmcoder`. The IDE extensions share the
copy of cmcoder in `.cmcoder\bin`, so remove that folder (CLI uninstall) only when nothing else
uses it.

### For IT (scripted)

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Destination D:\Tools\cmcoder        # CLI, other folder
code --install-extension \\share\cmcoder\cmcoder-win32-x64.vsix --force                    # VS Code
VSIXInstaller.exe /quiet \\share\cmcoder\cmcoder-visualstudio-win32-x64.vsix               # Visual Studio
```

Run each as the user who will use it (not "as administrator" for someone else).

---

## 4. If something fails

| Message or symptom | Fix |
|---|---|
| "Needed a single revision", "dubious ownership", "not a git repository" | set `$env:CMCODER_BUILD_ID = "…"` in this window and run the script again |
| "running scripts is disabled" | start scripts as `powershell -ExecutionPolicy Bypass -File …` |
| "Node.js 22 or newer is needed" | install Node.js 22 LTS, open a **new** PowerShell |
| "No Visual Studio on this PC" | install Visual Studio 2022 with the extension-development workload (or Build Tools), see section 2 |
| `.NET Framework 4.8 targeting pack` missing | Visual Studio Installer → Modify → Individual components |
| `cmcoder inside: False` | use `package-visualstudio.ps1` (it copies a fresh cmcoder in); by hand, copy `dist\cmcoder` into `visualstudio\src\Cmcoder.VisualStudio\bin\cmcoder` before `build-vsix.ps1` |
| `test_standalone.py` fails ("started Python") | don't hand the file out; keep the output and report it |
| "cmcoder is running from …" on install or uninstall | close every terminal and IDE using cmcoder, then try again |
| npm or NuGet errors | the build machine needs npm and nuget.org (or your mirrors); set `HTTPS_PROXY` |
| Antivirus removes `dist\cmcoder` | exclude the build folder on the build machine |

More detail per package: [build-package/](build-package/README.md)
([cli](build-package/cli.md), [vscode](build-package/vscode.md),
[visualstudio](build-package/visualstudio.md)); all packages with checks and checksums:
[build-package/release.md](build-package/release.md).
