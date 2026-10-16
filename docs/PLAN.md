# Plan: cmcoder rebuilt on LangChain v1 + LangGraph (in `Vibecoder`)

## Context

`Kathir2312/cmcoderagent` already meets the requirements (Claude Code-style agent for terminal + 6 IDEs, OpenAI-compatible gateways, Qwen3, strict security, no Python on dev PCs). Its engine (`src/cmcoder/core/agent.py`, 1,590 lines, plus compaction, subagents, critic, sessions, MCP, RAG) is hand-written.

Goal: **same requirements, same product name (`cmcoder`, `.cmcoder/`, `CMCODER.md`), same stdio protocol so all IDE clients keep working**, but the engine is rebuilt in `Vibecoder` on **LangChain v1 + LangGraph** using their standard patterns: `create_agent` + middleware, LangGraph checkpointers, `interrupt()`/`Command(resume=…)` for human-in-the-loop, subgraphs for subagents, `StateGraph` for multi-step workflows, LangChain `VectorStore`/`Embeddings`/`BaseTool`/`BaseChatModel` abstractions.

Decisions (from you): new engine, reuse the rest; keep the `cmcoder` name.

## Library baseline (checked on PyPI, 16 Oct 2026; pin exact in `uv.lock`)

| Package | Version | Use |
|---|---|---|
| `langgraph` | 1.2.x | Runtime, `StateGraph`, `interrupt`, `Command`, `Send`, streaming |
| `langchain` | 1.4.x | `create_agent`, built-in middleware |
| `langchain-core` | 1.6.x | Messages, `BaseTool`, `BaseChatModel`, `VectorStore`, `Embeddings` |
| `langchain-openai` | 1.7.x | `ChatOpenAI`/`OpenAIEmbeddings` against LiteLLM / Open WebUI |
| `langgraph-checkpoint-sqlite` | 3.1.x | `AsyncSqliteSaver` sessions, resume, time travel |
| `langchain-mcp-adapters` | 0.3.x | `MultiServerMCPClient` (stdio / streamable HTTP / SSE), `tool_name_prefix`, `tool_interceptors` |
| `langchain-text-splitters` | 1.1.x | `RecursiveCharacterTextSplitter.from_language` for code chunks |
| `deepagents` | 0.7.x (optional, behind our interface) | `SubAgentMiddleware`, `SkillsMiddleware` if they fit; otherwise our own |
| `langsmith` | 0.14.x | **Off by default** (no telemetry rule); OTel export instead |

Kept from today: Python 3.11+, `uv`, `httpx`, `pydantic` v2, `typer`, `textual`+`rich`, `prompt_toolkit`, `truststore`, `keyring`, `pathspec`, ripgrep, `pytest`/`pytest-asyncio`, `ruff`, `pyright`, PyInstaller.

**Not used:** `langchain-chroma` (it pulls in the full `chromadb`, which breaks the standalone build, D17); `langchain-community` (heavy, unpinned transitive deps); LangGraph Platform/Server (agent must run on the dev PC).

## Architecture

```
TUI / REPL / -p / --protocol stdio  (IDE clients: VS Code, VS, Eclipse, NetBeans, JetBrains — unchanged)
            │  same JSON-lines protocol (events.py, messages.py reused)
      cmcoder.runtime.Session  ── maps LangGraph stream → protocol events, Command(resume) ← permission_response
            │
  create_agent(model, tools, system_prompt, middleware=[…], checkpointer=AsyncSqliteSaver, context_schema=RunContext)
            │
 Middleware stack (order matters, outer → inner):
   ManagedPolicy → Hooks(UserPromptSubmit/Stop) → ModelCallLimit/ToolCallLimit
   → ContextBudget (max_tokens sizing) → Summarization (small/fast model, 0.8 threshold)
   → ContextEditing (clear old tool outputs) → PromptedToolFallback + ToolCallRepair (Qwen)
   → ModelRetry/ModelFallback (small, LiteLLM already retries) → TodoList
   → Permission (wrap_tool_call + interrupt) → Hooks(Pre/PostToolUse) → Checkpoint (file snapshots)
   → ToolError → LoopDetection → Critic (after_agent, optional)
            │
 Tools (BaseTool): Read Write Edit MultiEdit Glob Grep Bash WebFetch WebSearch TodoWrite Task CodeSearch + MCP + IDE tools
            │
 CmcoderChatModel (ChatOpenAI subclass) ── httpx client (truststore, caCertPath, proxy, headers, AuthProvider)
            │
 LiteLLM / Open WebUI → Qwen3 27B (main), 7B (small/fast)
```

