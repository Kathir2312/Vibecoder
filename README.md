# Vibecoder (cmcoder on LangChain + LangGraph)

An agentic coding assistant in the style of Claude Code, rebuilt on
**LangChain v1** (`create_agent` + middleware) and **LangGraph** (checkpoints,
interrupts, streaming). It talks to any **OpenAI-compatible gateway**, such as
LiteLLM or Open WebUI serving Qwen3 models on your network. The agent runs on
your machine; the model runs on the gateway.

This project is also meant for **learning LangChain and LangGraph**: start
with [docs/learn/](docs/learn/README.md). Every engine file explains the
framework concepts it uses.

- Requirements and roadmap: [docs/PLAN.md](docs/PLAN.md)
- Status: **all five phases done**: agent loop, tools, permissions with
  human-in-the-loop interrupts, Qwen3 reasoning and text tool calls, saved
  sessions (`--continue`, `--resume`), auto-compaction, `/rewind`, todos, slash
  commands, `-p` mode, REPL, `doctor`, `setup`, and the IDE protocol
  (`--protocol stdio`) with the VS Code, Visual Studio, Eclipse, NetBeans and
  JetBrains plugins, plus MCP servers, hooks, custom commands, skills,
  subagents (`Task`), the critic (`/critic`) and the team (`/team`: a
  LangGraph `StateGraph` of PM, developer, tester and DevOps), and the
  memory between sessions on a LangGraph store (`/memory`; `/init` learns a
  project and saves notes about it; `/review` checks your changes for bugs; the `Browser` tool drives a real
  browser through Playwright to test web apps (`/webtest` runs a whole test; screenshots appear
  in the chat; **Pick element** / `/pick` sends an element you click on the page to cmcoder), see
  [docs/guides/browser-testing.md](docs/guides/browser-testing.md)), and the
  hardening: Open WebUI as a gateway, the Bash sandbox, images through a
  `visionModel`, OpenTelemetry (off by default), the full `cmcoder doctor`,
  standalone builds and IDE packages, and a [security
  review](docs/SECURITY-REVIEW.md), and code search (Phase 5: LangChain
  `Embeddings` and `VectorStore`s, the `CodeSearch` tool and automatic
  context).

## Quick start

```bash
uv sync
uv run pytest -q
```

Configure your gateway in `~/.cmcoder/settings.json`:

```jsonc
{
  "providers": {
    "corp": {
      "baseUrl": "https://ai-gateway.example.com/v1",
      "caCertPath": "/path/to/corp-root-ca.pem"   // optional; the OS trust store is used first
    }
  },
  "model": "corp:qwen3-27b",
  "smallFastModel": "corp:qwen3-7b"
}
```

Then:

```bash
uv run cmcoder login     # API key -> OS keychain (or set CMCODER_API_KEY)
uv run cmcoder doctor    # gateway, TLS, key, chat and tool calling
uv run cmcoder           # interactive (/help for commands)
uv run cmcoder -c        # continue the latest conversation in this project
uv run cmcoder sessions  # list saved conversations; --resume <id> continues one
uv run cmcoder -p "explain src/cmcoder/agents/main.py" --output-format stream-json
```

## Extending it (Phase 3)

