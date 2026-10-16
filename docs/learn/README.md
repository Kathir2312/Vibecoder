# Learning LangChain and LangGraph with cmcoder

cmcoder is a real coding agent (in the style of Claude Code) built on
**LangChain v1** and **LangGraph**. This guide walks through the code in the
order that teaches the concepts best. Every file it points to starts with a
`LANGCHAIN CONCEPTS IN THIS FILE` section that explains what it uses and why.

Versions used: `langgraph` 1.2, `langchain` 1.4, `langchain-core` 1.6,
`langchain-openai` 1.7 (see `pyproject.toml` and `uv.lock`).

## The big picture

```
 you ──► cli/ (REPL, -p) · IDEs ──► cli/stdio.py ┐
           │                                    │ cmcoder's own code
           ▼                                    │
       runtime/session.py   graph.astream(...)  │ streams events, handles interrupts
           │                                    ┘
           ▼
   ┌───────────────── create_agent(...) = a compiled LangGraph graph ────────────────┐
   │                                                                                  │
   │   START ─► before_model ─► model ─► after_model ─► tools ─► model ─► ... ─► END   │
   │            (Compaction:    ▲ awrap_model_call:     │ awrap_tool_call (per call):   │
   │             summarise)     │ ContextEditing →      │ LoopGuard → BashFileWork →    │
   │                            │ ModelProfile →        │ Permission (interrupt!) →     │
   │                            │ TextToolCalls         │ FileCheckpoint → tool         │
   │   state = {messages, todos}; the checkpointer saves it after every step,         │
   │   per thread_id (SQLite: runtime/store.py); context = RunContext (per run)        │
   └──────────────────────────────────────────────────────────────────────────────────┘
           │
           ▼
   models/chat.py  CmcoderChatModel(ChatOpenAI) ──HTTPS──► LiteLLM / Open WebUI ──► Qwen3
```

## Reading order

| # | File | You learn |
|---|---|---|
| 1 | [`src/cmcoder/models/chat.py`](../../src/cmcoder/models/chat.py) | `BaseChatModel`, `ChatOpenAI` with `base_url`, message types, streaming chunks, `additional_kwargs`, subclassing a provider for non-standard fields |
| 2 | [`src/cmcoder/tools/adapter.py`](../../src/cmcoder/tools/adapter.py) | `BaseTool`, `args_schema`, `_arun`, `ToolException`; why live resources stay out of graph state |
| 3 | [`src/cmcoder/agents/main.py`](../../src/cmcoder/agents/main.py) | `create_agent`, the agent graph, middleware order, checkpointers, the built-in `ModelCallLimitMiddleware` |
| 4 | [`src/cmcoder/middleware/model.py`](../../src/cmcoder/middleware/model.py) | `awrap_model_call`, `ModelRequest.override`, `model_settings`, `aafter_model` |
| 5 | [`src/cmcoder/middleware/guards.py`](../../src/cmcoder/middleware/guards.py) | Changing the model's reply in state (`add_messages` replaces by id), answering a tool call without running it |
| 6 | [`src/cmcoder/middleware/permission.py`](../../src/cmcoder/middleware/permission.py) | **Human-in-the-loop**: `interrupt()`, `Command(resume=...)`, why the node re-runs on resume |
| 7 | [`src/cmcoder/runtime/session.py`](../../src/cmcoder/runtime/session.py) | `astream` with `stream_mode=["messages", "updates"]` and `version="v2"`, `thread_id`, `aget_state().interrupts`, `aupdate_state`, the `add_messages` reducer, **time travel** (`aget_state_history` + forking) |
| 8 | [`src/cmcoder/runtime/store.py`](../../src/cmcoder/runtime/store.py) | **Checkpointers**: `AsyncSqliteSaver` vs. `InMemorySaver`, `adelete_thread`, keeping your own index next to LangGraph's tables |
| 9 | [`src/cmcoder/agents/context.py`](../../src/cmcoder/agents/context.py) | **Runtime context**: `context_schema`, `astream(..., context=...)`, `runtime.context` in middleware |
| 10 | [`src/cmcoder/middleware/compaction.py`](../../src/cmcoder/middleware/compaction.py) | Built-in `SummarizationMiddleware` (subclassed), `RemoveMessage`, fraction triggers and model profiles, a summariser model |
| 11 | [`src/cmcoder/middleware/tool_text.py`](../../src/cmcoder/middleware/tool_text.py) | Rewriting a whole request (tools, system message, history) and the response; `convert_to_openai_tool` |
| 12 | [`src/cmcoder/middleware/checkpoint.py`](../../src/cmcoder/middleware/checkpoint.py) | The two kinds of checkpoint: LangGraph's (conversation) and file snapshots (code) |
| 13 | [`src/cmcoder/cli/stdio.py`](../../src/cmcoder/cli/stdio.py) | Driving a graph from a long-lived server: a turn as an asyncio task, interrupts answered by another client message, rebuilding a graph on the same checkpointer; when *not* to use an interrupt (quick IDE calls) |
| 14 | [`src/cmcoder/runtime/titles.py`](../../src/cmcoder/runtime/titles.py), [`cli/doctor.py`](../../src/cmcoder/cli/doctor.py) | `ainvoke` with per-call options, `bind_tools`, the `@tool` decorator |
| 16 | [`src/cmcoder/mcp/manager.py`](../../src/cmcoder/mcp/manager.py) | **MCP** through `langchain-mcp-adapters`: `load_mcp_tools` on a persistent session, `tool_interceptors` (a timeout that returns an error result), `load_mcp_prompt` |
| 17 | [`src/cmcoder/middleware/hooks.py`](../../src/cmcoder/middleware/hooks.py) | `aafter_agent` with `@hook_config(can_jump_to=["model"])` (a Stop hook says "not done yet"), the **custom stream** (`get_stream_writer()`) |
| 18 | [`src/cmcoder/agents/subagents.py`](../../src/cmcoder/agents/subagents.py) | **A graph inside a tool**: subagents share the parent's checkpointer, so their interrupts pause the main graph; `ToolRuntime` injection; parallel tool calls (`Send`) |
| 19 | [`src/cmcoder/middleware/critic.py`](../../src/cmcoder/middleware/critic.py) | **Structured output**: `response_format=ToolStrategy(Verdict)` and `structured_response`; an after-agent review loop |
| 20 | [`src/cmcoder/agents/team.py`](../../src/cmcoder/agents/team.py) | **Your own `StateGraph`**: a typed state with a reducer, conditional edges (a retry loop), `context_schema`, a compiled graph nested two levels deep |
| 21 | [`tests/test_agent_e2e.py`](../../tests/test_agent_e2e.py), [`tests/test_phase1.py`](../../tests/test_phase1.py), [`tests/test_stdio.py`](../../tests/test_stdio.py), [`tests/test_team.py`](../../tests/test_team.py) | Testing an agent end to end against a scripted OpenAI-compatible server |