## Module layout in `Vibecoder`

```
pyproject.toml, uv.lock, CMCODER.md, README.md
src/cmcoder/
  protocol/        COPY  events.py, messages.py, typescript.py (protocol is the IDE contract; unchanged)
  config/          COPY  settings.py (layered + managed settings)
  security/        COPY  core/risk.py, core/readonly.py, core/permissions.py rules engine, sensitive.py, sandbox/
  models/          NEW   chat.py (CmcoderChatModel), profiles.py (port), auth.py (port AuthProvider → httpx.Auth),
                         openwebui.py, errors.py (port classify_http_error), qwen.py (<think>, <tool_call> parsing; port text_tools.py)
  tools/           PORT  each Tool → langchain BaseTool with pydantic args_schema; reuse bodies from tools/files.py,
                         search.py, shell.py, bash.py, todo.py; ToolRuntime gives state/context/stream_writer
  middleware/      NEW   permission.py, hooks.py, context_budget.py, prompted_tools.py, repair.py, loop_detect.py,
                         checkpoints.py, critic.py, managed.py, ide_context.py, auto_context.py (RAG), vision.py
  agents/          NEW   main.py (build_agent factory), subagents.py (Task tool → compiled subgraphs),
                         definitions.py (port .cmcoder/agents/*.md loader), team.py (StateGraph PM→Dev→Tester→DevOps)
  memory/          NEW   CMCODER.md/AGENTS.md loader → system prompt; `#` adds to memory; skills loader
  rag/             PORT  chunker → text_splitters; embed → OpenAIEmbeddings(check_embedding_ctx_length=False);
                         stores → VectorStore subclasses (LocalNumpyStore, ChromaHttpStore over httpx); index.py incremental
  mcp/             NEW   MultiServerMCPClient wrapper, approval of servers, `mcp__<server>__<tool>` naming
  runtime/         NEW   session.py (thread_id = session id, AsyncSqliteSaver), stream.py (LangGraph → protocol events),
                         rewind.py (get_state_history + file snapshots), titles.py, cost.py (usage_metadata)
  cli/             PORT  main.py, repl.py, tui.py, headless.py, stdio.py, doctor.py, setup_cmd.py, rag_cmd.py, mcp_cmd.py
  testing/         PORT  mock_server.py (OpenAI-compatible replay server) + GenericFakeChatModel fixtures
