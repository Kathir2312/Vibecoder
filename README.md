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
- Status: **Phase 0 (foundations)**: agent loop, tools, permissions with
  human-in-the-loop interrupts, Qwen3 reasoning handling, `-p` mode, REPL, `doctor`.

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
uv run cmcoder           # interactive
uv run cmcoder -p "explain src/cmcoder/agents/main.py" --output-format stream-json
```

## Layout

| Folder | What |
|---|---|
| `src/cmcoder/models/` | `CmcoderChatModel` (ChatOpenAI for Qwen3/LiteLLM), reasoning split, error hints |
| `src/cmcoder/agents/` | `build_agent()`: `create_agent` with cmcoder's middleware stack |
| `src/cmcoder/middleware/` | Permissions (interrupts), context budget, tool-call repair, loop guard |
| `src/cmcoder/runtime/` | `Session`: streams the graph into protocol events, resumes interrupts |
| `src/cmcoder/tools/` | Read/Write/Edit/Glob/Grep/Bash/TodoWrite and the LangChain adapter |
| `src/cmcoder/core/`, `config/`, `protocol/`, `sandbox/` | Carried over from cmcoderagent: permission rules, high-risk command checks, settings, IDE protocol |
| `src/cmcoder/cli/` | `cmcoder` command, `-p` mode, REPL, `doctor` |
| `tests/` | Unit and end-to-end tests (scripted mock gateway, no GPU or key needed) |

Privacy: code and prompts go only to the configured gateway. LangSmith tracing
is off unless you set `CMCODER_ENABLE_LANGSMITH=1` along with LangSmith's own
variables.
