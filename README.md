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
- Status: **Phases 0–2 done**: agent loop, tools, permissions with
  human-in-the-loop interrupts, Qwen3 reasoning and text tool calls, saved
  sessions (`--continue`, `--resume`), auto-compaction, `/rewind`, todos, slash
  commands, `-p` mode, REPL, `doctor`, `setup`, and the IDE protocol
  (`--protocol stdio`) with the VS Code, Visual Studio, Eclipse, NetBeans and
  JetBrains plugins. Next: extensibility (Phase 3: MCP, hooks, subagents).

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

## Layout

| Folder | What |
|---|---|
| `src/cmcoder/models/` | `CmcoderChatModel` (ChatOpenAI for Qwen3/LiteLLM), reasoning split, error hints |
| `src/cmcoder/agents/` | `build_agent()`: `create_agent` with cmcoder's middleware stack |
| `src/cmcoder/middleware/` | Permissions (interrupts), compaction, context budget and window learning, text tool calls, file snapshots, tool-call repair, loop guard |
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