## Key ideas, briefly

**An agent is a graph.** `create_agent` builds a LangGraph `StateGraph` whose
state is `{"messages": [...]}`. The model node appends an `AIMessage`; if it
has `tool_calls`, LangGraph sends each call to the tools node (one task per
call, so they can run in parallel), which appends `ToolMessage`s; then back to
the model. With no tool calls, the run ends.

**Middleware is how you customise the loop**, instead of writing your own
loop. Hooks: `before_agent`, `before_model`, `awrap_model_call`,
`aafter_model`, `awrap_tool_call`, `after_agent`. The first middleware in the
list is the outermost wrapper.

**State vs. context vs. session objects.**
- *State* (messages, todos): serialisable, saved by the checkpointer, restored on resume.
- *Runtime context* (`context_schema`, here `RunContext`): per run, not saved
  (the turn number, the file-snapshot store).
- *Session objects* (a shell process, an HTTP client): live Python objects. They
  stay outside the graph; cmcoder passes them to tools and middleware when it
  builds them.

**Interrupts need a checkpointer.** `interrupt(payload)` saves the state and
stops the run. The caller reads `state.interrupts`, asks the user, then runs
the graph again with `Command(resume={interrupt_id: answer})`. The interrupted
node runs again from its start, and `interrupt()` returns the answer this
time. That's why code before `interrupt()` must be safe to repeat.

**Same thread, same conversation.** `config={"configurable": {"thread_id": id}}`
selects the checkpoint. A second `astream` call on the same thread continues
the conversation; you only send the new `HumanMessage`. With a SQLite
checkpointer that also works across program runs (`--continue`).

**Every step is kept.** The checkpointer stores a checkpoint per step, not just
the latest. `aget_state_history` lists them; writing to an old one with
`aupdate_state(old_snapshot.config, ...)` forks the thread from there. That is
`/rewind`, and why a rewind or a compaction never loses anything.

**A compiled graph is fixed; a thread is not.** `create_agent` compiles one
model and one tool list. To switch models or add tools (an IDE announcing
`getDiagnostics`), build a new graph on the *same checkpointer* and keep the
thread id: the conversation carries on in the new graph.