| Feature | Where |
|---|---|
| MCP servers | `mcpServers` in settings, `.mcp.json` in a project (approved before first start), `--mcp-config`; `/mcp` |
| Hooks | `hooks` in settings: PreToolUse, PostToolUse, Stop, SubagentStop, UserPromptSubmit, SessionStart, PreCompact, Notification |
| Custom commands, skills | `.cmcoder/commands/*.md`, `.cmcoder/skills/<name>/SKILL.md` (yours in `~/.cmcoder/`) |
| Subagents | the `Task` tool; your own in `.cmcoder/agents/*.md`; `/agents`. **Talk to one yourself:** the Agent dropdown in the VS Code and Visual Studio chat, or `cmcoder --agent NAME`: the whole conversation is that agent (its instructions, tools, model) |
| Playwright tests | **`/playwright`** sets up Playwright tests for any web app and walks you through them: `setup <address>`, `map` (every page), `plan`, `generate`, `run`, `heal` (fixes tests that are wrong; the app's own problems are listed as `test.fixme`, never hidden). Each step runs as its own agent, picked in the Agent dropdown for you (and put back afterwards). Plain `/playwright` says where you are and what to type next: [docs/guides/playwright-commands.md](docs/guides/playwright-commands.md) |
| Length of a request | `maxTurns` (default 50) is how many model calls one request may use; subagents have `subagentMaxTurns` (100). **`/turns`** shows it and **`/turns 200`** changes it while cmcoder runs (terminal and IDE chats; until the window closes), `--max-turns` sets it at start. A request that uses them all says `Stopped after 50 model calls …` and the IDE chat offers **Continue**; the work is kept and a new request gets a fresh budget |
| When the model fails | An error says what happened in plain words and offers what helps: a gateway that was down or too slow gets **Retry** (or `/retry`), which runs the failed step again from LangGraph's checkpoint (the message is not sent twice, finished tool work is not redone) and **Switch model**; a conversation that is too long gets **Compact** and **New conversation**; a refused key gets neither retry nor switch, only the fix (`cmcoder login`) |
| Planning before changing | In **plan** mode (the mode list, or `--permission-mode plan`) cmcoder only looks. When it knows what to do it shows its plan as a card (title, numbered steps, the files each touches) and waits: **Approve and run**, **Approve, accept edits**, **Edit steps** (change, drop or add lines), **Revise…** (send it back with a note) or **Reject**. Approving ends plan mode and the steps become the task list; nothing is changed before that. In the terminal the plan is printed and answered with 1 or 3 |
| Reviewing what changed | A request that edited files ends with a bar (*3 files changed +47 −3*): **Review changes** lists every file and hunk, with **Revert hunk** and **Revert file** (a file cmcoder created is deleted), and **Undo all** takes the whole request back. cmcoder puts files back from the snapshots it keeps for `/rewind` and never overwrites a file you edited since; shell-made changes aren't tracked. Also `/revert <request> <file> [<hunk>]` (numbers from the list); in the terminal a one-line note says the request changed files |
| Rethink when stuck | on by default (`"rethink": {"enabled": false}` turns it off): three failed tool calls in a row make the agent say what went wrong and try something different; still failing, it stops and reports to you what it tried and what it needs. A reminder to wrap up at 80% of `maxTurns` |
| Critic | `/critic on`, `--critic` or `"critic": {"enabled": true}`: a read-only reviewer checks each answer first |
| Team | `"team": {"enabled": true}`, then `/team <feature>`: PM → developer → tester (↺ developer on a failure, up to `team.maxRounds`) → DevOps. Roles: `pm.md`, `developer.md`, `tester.md`, `devops.md` in `.cmcoder/agents/`, else built-in |

The project's own `.cmcoder/` files are used only in a trusted project
(`--trust-project`, or the IDE's trust setting).

## Gateways, sandbox, images, telemetry, packages (Phase 4)

| What | How |
|---|---|
| Open WebUI as the gateway | `"providers": {"webui": {"type": "openwebui", "baseUrl": "https://webui.corp"}}` with the API key from your Open WebUI account (Settings → Account). Ollama models behind it get cmcoder's context window as `num_ctx` |
| Model limits | Read from LiteLLM's `/model/info` or Open WebUI, else probed once and cached; `cmcoder models` lists what the gateway offers |
| Bash sandbox | On by itself where it can run (bubblewrap on Linux and WSL2, `sandbox-exec` on macOS): writes only in the project and a temp folder, network only through `sandbox.network.allowedHosts` |
| Background jobs | The agent can start a command that keeps running (a dev server, a test watcher) with Bash `run_in_background`, then go on: the `Jobs` tool reads what it printed (and can wait a few seconds for more) or ends it. The chat lists them in a **Jobs** panel under Tasks and Subagents: how long each has run, its last lines, **Stop** (works while a request is running) and **Stop all**. `/jobs`, `/jobs output <n>` and `/jobs stop <n\|all>` do the same by command. Up to 5 run at once; each runs where Bash would (in the sandbox, or outside it if you allowed that), the whole process tree stops with it, and they go on through a `/model` switch or a model fallback and end when cmcoder ends |
| Commit and pull request | `/commit [what it is for]` commits the uncommitted changes: cmcoder collects the diff (secret files left out), the branch, the recent commit subjects and the files that must never be staged, the model writes the message and stages files by name (never `git add -A`), and the command card asks you before `git add` and `git commit` run. `/pr [base]` does the same for the branch: the commits and change since it left the base (default branch, or the one you name), a title and body, `git push -u` (never forced) and `gh pr create` if `gh` is installed, else the page that opens the pull request for GitHub, GitLab and Azure DevOps remotes (a password in a remote address is never sent to the model). The Bash sandbox keeps `.git` read-only, so these commands run outside it, which always asks you; where the sandbox cannot be turned off (managed settings) the model gives you the commands to run in a terminal instead |
| Audit log | `"audit": {"enabled": true}` (or an administrator's managed settings, or `/audit on` for one conversation): one JSON line per event in `~/.cmcoder/audit/audit-YYYY-MM-DD.jsonl` (`audit.dir` moves it): the tool calls and how they ended, every permission question and who answered what, what a rule or mode refused, the files each request changed, model switches, errors and how each request ended. Never in it: file contents, the model's replies, tool output, and (unless `includePrompts`) your prompts; commands, labels and errors have what looks like a key, a token or a password hidden. Appended to, mode 600, readable with `jq`. A repository's settings can neither switch it on or off nor move it. `/audit` shows what it has recorded of this conversation |
| Code intelligence (VS Code) | the agent can ask your language servers where a symbol is defined (`goToDefinition`), what uses it (`findReferences`) and what is called what in the workspace (`workspaceSymbols`): exact, through imports and inheritance, where Grep only matches text. Secret files are never listed; a place outside the project is a path only. Other IDEs do not offer these yet (they need their own language-server calls) |
| Check after edits | `"verify": {"commands": ["npm run build", "npm test"]}`: after a request that changed code (Write/Edit; a Markdown edit or a question does not count) the commands run in the project folder, in the Bash sandbox when there is one, each as a **check** card with live output. The first that fails stops the check and its output goes back to the model, which fixes it (`maxRounds` attempts, default 2; the checks run again after its next edit); still failing after that, the answer stands with a warning. `/verify` runs them now, `/verify off\|on` switches the step for the conversation, `/verify setup` suggests commands from your project files (never run by themselves). A project's own `verify` setting counts only when the project is trusted (`cmcoder trust`) |
| Fallback model | `"fallbackModel": "corp:qwen3-7b"` (or `CMCODER_FALLBACK_MODEL`): when the main model's gateway fails (502/503/504, a server error, a timeout, no connection, rate limiting, a model the gateway does not know) the request goes on with that model from the last checkpoint, the chat says so and labels the model "· fallback", and your next message tries the main model first. A refused key, a spent budget, a refused request and a conversation that is too long are not the gateway's fault: they stop with the error card as before. An agent that names its own `model:` keeps it |
| Images | Pasted images go to a model that sees them; for one that doesn't, set `"visionModel"` (a Qwen-VL) and it describes them first |
| Metrics | `"telemetry": {"enabled": true, "endpoint": "https://otel.corp:4318"}`: OpenTelemetry counts and timings only (off by default) |
| Checks | `cmcoder doctor`: network, TLS, key, every model (window, streaming, thinking, tool calling, images), MCP, hooks, sandbox, telemetry |
| Packages | `uv run --with pyinstaller python packaging/build.py` (standalone, no Python needed), then `packaging/package-all.sh`: see [docs/build-package/](docs/build-package/README.md) |

## Code search (Phase 5)

```bash
uv run cmcoder rag setup   # pick an embedding model on your gateway, and where the index lives
uv run cmcoder index       # build or update this project's index (changed files only)
uv run cmcoder rag status
```

In a session the agent gets the `CodeSearch` tool, and each question of a few
words gets the best-matching code added to it automatically
(`rag.autoContext`). `/index` builds or updates the index from the chat. The
index is built in (`~/.cmcoder/index/`, readable by you only), kept by
LanceDB inside cmcoder for big codebases (`"store": {"type": "lancedb"}`, on
disk, no server; see [docs/guides/code-search-lancedb.md](docs/guides/code-search-lancedb.md)),
or kept on a Chroma server (`"store": {"type": "chroma", "url": "..."}`).

## Layout

| Folder | What |
|---|---|
| `src/cmcoder/models/` | `CmcoderChatModel` (ChatOpenAI for LiteLLM / Open WebUI and Qwen3), the gateway's own endpoints, learned windows, reasoning split, error hints |
| `src/cmcoder/agents/` | `build_agent()`: `create_agent` with cmcoder's middleware stack; subagents; the team `StateGraph` |
| `src/cmcoder/mcp/` | MCP servers through `langchain-mcp-adapters` |
| `src/cmcoder/rag/` | Code search: embeddings, vector stores, chunking, the incremental index |
| `src/cmcoder/middleware/` | Permissions (interrupts), hooks, the critic, compaction, context budget and window learning, text tool calls, file snapshots, tool-call repair, loop guard, rethink when stuck |
| `src/cmcoder/runtime/` | `Session` (streaming, interrupts, compaction, rewind), SQLite session store, slash commands, titles |
| `src/cmcoder/tools/` | Read/Write/Edit/Glob/Grep/Bash and the LangChain adapter (todos: LangChain's `write_todos`) |
| `src/cmcoder/core/`, `config/`, `protocol/`, `sandbox/` | Carried over from cmcoderagent: permission rules, high-risk command checks, settings, IDE protocol |
| `src/cmcoder/cli/` | `cmcoder` command, `-p` mode, REPL, `--protocol stdio` (IDEs), `doctor`, `setup` |
| `vscode/`, `visualstudio/`, `eclipse/`, `netbeans/`, `jetbrains/`, `clients/` | The IDE plugins and their shared chat panel, unchanged from cmcoderagent |
| `tests/`, `evals/` | Unit and end-to-end tests and eval tasks (scripted mock gateway, no GPU or key needed) |
| `packaging/` | The standalone build (PyInstaller): no Python needed on developers' PCs |

Privacy: code and prompts go only to the configured gateway. LangSmith tracing
is off unless you set `CMCODER_ENABLE_LANGSMITH=1` along with LangSmith's own
variables.
