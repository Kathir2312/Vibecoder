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

## Phase 0 status: complete (except the real-gateway check)

Done:
- Project setup: `uv`, the pinned LangChain/LangGraph stack, ruff, pyright and pytest; CI on Linux, macOS and Windows (lint, types, tests, mock evals).
- Reused from cmcoderagent without changes: `protocol/`, `config/`, `core/` (permissions, risk, readonly, prompt, steer), `providers/` (auth, profiles, transport), `tools/`, `sandbox/`, `testing/mock_server.py`, plus their tests.
- `models/`: `CmcoderChatModel` (keeps `reasoning_content`, chooses `max_tokens` or `max_completion_tokens`, company TLS through our httpx client), `<think>` splitting, error hints.
- `tools/adapter.py`: the cmcoder tools as LangChain `BaseTool`s.
- `middleware/`: permissions with `interrupt()`/`Command(resume)`, a per-call `max_tokens` budget, tool-call repair, loop guard, steering shell file work to the file tools, reasoning cleanup.
- `agents/main.py`: `create_agent`. `runtime/session.py`: streaming to protocol events, interrupts and repair after Ctrl+C.
- CLI: REPL, `-p` (text/json/stream-json), `doctor`, `login`, `logout`.
- Evals: 25 tasks copied; the 20 that need only Phase 0/1 features pass with `evals/run.py --mock` (5 are marked for Phases 3 and 5: subagents, MCP, custom commands, critic, code search).
- Standalone build (`packaging/build.py`, PyInstaller onedir), measured on Linux x86-64:
  - size **113 MB** (largest parts: zstandard 23 MB, cryptography 14 MB, libpython 7 MB);
  - `cmcoder --version`: **0.45 s** (the engine is imported lazily);
  - first `-p` call up to the first request: about **2.1 s**, almost all of it importing LangChain, LangGraph and the OpenAI SDK (about 1.2 s in a normal Python). That is over the ~1 s goal. It matters less for the IDEs, which start cmcoder once per session. Next steps if it bothers users: import the engine in the background while the REPL prompt is shown, and trim unused optional packages from the bundle.
  - Runs with no Python on the PATH (`packaging/no_python.py run -- dist/cmcoder/cmcoder -p ...`).
- Learning guide: [docs/learn/README.md](learn/README.md).

Not done (needs the company network): running `cmcoder doctor` and `evals/run.py` against the real LiteLLM gateway and Qwen3 models.

Changes from the plan:
- Reused modules keep their original paths (`core/`, `providers/`) rather than moving to `security/`. This keeps imports and tests identical to cmcoderagent.
- No `MultiEdit` tool: cmcoderagent doesn't have one either.

## Phase 1 status: complete (Textual TUI deferred)

Done:
- **Saved sessions** on LangGraph's `AsyncSqliteSaver`: one owner-only database per project (`~/.cmcoder/projects/<project>/sessions.db`) plus a small session index table; `--continue`, `--resume <id>`, `cmcoder sessions`, `/resume`; `persistSessions: false` keeps everything in memory; `cleanupPeriodDays` deletes old threads.
- **Auto-compaction** with LangChain's `SummarizationMiddleware` (subclassed in `middleware/compaction.py`): the small/fast model summarises at `autoCompactThreshold` of the window, the newest quarter is kept, the latest user request is carried word for word, and the fixed system-prompt and tool overhead is taken into account. `/compact [focus]` runs it by hand.
- **Context editing**: LangChain's `ContextEditingMiddleware` clears old tool outputs at 60% of the window.
- **Context window learning**: the window is taken from the server's "maximum context length is N" error, saved per gateway and model, and the call is retried with the server's own prompt count. The characters-per-token estimate is calibrated from the server's usage numbers.
- **`/rewind`**: files from content-addressed snapshots taken before Write/Edit (`middleware/checkpoint.py`, reusing `core/checkpoints.py`); the conversation by LangGraph time travel (forking from an earlier checkpoint, so nothing is lost).
- **Todos**: LangChain's `TodoListMiddleware` (`write_todos`); the list lives in graph state, so it survives compaction and `--resume`, and streams as `todo_update` events.
- **Text tool calls**: Qwen `<tool_call>` blocks are parsed into real tool calls; "prompted" mode (profile setting, or switched on automatically when the server rejects `tools`) describes the tools in the system prompt instead.
- **Titles**: written by the small model in the background after the first turn.
- **REPL**: slash commands (`runtime/commands.py`, shared with the IDE protocol in Phase 2), Shift+Tab mode cycling, Ctrl+C cancels the turn, `# note` adds to CMCODER.md, `/model` switches the model on the same thread. Tested in a pseudo-terminal.
- **Managed settings** apply to the new engine (bypass mode disabled, managed deny rules), with tests.
- The cmcoderagent user-trial issues are covered: long permission previews are capped with the options repeated, Bash file work is steered to the file tools, and the real context window is learned from the server.
- 265 tests and 20/20 mock evals pass.