**Which stream mode for what.** `messages` for tokens, `updates` for finished
node results, `tasks` for "a node just started" (cmcoder shows a tool call the
moment the tools node starts, not when a long command ends).

**`create_agent` or your own `StateGraph`?** `create_agent` is a loop: the
model decides what happens next. When the *order* matters more than the
model's judgement (PM, then developer, then tester, and back to the developer
only on a failure), write the graph yourself: nodes are async functions that
return the state keys they change, and `add_conditional_edges` decides where
to go. agents/team.py does that; each node runs a `create_agent` subagent.

**Graphs nest, and so do interrupts.** A graph run inside a node or a tool
(compiled without a checkpointer) becomes a subgraph: it saves its own
checkpoints in the parent's checkpointer under its own namespace. An
`interrupt()` deep inside pauses everything up to the top; on resume
LangGraph re-runs the path down to it, skipping nodes that already finished.
Anything the re-run rebuilds from scratch (here: a subagent's tool context)
must be kept across the pause by you.

**Context comes from the direct parent.** A nested graph's `runtime.context`
is what its own caller passed. agents/team.py passes the main graph's
`RunContext` on to the role agents explicitly.

**Structured output instead of parsing.** `response_format=ToolStrategy(Model)`
gives the model a tool shaped like a pydantic model; calling it ends the run
and the object lands in `state["structured_response"]`. The critic's verdict
and the tester's pass/fail are read as fields, never parsed from prose.

**Counting tokens is your job.** `SummarizationMiddleware` counts the tokens
of the *messages*; the system prompt and tool schemas also take room in the
window. With small (32K) windows, account for that (see compaction.py).

## Try it

```bash
uv sync                                  # install (Python 3.11+)
uv run pytest -q                         # the whole suite; no GPU or API key needed
uv run pytest tests/test_agent_e2e.py -v # the agent fixing a bug against the mock server
```

Run the agent against the scripted mock server by hand:

```bash
# terminal 1: a fake gateway that answers with a script
echo '[{"content": "Hello! I am a scripted reply."}]' > /tmp/script.json
uv run python -m cmcoder.testing.mock_server --script /tmp/script.json --port 8765

# terminal 2
export CMCODER_BASE_URL=http://127.0.0.1:8765/v1 CMCODER_MODEL=qwen3-27b CMCODER_API_KEY=x
uv run cmcoder -p "hi" --output-format stream-json
```

(If your shell has an HTTPS proxy set, add `NO_PROXY=127.0.0.1`.)

Against your real gateway: put it in `~/.cmcoder/settings.json` (see the main
README), then `uv run cmcoder login` and `uv run cmcoder doctor`.

## Exercises

1. **Your own middleware.** Write a `before_model` middleware that adds the
   current git branch to the last user message. Add it through
   `build_agent(..., extra_middleware=[...])` and test it with the mock server.
2. **A new tool.** Add a `Today` tool with the `@tool` decorator and pass it to
   `create_agent` next to the cmcoder tools. What does the model see in the
   request? (Check `server.requests[0]["tools"]` in a test.)
3. **Watch the graph.** In a test, print every `part` from
   `graph.astream(..., stream_mode=["updates"], version="v2")`. Which node
   names appear, in which order? Where do the middleware nodes show up?
4. **Time travel.** After two turns, list `await graph.aget_state_history(config)`
   with each checkpoint's message count and `next` nodes. Find the one
   `/rewind` would fork from (`Session._fork_before`). Fork from it by hand and
   check that the newer checkpoints are still there.
5. **Built-in middleware.** Swap the hand-written loop guard for LangChain's
   `ToolCallLimitMiddleware` and compare the behaviour (see the
   `langchain.agents.middleware` module).
6. **Compaction.** Lower `contextWindow` in `modelProfiles` and watch for the
   `compacted` event in `cmcoder -p ... --output-format stream-json`. Then
   change `keep` in middleware/compaction.py and compare what survives.

7. **A new team role.** Add a `reviewer` node to agents/team.py between the
   developer and the tester, with a structured `Review` output, and a
   conditional edge that sends a rejected change back to the developer.
   Draw the graph with `TEAM_GRAPH.get_graph().draw_mermaid()`.
8. **Structured output.** Give the developer role a `ChangeReport` schema
   (files changed, how it was checked) and pass its fields to the tester
   instead of the free-text report.

## Where things go next

See [the plan](../PLAN.md):
- Phase 4: hardening (Open WebUI, the Bash sandbox, OpenTelemetry, standalone builds, a security review).
- Phase 5: RAG with LangChain `Embeddings` and `VectorStore`.