vscode/ visualstudio/ eclipse/ netbeans/ jetbrains/ clients/ branding/ packaging/   COPY as-is
evals/  COPY tasks + runner, add LangGraph trajectory scoring
tests/  port test suite; keep test file names where behaviour is the same
docs/   port; DESIGN.md rewritten for the LangGraph architecture
```

## How each requirement maps to LangChain / LangGraph

| Requirement | Standard pattern | Notes / what we keep custom |
|---|---|---|
| Agent loop | `langchain.agents.create_agent` (a compiled LangGraph) | Replaces `core/agent.py::_run` |
| OpenAI-compatible gateway, Qwen3 | `CmcoderChatModel(ChatOpenAI)` with `base_url`, `http_async_client=httpx.AsyncClient(verify=truststore ctx, proxy…)`, `stream_usage=True` | Override chunk conversion to keep `reasoning_content`/`<think>` as reasoning content blocks, never sent back; `extra_body={"chat_template_kwargs": {"enable_thinking": …}}`; `parallel_tool_calls` per profile |
| Model profiles, context window learning | Our `profiles.py` + LangChain model `profile` metadata | Learn window from "maximum context length is N" error, cache per gateway/model |
| Small/fast model jobs | Second `CmcoderChatModel`; `SummarizationMiddleware(model=small)`; titles via `.with_structured_output` | |
| Weak-model tool calling | `wrap_model_call` middleware: if `tools` rejected → prompted Qwen `<tool_call>` format; parse text tool calls into `AIMessage.tool_calls`; JSON repair + pydantic validation with short error to model | Port `providers/text_tools.py`, `parse_tool_arguments` |
| Permissions (modes, allow/ask/deny, protected paths, high-risk, secret files) | Custom `PermissionMiddleware.awrap_tool_call` → rules engine → `interrupt({...})` → resume with `Command(resume={"decision": "allow_once"/"allow_always"/"deny", "reason":…})` | Built-in `HumanInTheLoopMiddleware` is too coarse; keep `risk.py`/`readonly.py` verbatim. `-p` mode: high-risk → deny without interrupt |
| Plan mode | Mode in `RunContext`; middleware filters to read-only tools + plan prompt | Shift+Tab sends `set_mode` |
| Hooks (PreToolUse, PostToolUse, UserPromptSubmit, SessionStart, Stop, SubagentStop, PreCompact, Notification) | Middleware hooks: `before_agent`, `awrap_tool_call`, `after_agent`, summarization callback | Reuse `core/hooks.py` runner (JSON stdin, exit codes) |
| Context budget / compaction | `SummarizationMiddleware` (trigger on token fraction, keep recent ~25%), `ContextEditingMiddleware` (clear old tool results), custom `ContextBudget` (cap `max_tokens`, retry once on overflow) | `/compact [focus]` invokes the summarizer directly |
| Sessions, `--continue`, `--resume` | `AsyncSqliteSaver` at `~/.cmcoder/projects/<hash>/sessions.db`, `thread_id` = session id | Owner-only file perms; `cleanupPeriodDays` prunes threads |
| Checkpoints / `/rewind` | Conversation: `graph.aget_state_history` + fork from a checkpoint; code: our content-addressed file snapshots taken in `CheckpointMiddleware` | Port `core/checkpoints.py` |
| TodoWrite | `TodoListMiddleware` (state key `todos`) → `todo_update` events | |
| Subagents (`Task`, `.cmcoder/agents/*.md`, per-role model, parallel) | Each definition → its own `create_agent` subgraph with a tool subset; `Task` tool invokes it; parallel via multiple tool calls (LangGraph runs them concurrently) | Evaluate `deepagents.SubAgentMiddleware`; adopt only if it supports our tool filtering, model per role and event mapping |
| Team (PM → Developer → Tester → DevOps) | Explicit `StateGraph` workflow with conditional edges (tester fails → back to developer), each node a subagent | Replaces prompt-only orchestration |
| Critic | `after_agent` middleware running a read-only critic subgraph with structured `Verdict` output (`response_format`); on fail, `jump_to="model"` with findings, up to `critic.maxRounds` | Port `core/critic.py` prompts |
| Skills | `SKILL.md` folders; only descriptions in prompt; a `Skill` tool loads the body | Evaluate `deepagents.SkillsMiddleware` |
| Memory (`CMCODER.md` chain, `AGENTS.md`, `CMCODER.local.md`) | Loaded into `system_prompt` (stable prefix for KV-cache) | Port loader |
| MCP | `MultiServerMCPClient(…, tool_name_prefix=True)`; `tool_interceptors` for permission + approval of untrusted project servers | Names normalised to `mcp__<server>__<tool>` |
| Code search (RAG) | `Embeddings` = `OpenAIEmbeddings` via gateway; `VectorStore` subclasses for local numpy store and Chroma-over-HTTP; code-aware splitter; `CodeSearch` tool + auto-context middleware | No `chromadb` in the bundle (D17) |
| Images / vision | Multimodal content blocks; vision-model fallback middleware describes images for text-only models | Port `core/vision.py` |
| IDE integration | Same stdio protocol; `ide_context` → `RunContext`; IDE tools (`getDiagnostics`, `openFile`, `showDiff`) call back to the client through an `interrupt`-style request/response | Diff review = permission interrupt with the diff payload |
| Streaming to UIs | `graph.astream(..., stream_mode=["messages", "updates", "custom"], subgraphs=True)`; tools emit progress via `get_stream_writer()` | `runtime/stream.py` maps to `assistant_delta`, `reasoning_delta`, `tool_use`, `tool_result`, `permission_request`, `subagent_status`, `usage` |
| Cost, usage | `AIMessage.usage_metadata` + LiteLLM cost header from the httpx response hook | |
| Telemetry | OpenTelemetry exporter, off by default; LangSmith tracing **force-disabled** unless the user sets it | `LANGSMITH_TRACING` unset in the bundle |
| Doctor | Port `cli/doctor.py`; probe tool calling with `bind_tools` on a 1-tool schema | |
| No Python on PCs | PyInstaller standalone with LangChain; lazy-import heavy modules; CI `no_python.py` gate unchanged | Measure startup time and size early (Phase 0 exit criterion) |

## Phases

Each phase ends with green CI (Linux, macOS, Windows), updated docs and a git tag, as in the original project.

**Phase 0 — Foundations (engine skeleton)**
1. `pyproject.toml` with the baseline above, `uv lock`, ruff, pyright (strict on `models/`, `middleware/`, `runtime/`), pytest.
2. Copy `protocol/`, `config/`, `security/` (risk, readonly, permissions rules, sensitive), `branding/`, `testing/mock_server.py`.
3. `models/`: `CmcoderChatModel`, auth, TLS/proxy httpx client, error classification, profiles, Qwen reasoning parsing.
4. `tools/`: Read, Write, Edit, MultiEdit, Glob, Grep, Bash as `BaseTool`s with read-before-write and output limits.
5. `agents/main.py`: `build_agent()` with `create_agent` + Permission, ContextBudget, ToolError, LoopDetection, ToolCallRepair.
6. `runtime/`: session with in-memory checkpointer, stream mapping, interrupt/resume.
7. CLI: REPL, `-p` (text/json/stream-json), `doctor`, `login`.
8. Evals: copy the 20 tasks; run against the mock server.
- **Done when:** a simple bug is fixed end to end against the mock server and (on the company network) LiteLLM + Qwen3; standalone binary builds, and its size and startup time are recorded.

**Phase 1 — Daily driver**
Summarization + context editing, `AsyncSqliteSaver` sessions and `--resume`, `/rewind` (time travel + file snapshots), prompted-tool fallback, TodoList, small/fast model (titles), managed settings, Textual TUI, slash commands, memory files.
- **Done when:** the original Phase 1 trial issues (long previews, Bash-for-files, unknown window) are all covered by tests.

**Phase 2 — IDE parity**
`--protocol stdio` on the new runtime; copy `vscode/`, `visualstudio/`, `eclipse/`, `netbeans/`, `jetbrains/`, `clients/`; IDE context and tools; native diff via permission interrupts.
- **Done when:** the existing IDE gate scenarios (G-list) pass unchanged against the new engine. This is the proof that the protocol didn't change.

**Phase 3 — Extensibility**
MCP adapters, hooks, custom commands, subagents as subgraphs (per-role model, parallel), skills, critic, team `StateGraph`.

**Phase 4 — Hardening**
Open WebUI gateway, Bash sandbox (copy `sandbox/`), OpenTelemetry (off), standalone builds + platform VSIX/plugins, `packaging/` release workflow, security review (prompt injection via MCP/tool output, interrupt bypass, checkpoint DB permissions).

**Phase 5 — Code search (RAG)**
Embeddings through the gateway, local and Chroma-HTTP `VectorStore`s, incremental index, `CodeSearch` tool, auto-context middleware, retrieval evals.

## Risks and mitigations
- **Standalone binary size and startup** (LangChain imports): lazy imports, exclude unused providers, measure in Phase 0; fall back to a smaller dependency set if startup goes over ~1 s.
- **`langchain-openai` drops non-standard fields** (`reasoning_content`, LiteLLM cost header): handled in the `CmcoderChatModel` subclass and the httpx response hook; covered by `respx` and LiteLLM integration tests.
- **Fast-moving APIs:** exact pins in `uv.lock`, Renovate/Dependabot with CI, and all LangChain imports behind our `models/`, `middleware/` and `agents/` modules.
- **Security regressions:** port the permission, risk and sandbox tests first and run them against the new middleware before anything else.
- **Interrupts in `-p` mode:** no interrupts; the permission middleware decides from rules only (deny when it would ask).

## Verification
- `uv run ruff check . && uv run pyright && uv run pytest` on all three operating systems in CI.
- Ported suites: `test_permissions*.py`, `test_risk`, `test_sandbox.py`, `test_compaction.py`, `test_sessions.py`, `test_checkpoints.py`, `test_subagents*.py`, `test_mcp.py`, `test_stdio.py`, `test_protocol_ts.py`, `test_rag_*.py`.
- Unit tests with `GenericFakeChatModel`; integration tests with `mock_server.py` and a real LiteLLM proxy (`test_litellm_integration.py`).
- `uv run python evals/run.py` (mock and real gateway): success rate, turns, tokens and tool choice compared against cmcoderagent's baseline.
- IDE release gate (G-list) in every IDE using the bundled standalone `cmcoder`, with no Python on the test machine's path.
- `cmcoder doctor` against the real gateway on the company network.