Not done:
- The full-screen Textual UI. cmcoderagent's `cli/tui.py` is tied to its old agent class. The line REPL covers the same features, so the Textual UI moves to after Phase 2, where it can share the IDE protocol client.

## Phase 2 status: complete here; real-IDE gates run in CI

Done:
- **`cmcoder --protocol stdio`** (`cli/stdio.py`) on the LangGraph runtime, with the same protocol as cmcoderagent:
  - user messages with the IDE's context (open file, selection, problems; secret files withheld) and images;
  - `interrupt`, `permission_response` (LangGraph interrupts underneath), `set_mode`, `set_model`;
  - `ide_capabilities` / `ide_tool_result`: the IDE tools `getDiagnostics` and `openFile`;
  - `list_sessions`, `list_commands`, `rewind` with `history`, the resumed conversation and todos at start;
  - `--client`, `--trust-project`, `--continue`, `--resume`.
- Code search and critic requests from IDE panels get the protocol's own "not set up" answers until Phases 3 and 5.
- `tool_use` events are sent when the tools node *starts* (LangGraph's `tasks` stream mode), so IDEs show a long-running command while it runs.
- `cmcoder setup` (first-time configuration, the IDEs' first-run hint) and `cmcoder protocol-schema`.
- Titles use the small model only when one is configured, as in cmcoderagent; otherwise the first words of the first message.
- The IDE plugins and the shared panel copied unchanged: `vscode/`, `visualstudio/`, `eclipse/`, `netbeans/`, `jetbrains/`, `clients/`, `branding/`.

Checked here, against the new engine:
- cmcoderagent's own stdio protocol tests: 8/8 (two changes: LangChain's `write_todos` instead of `TodoWrite`, and the first-run hint).
- Evals through `-p` and through `--protocol stdio`: 40/40, and **20/20 tasks give the same result and tool calls both ways**.
- VS Code extension tests (real `cmcoder --protocol stdio` + mock gateway): 7/7. Shared chat panel tests: 76/76.
- JVM IDE core (Eclipse/NetBeans): 31/32. The one left, `CodeSearchTest`, needs code search (Phase 5) and is held out of CI until then.
- The NetBeans and Eclipse plugins build.

Not checked here (CI runs them): the release gates in real VS Code, IntelliJ IDEA, NetBeans and Eclipse, and the Visual Studio core and extension. VS Code and JetBrains downloads are blocked from this environment, and there is no .NET or Windows. CI now has the IDE jobs from cmcoderagent.

## Phase 3 status: complete

Done:
- **MCP** (`mcp/manager.py`) through `langchain-mcp-adapters`: stdio, streamable HTTP and SSE servers, one persistent session per server, tools named `mcp__<server>__<tool>`, `${VAR}` expansion, a per-server tool timeout (a `tool_interceptor` that returns an error result), project servers approved before their first start, managed allow/deny lists, MCP prompts as slash commands; `/mcp`, `--mcp-config`.
- **Hooks** (`middleware/hooks.py`, runner from cmcoderagent): PreToolUse in the permission middleware, PostToolUse in `awrap_tool_call`, Stop/SubagentStop in `aafter_agent` (a block jumps back to the model, at most 3 times), UserPromptSubmit, SessionStart and Notification in the session, PreCompact in compaction. Project hooks need approval.
- **Custom commands and skills**: `.cmcoder/commands/*.md` (`$ARGUMENTS`, `allowed-tools`), `SKILL.md` folders with only descriptions in the prompt and a `Skill` tool for the body.
- **Subagents** (`agents/subagents.py`): the `Task` tool runs a `create_agent` graph inside the tool, sharing the main checkpointer, so a subagent's permission prompt pauses the main graph. Built-in `general-purpose` and `explore`, your own `.cmcoder/agents/*.md`, a model per role, parallel runs capped by `maxParallelSubagents`, `subagent_status` events and `/agents`.
- **Critic** (`middleware/critic.py`): an `after_agent` review by a read-only subagent with structured output (`ToolStrategy(Verdict)`); a failed review sends the findings back to the model, up to `critic.maxRounds`; only the reviewed answer is shown. `/critic`, `--critic`, the IDE's critique toggle.
- **Team** (`agents/team.py`): an explicit `StateGraph` (PM → developer → tester, a failed test back to the developer up to `team.maxRounds`, DevOps when the stories say CI must change) behind a `Team` tool, with structured `Stories` and `TestReport` outputs. Roles from `.cmcoder/agents/` or built in. `team.enabled`, `/team <feature>`.

Fixed on the way: a subagent paused by a permission prompt now keeps its tool context (what it read, its shell) for the resumed run; before, an approved Write after a Read failed with "not read yet".

Checked:
- 313 tests; ruff and pyright clean.
- Evals at `BUILT_PHASE = 3`: 50/50 through `-p` and `--protocol stdio`, 25/25 tasks the same both ways. The Phase 3 tasks: `mcp-ticket`, `custom-command`, `subagent-search`, `critic-review`, `team-feature`.

Not checked here: the team and critic with a real Qwen3 27B on the company gateway (how reliably it fills `Stories`/`TestReport`/`Verdict` in prompted tool mode). That's the first thing to run on the real network.

## Phase 4 status: complete here; the release gates run in CI

Done:
- **Open WebUI as a gateway** (`"type": "openwebui"`). `models/gateway.py` covers the gateway's own endpoints: models, LiteLLM `/model/info`, Open WebUI's backends, Ollama's trained length, and a context-window probe. `prepare_agent()` caches what they say before the (synchronous) build. Ollama models get `num_ctx`. Learned windows keep their source and age.
- **The Bash sandbox** (bubblewrap / `sandbox-exec`, from cmcoderagent) in the new engine: shared by subagents, kept across rebuilds, a warning when it was asked for and can't run.
- **Images for a text-only model** (`visionModel`). The session has the vision model describe them first (`images_describing` / `images_described`); a failure stops the turn. `VisionMiddleware` sends the descriptions instead of the images, and summaries get them too.
- **Thinking off for quick jobs** (titles, summaries): `ModelBundle.quick()`.
- **OpenTelemetry** (off by default): model calls and tool runs through a LangChain callback handler, so subagents count too; turns and compactions from the session. LiteLLM's cost header becomes `response_metadata["cost"]` and `/cost` shows it.
- **The full `cmcoder doctor`**: network/TLS (with the untrusted issuer), key, models, and per model the window, streaming, the thinking switch, tool calling and images; then the vision model, MCP, hooks, sandbox, telemetry and extensions.
- **Packaging and release.** Branding, the shared program in `~/.cmcoder/bin`, the first-run user folder, the package scripts for the terminal and all IDEs, the release bundle with gate report and checksums, OSV checks, and the release and packaging-scripts workflows.
- **One version everywhere** (0.2.0).
- **Subcommands** `trust`, `mcp`, `models`, `version`, `terminal-profile`.
- **Security review** ([SECURITY-REVIEW.md](SECURITY-REVIEW.md)). Two fixes:
  - per-call caches keyed by the server's tool-call id now include name and arguments;
  - checkpoints load only cmcoder's own types.

Checked here:
- 582 tests, ruff and pyright clean. The sandbox tests run under real bubblewrap.
- Through a real Open WebUI (latest release), with an Ollama stand-in and the mock as an OpenAI-compatible backend: 2/2 (`tests/test_openwebui_real.py`).
- Evals at `BUILT_PHASE = 4`: 50/50, 25/25 the same through `-p` and `--protocol stdio`.
- The standalone build (Linux): 118 MB, `--version` in about 0.45 s. Its tests pass with no Python in reach.
- The JVM core 31/31 (CodeSearch is Phase 5), the shared panel 76/76; the NetBeans and Eclipse plugins build at 0.2.0.
- pip-audit over `uv.lock`: no known vulnerabilities. Semgrep: nothing to fix.

Not checked here (CI and the company network):
- the release gates in real IDEs and on Windows and macOS;
- `cmcoder doctor` and the evals against the real LiteLLM + Qwen3 gateway;
- how reliably Qwen3 27B fills the critic's and the team's structured outputs in prompted tool mode.

## Phase 5 status: complete

Done:
- **Embeddings through the gateway** (`rag/embed.py`). `GatewayEmbeddings` is a LangChain `Embeddings` that uses the gateway's own client: LiteLLM `/v1/embeddings` and Open WebUI `/api/embeddings` (which sends Ollama models on to Ollama). It works in batches, returns unit vectors, and explains a wrong model or one Open WebUI doesn't know.
- **Two `VectorStore`s** (`rag/stores.py`):
  - `LocalVectorStore`: SQLite plus a matrix in memory, built in, works in the standalone build;
  - `ChromaVectorStore`: Chroma's REST API, so no `chromadb` package; on this PC or a team server, with its key in the keychain.

  Both give `as_retriever()`.
- **Chunking along code structure** (`rag/chunker.py`, from cmcoderagent): Python by its parser, other languages at their definitions, Markdown by headings. Each chunk becomes a `Document` with path, lines and symbol. LangChain's character splitter isn't used because results need line numbers and names.
- **An incremental index** (`rag/index.py`):
  - a manifest per project and model;
  - only changed files are embedded again;
  - the agent's edits are re-indexed before the next search, and stale results are refreshed;
  - a background update starts with the session;
  - index folders are private (0700).
- **In the agent**:
  - the `CodeSearch` tool (read-only), shared with subagents;
  - automatic context (`AutoContextMiddleware`, `abefore_agent`): the best matches go in front of the user's message, within a token budget, never the same code twice, with a warning once if the index fails;
  - `/index`, which attaches a new index by rebuilding the agent on the same conversation.
- **Commands and IDEs**:
  - `cmcoder rag setup|status|on|off` and `cmcoder index`;
  - the doctor's code-search section;
  - the IDE protocol's `index`, `rag_candidates` and `rag_setup` (`index_status`, `index_progress`);
  - the CLI still loads without LangChain (`rag/common.py`).

Checked here:
- 611 tests; ruff and pyright clean. The index tests ran against a real Chroma server too (`CMCODER_TEST_CHROMA_URL`).
- Evals: all 26 tasks (`BUILT_PHASE = 5`), 52/52 through `-p` and `--protocol stdio`, 26/26 the same both ways. `code-search` finds the VAT bug through CodeSearch.
- The JVM core's `CodeSearchTest` passes again and is back in CI; the VS Code extension's tests pass 7/7.
- The standalone build with code search: 160 MB (numpy), `--version` in about 0.55 s. Its code-search test passes with no Python in reach.

Not checked here: the real gateway's embedding model (bge-m3, nomic-embed-text, ...) on the company network, and retrieval quality on a large real repository (the evals use the mock's word-hash embeddings).
