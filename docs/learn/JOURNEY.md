# cmcoder: zero to hero

A learning journey for a developer who wants to **use cmcoder every day** and
**learn LangChain and LangGraph from it** at the same time. Each level pairs
something you do with cmcoder with the LangChain/LangGraph idea that makes it
work, the code to read, and a lab to run.

The companion reference is [the learning guide](README.md): its reading order
table and "Key ideas" section are the theory; this page is the path through
them. Every source file it points to starts with a `LANGCHAIN CONCEPTS IN THIS
FILE` section: read that first.

## How each level works

| Part | What you do |
|---|---|
| **Use it** | Something to try in VS Code or the terminal, on a real project |
| **What happened inside** | The LangChain / LangGraph concept behind it |
| **Read** | The files to open (and their row in [the guide](README.md#reading-order)) |
| **Lab** | Run it yourself: against the mock server, no gateway or GPU needed |
| **You can now** | What to check you understood before moving on |

Levels 0–4 are for every user (about a day). Levels 5–8 make you a power user
who can extend cmcoder (a few days). Levels 9–12 are for building agents of
your own with LangChain and LangGraph (a week or two, at your own pace).

```
 USE                                          LEARN
 L0  Set up                                   uv, the repo, the mock server
 L1  First chat                               chat models, messages, streaming
 L2  Let it read and edit                     tools (BaseTool)
 L3  A whole task                             create_agent: an agent is a graph
 L4  Say yes or no                            interrupt() and Command(resume)
 ───────────────────────── power user ─────────────────────────
 L5  Come back tomorrow, undo                 checkpointers, threads, time travel
 L6  Long conversations                       summarization, context editing
 L7  Make it yours                            memory, commands, skills, hooks
 L8  Connect tools                            MCP adapters
 ───────────────────────── builder ────────────────────────────
 L9  Middleware                               the hooks around the loop
 L10 Many agents                              subagents, structured output, StateGraph
 L11 Code search                              Embeddings, VectorStore, RAG
 L12 Production                               IDE protocol, callbacks, tests, evals
 Capstone                                     build your own agent feature end to end
```

---

## Level 0: Set up (30 minutes)

**Use it**

```bash
git clone -b claude/langgraph-plan https://github.com/Kathir2312/Vibecoder.git
cd Vibecoder
uv sync                       # Python 3.11+ and every dependency, in .venv
uv run pytest -q              # ~600 tests, no API key needed: you have a working setup
uv run cmcoder --help
```

Then point cmcoder at your gateway: copy
[`docs/examples/settings.example.json`](../examples/settings.example.json) to
`~/.cmcoder/settings.json`, set `baseUrl` and the model, then:

```bash
uv run cmcoder login          # stores the API key in the OS keychain
uv run cmcoder doctor         # checks TLS, proxy, the key, the model, tool calling
```

In VS Code, install the extension (see [build-package/vscode.md](../build-package/vscode.md))
and open the cmcoder panel.

**The mock server.** Every lab below uses `cmcoder.testing.mock_server`: a
fake OpenAI-compatible gateway that answers from a script (a JSON list of
replies). It makes the agent's behaviour repeatable, so you can watch exactly
what LangGraph does. Make a practice project once:

```bash
mkdir -p ~/cmcoder-lab && cd ~/cmcoder-lab && git init -q
printf 'def add(a, b):\n    return a - b\n' > app.py
```

and keep these in your shell for the labs (the repo is `$REPO`):

```bash
export REPO=~/Vibecoder   # where you cloned it
export CMCODER_BASE_URL=http://127.0.0.1:8765/v1 CMCODER_MODEL=qwen3-27b CMCODER_API_KEY=x
export NO_PROXY=127.0.0.1
export CMCODER_CONFIG_DIR=~/cmcoder-lab/.config   # keep lab sessions out of your real ~/.cmcoder
```

(Windows PowerShell: `$env:CMCODER_BASE_URL = "http://127.0.0.1:8765/v1"` and so on.)

**You can now:** run the test suite, run `cmcoder doctor` against your gateway,
start the mock server.

---

## Level 1: Your first conversation (1 hour)

**Use it.** In the terminal, `uv run cmcoder`, or in the VS Code panel, ask
something about your code: *"What does this project do? Look at the README."*
Watch the answer stream in. Try `/help`, `/model`, `/cost`, `/context`.

**What happened inside.** cmcoder talks to your gateway through a LangChain
**chat model**: `CmcoderChatModel`, a subclass of `ChatOpenAI` pointed at
LiteLLM or Open WebUI with `base_url`. Your text becomes a `HumanMessage`; the
reply is an `AIMessage` that arrives in **chunks** (`AIMessageChunk`) while it
streams. Qwen3's thinking (`<think>…</think>` or `reasoning_content`) is split
off as reasoning and never sent back to the model.

**Read.** [`models/chat.py`](../../src/cmcoder/models/chat.py) (guide row 1):
message types, streaming chunks, `additional_kwargs`, why it subclasses
`ChatOpenAI` instead of calling the API itself.

**Lab: talk to the model directly, without the agent.**

```python
# uv run python
from cmcoder.config.settings import load_settings
from cmcoder.models.chat import build_chat_model
model = build_chat_model(load_settings()).model     # your real gateway
print(model.invoke("Say hi in five words").content)
for chunk in model.stream("Count to five"):
    print(chunk.content, end="|", flush=True)          # one chunk at a time
```

**You can now** explain: chat model, `HumanMessage` / `AIMessage`, `invoke`
vs. `stream`, and why reasoning text is kept out of the next request.

---

## Level 2: Let it read and edit (1 hour)

**Use it.** Ask *"Read app.py and explain it"*, then *"Fix the bug in add()"*.
cmcoder reads the file, proposes an edit, and shows you a diff before it
writes. In VS Code the diff opens in the editor.

**What happened inside.** The model doesn't touch files: it answers with
**tool calls** (`AIMessage.tool_calls`: a name and JSON arguments). LangChain
**tools** run them. cmcoder's tools (Read, Write, Edit, MultiEdit, Glob, Grep,
Bash, …) are `BaseTool`s with a pydantic `args_schema`; the schema is what the
model sees, and arguments are validated against it before anything runs. Read
before Write is enforced: the agent can't overwrite a file it hasn't read.

**Read.** [`tools/adapter.py`](../../src/cmcoder/tools/adapter.py) (row 2),
then one tool in [`tools/files.py`](../../src/cmcoder/tools/files.py).

**Lab: see what the model is offered.** Run the bug fix against the mock
server with the requests recorded:

```bash
# terminal 1
uv run --project $REPO python -m cmcoder.testing.mock_server \
    --script $REPO/docs/learn/labs/fix_add.json --port 8765 --record ~/cmcoder-lab/requests.jsonl
# terminal 2, in ~/cmcoder-lab
uv run --project $REPO cmcoder -p "fix add" --verbose
```

`--verbose` shows `> Read(app.py) done`, then `> Edit(app.py) error`. The edit
was *denied*: `-p` mode can't ask you (Level 4 explains). Now open
`requests.jsonl`: the first request's `tools` list is every tool's JSON schema,
exactly as the model receives it.

**You can now** explain: tool calls, `BaseTool`, `args_schema`, and why the
model only *asks* for a tool to run.

---

## Level 3: A whole task (2 hours)

**Use it.** Give a real task: *"Add input validation to add(): raise
TypeError for non-numbers, and add a test."* Watch it read, edit, run the
tests, fix, and stop on its own. Turn on `/todos` for a bigger task to see its
plan.

**What happened inside.** This is **the agent loop**, and in LangChain v1 it
is a **LangGraph graph** built by `create_agent`: a `model` node and a `tools`
node. The model replies; if the reply has tool calls, the graph goes to
`tools` (each call can run in parallel), appends the `ToolMessage` results, and
goes back to `model`. A reply with no tool calls ends the run. The state is
`{"messages": [...], "todos": [...]}`.

**Read.** [`agents/main.py`](../../src/cmcoder/agents/main.py) (row 3), and
the guide's *"An agent is a graph"* and *"Which stream mode for what"*.

**Lab: watch the graph run.** [`labs/watch_graph.py`](labs/watch_graph.py)
streams `updates` and prints every node that ran:

```bash
# terminal 1: restart the mock server (each script is used up once)
# terminal 2
uv run --project $REPO python $REPO/docs/learn/labs/watch_graph.py http://127.0.0.1:8765/v1 ~/cmcoder-lab
```

You'll see `model → tools → model → tools → model`, and between them nodes
like `CompactionMiddleware.before_model` and `PermissionMiddleware.before_model`:
**middleware are nodes in the graph too** (Level 9). At the end, the
checkpoint holds 6 messages: user, AI (Read), tool, AI (Edit), tool, AI.

**You can now** draw the agent graph from memory and say what ends a run.

---

## Level 4: Say yes or no (1 hour)

**Use it.** Ask for an edit and press **Deny** (in VS Code, **Reject** on the
diff). The turn stops, and the model is not asked again until you say what to
do. Next time, deny *with feedback* ("use a different name"): the model gets
your words and tries again. Try the permission modes: `/mode plan` (read only),
`/mode acceptEdits`, Shift+Tab in the terminal. Add a rule to
`~/.cmcoder/settings.json`: `"permissions": {"allow": ["Bash(npm test:*)"]}`.

**What happened inside.** **Human-in-the-loop with `interrupt()`.**
`PermissionMiddleware` wraps every tool call (`awrap_tool_call`). When a call
needs your approval it calls `interrupt({...})`: LangGraph **saves the whole
state and stops**. cmcoder shows you the request; your answer resumes the
graph with `Command(resume=answer)`, and `interrupt()` returns it. The node
runs again from its start on resume, which is why code before `interrupt()`
must be safe to repeat. A plain "Deny" also ends the turn: the middleware's
`abefore_model` hook returns `{"jump_to": "end"}`.

In `-p` mode nobody can answer, so there is no interrupt: calls that would ask
are denied with a message saying how to allow them (`--allowedTools`).

**Read.** [`middleware/permission.py`](../../src/cmcoder/middleware/permission.py)
(row 6), the guide's *"Interrupts need a checkpointer"*.

**Lab: pause and resume a graph by hand.** [`labs/interrupt.py`](labs/interrupt.py)
runs the bug fix with `interactive=True`, reads `state.interrupts`, prints what
the middleware asked, and resumes with your answer:

```bash
uv run --project $REPO python $REPO/docs/learn/labs/interrupt.py http://127.0.0.1:8765/v1 ~/cmcoder-lab allow
# restart the mock server, put the bug back in app.py, then:
uv run --project $REPO python $REPO/docs/learn/labs/interrupt.py http://127.0.0.1:8765/v1 ~/cmcoder-lab deny
```

With `allow` the edit runs and the last message is the model's answer. With
`deny` the last message is the denial and the model was never called again.
Then, in `-p` mode: `cmcoder -p "fix add" --allowedTools "Edit"` lets it through
without asking.

**You can now** explain `interrupt()` / `Command(resume=...)`, why interrupts
need a checkpointer, and what the five permission modes do.

---

## Level 5: Come back tomorrow, and undo (2 hours)

**Use it.** Close the terminal, then `cmcoder --continue` (or `-c`): the
conversation is back. `cmcoder sessions` lists saved ones; `--resume <id>`
picks one. After a few edits, `/rewind` takes you back before an earlier
message: the code, the conversation, or both. `/clear` starts fresh (the old
conversation stays saved).

**What happened inside.** **Checkpointers.** Every step of the graph is saved
by an `AsyncSqliteSaver` in `~/.cmcoder/projects/<hash>/`, keyed by
`thread_id` (one per conversation). Continuing is just running the graph
again on the same `thread_id` with the new message only. Every step is kept,
not just the latest, so `/rewind` uses **time travel**: `aget_state_history`
finds the checkpoint before that message, and `aupdate_state` on it **forks**
the thread. File changes are undone from cmcoder's own snapshots (LangGraph
saves the conversation, not your files).

**Read.** [`runtime/store.py`](../../src/cmcoder/runtime/store.py) (row 8),
[`runtime/session.py`](../../src/cmcoder/runtime/session.py) (row 7),
[`middleware/checkpoint.py`](../../src/cmcoder/middleware/checkpoint.py) (row 12),
[`agents/context.py`](../../src/cmcoder/agents/context.py) (row 9: state vs.
context vs. session objects).

**Lab.** Guide exercise 4 (*Time travel*): after two turns in a test, list the
checkpoint history and fork from the one `/rewind` would use.

**You can now** explain state vs. runtime context vs. live objects, `thread_id`,
and how a rewind keeps the newer history.

---

## Level 6: Long conversations (1 hour)

**Use it.** In a long session, `/context` shows how full the model's window
is. `/compact` (or `/compact keep the API decisions`) summarises the
conversation to free room. Compaction also happens on its own at 80% full
(`autoCompactThreshold`).

**What happened inside.** LangChain's built-in **`SummarizationMiddleware`**
(subclassed): when the token count passes the threshold, older messages are
replaced by a summary written by the small/fast model, with `RemoveMessage`.
Old, large tool outputs are cleared first (context editing). With a 32K
window, the system prompt and tool schemas count too, which is why cmcoder
counts them itself.

**Read.** [`middleware/compaction.py`](../../src/cmcoder/middleware/compaction.py)
(row 10), [`middleware/model.py`](../../src/cmcoder/middleware/model.py) (row 4).

**Lab.** Guide exercise 6 (*Compaction*): lower `contextWindow` and watch for
the `compacted` event in `--output-format stream-json`.

**You can now** explain why the context window is the main limit of a 27B
model, and what compaction keeps and drops.

---

## Level 7: Make it yours (2 hours)

**Use it.**

| Feature | How |
|---|---|
| **Memory** | `CMCODER.md` in the project root (shared) or `CMCODER.local.md` (yours): build commands, conventions. In the terminal, a line starting with `#` adds to memory. |
| **Custom commands** | `.cmcoder/commands/review.md` becomes `/review`; `$ARGUMENTS` is what you type after it |
| **Skills** | `.cmcoder/skills/<name>/SKILL.md`: only the description is in the prompt; the agent loads the rest when it needs it |
| **Hooks** | Shell commands on events, in settings: `"hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "ruff format ."}]}]}` |
| **Settings** | `~/.cmcoder/settings.json` (yours), `.cmcoder/settings.json` (the project's; used once you `cmcoder trust` it) |

**What happened inside.** Memory files go into the **system prompt**, kept
stable so the gateway can reuse its prompt cache. Hooks are middleware:
PreToolUse/PostToolUse wrap tool calls, and a Stop hook runs in `aafter_agent`
and can send the agent back to work with `jump_to: "model"`.

**Read.** [`middleware/hooks.py`](../../src/cmcoder/middleware/hooks.py) (row 17),
[`core/commands.py`](../../src/cmcoder/core/commands.py),
[`core/skills.py`](../../src/cmcoder/core/skills.py).

**Lab.** Write a `/explain` command that asks for an explanation of a file for
a new team member, and a PostToolUse hook that runs your formatter after every
Edit. Check that a project's commands only appear after `cmcoder trust`.

**You can now** tailor cmcoder to a project, and explain why project files
need trust before they run anything.

---

## Level 8: Connect tools with MCP (1 hour)

**Use it.** Add an MCP server (a database, Jira, a docs search) in
`.mcp.json` or under `mcpServers` in settings, then `/mcp` to see its tools.
A project's servers ask for your approval the first time
(`cmcoder mcp approve`).

**What happened inside.** `langchain-mcp-adapters` turns each MCP tool into a
LangChain tool named `mcp__<server>__<tool>`, so the agent, permissions and
hooks treat it like any other tool. A **tool interceptor** adds a timeout that
returns an error result instead of hanging.

**Read.** [`mcp/manager.py`](../../src/cmcoder/mcp/manager.py) (row 16).

**You can now** add an MCP server and explain how its tools reach the model.

---

## Level 9: Middleware, the heart of customising an agent (half a day)

**Use it.** Nothing new to click: you've been using middleware since Level 3.

**What happened inside.** Instead of writing your own loop, you add
**middleware** to `create_agent`. The hooks:

| Hook | Runs | cmcoder uses it for |
|---|---|---|
| `abefore_agent` | once per run, first | automatic code context (Level 11) |
| `abefore_model` | before every model call | compaction; ending a turn after a denial |
| `awrap_model_call` | around every model call | context editing, model profile, prompted tool calls for weak models, vision fallback |
| `aafter_model` | after every model reply | repairing broken tool calls, splitting reasoning |
| `awrap_tool_call` | around every tool call | permissions, loop guard, file snapshots |
| `aafter_agent` | once per run, last | the Stop hook, the critic |

The first middleware in the list is the outermost wrapper.

**Read.** [`middleware/guards.py`](../../src/cmcoder/middleware/guards.py) (row 5),
[`middleware/tool_text.py`](../../src/cmcoder/middleware/tool_text.py) (row 11),
the `middleware` list in [`agents/main.py`](../../src/cmcoder/agents/main.py).

**Lab: your own middleware.** [`labs/my_middleware.py`](labs/my_middleware.py)
adds a `WatchMiddleware` through `build_agent(..., extra_middleware=[...])`.
It prints how many messages each model call sees and times each tool:

```text
[before_model] the model will see 1 messages
[tool] Read took 1.1 ms
[before_model] the model will see 3 messages
[tool] Edit took 1.5 ms
[before_model] the model will see 5 messages
answer: Fixed add().
```

Then extend it: guide exercise 1 (add the git branch to the last user message)
and exercise 5 (swap the loop guard for LangChain's `ToolCallLimitMiddleware`).

**You can now** pick the right hook for a job and write a middleware.

---

## Level 10: Many agents (1 day)

**Use it.**

1. *"Use the explore agent to find where errors are logged."* The main agent
   starts a **subagent** with the Task tool; the panel shows it with its own
   progress.
2. Your own agents: `.cmcoder/agents/bug-hunter.md` with `name`,
   `description`, `tools`, `model` front matter. `/agents` lists them; ask for
   them by name.
3. `/critic on`: a critic agent reviews each answer before you see it.
4. `"team": {"enabled": true}` in settings, then `/team <feature>`: PM →
   developer → tester (→ developer again on failure) → DevOps.

**What happened inside.**

* **A graph inside a tool.** Each subagent is its own `create_agent` graph,
  run inside the Task tool. It shares the parent's checkpointer, so a
  permission prompt deep inside a subagent pauses the *whole* run, and resumes
  exactly there. Several Task calls in one reply run in parallel.
* **Structured output.** The critic answers with a `Verdict` object via
  `response_format=ToolStrategy(Verdict)`; the team's tester with a
  `TestReport`. Routing reads fields, never parses prose.
* **Your own `StateGraph`.** The team is not a prompt asking the model to
  follow a process: it is a graph with a typed state, a reducer (`changes`
  accumulates every developer round) and **conditional edges** (a failed test
  goes back to the developer while rounds are left). Use `create_agent` when
  the model should decide what happens next; write a `StateGraph` when the
  order matters more than the model's judgement.

**Read.** [`agents/subagents.py`](../../src/cmcoder/agents/subagents.py) (row 18),
[`middleware/critic.py`](../../src/cmcoder/middleware/critic.py) (row 19),
[`agents/team.py`](../../src/cmcoder/agents/team.py) (row 20). The guide's
*"Graphs nest, and so do interrupts"* and *"Context comes from the direct
parent"*.

**Lab: a subagent run.** With [`labs/subagent.json`](labs/subagent.json) as the
mock server's script:

```bash
uv run --project $REPO cmcoder -p "where is add?" --verbose
```

shows `> Task`, then the subagent's own `> Grep("def add")`, then the main
agent's answer. Then guide exercise 7: add a `reviewer` node to the team graph
and draw it with `TEAM_GRAPH.get_graph().draw_mermaid()`.

**You can now** choose between a subagent, `create_agent` and a hand-written
`StateGraph`, and explain how interrupts travel through nested graphs.

---

## Level 11: Code search (RAG) (half a day)

**Use it.** `cmcoder rag setup` (or "Set up code search" in VS Code), then
`/index`. Ask a question about code you didn't name: a "code context" block
with the best matches appears before the answer. `/index status` shows the
index; it updates itself as files change.

**What happened inside.** The two classic RAG patterns, both on LangChain
abstractions:

* An **`Embeddings`** class (`GatewayEmbeddings`) turns code chunks into
  vectors through your gateway's embedding model.
* **`VectorStore`** subclasses keep them: a local store (SQLite + numpy) or
  Chroma over REST. Chunks are `Document`s with metadata (path, lines, symbol).
  Every `VectorStore` is also a **retriever** (`as_retriever()`).
* *Retrieve, then generate:* `AutoContextMiddleware` (`abefore_agent`) searches
  with your words and puts the matches in front of your message.
* *Retrieval as a tool:* the `CodeSearch` tool lets the model search for what
  it decides it needs.

**Read.** [`rag/embed.py`](../../src/cmcoder/rag/embed.py) (row 25),
[`rag/stores.py`](../../src/cmcoder/rag/stores.py) and
[`rag/chunker.py`](../../src/cmcoder/rag/chunker.py) (row 26),
[`rag/index.py`](../../src/cmcoder/rag/index.py),
[`middleware/code_context.py`](../../src/cmcoder/middleware/code_context.py),
[`tools/code_search.py`](../../src/cmcoder/tools/code_search.py) (row 27).

**Lab.** Guide exercise 10: take `store.as_retriever()` and build
`retriever | format_docs | prompt | model` with LCEL; compare its answer with
the agent's.

**You can now** explain embeddings, vector stores and retrievers, and when to
retrieve before the model vs. give the model a search tool.

---

## Level 12: Production (1–2 days)

**Use it.** You already have: every IDE (VS Code, Visual Studio, IntelliJ,
Eclipse, NetBeans) runs the same `cmcoder --protocol stdio`; the packages
carry a standalone `cmcoder` with no Python needed on developers' PCs.

**What happened inside, and what to study:**

| Topic | Read | Idea |
|---|---|---|
| Driving a graph from a server | [`cli/stdio.py`](../../src/cmcoder/cli/stdio.py) (row 13) | A turn is an asyncio task; an interrupt is answered by a later client message; rebuilding a graph on the same checkpointer keeps the conversation |
| Observing everything | [`telemetry.py`](../../src/cmcoder/telemetry.py) (row 21) | **Callbacks** (`AsyncCallbackHandler`) see every model and tool call, nested graphs included; middleware changes things, callbacks only watch |
| What a checkpoint may load | [`runtime/serde.py`](../../src/cmcoder/runtime/serde.py) (row 24) | Checkpoints are data you deserialise: allow-list the classes |
| Gateways | [`models/gateway.py`](../../src/cmcoder/models/gateway.py) (row 23) | What model classes don't cover: model lists, context windows, response headers |
| Images | [`middleware/vision.py`](../../src/cmcoder/middleware/vision.py) (row 22) | Multimodal content blocks; describing images for a text-only model |
| Testing agents | [`tests/test_agent_e2e.py`](../../tests/test_agent_e2e.py), [`tests/test_stdio.py`](../../tests/test_stdio.py) (row 28) | A scripted OpenAI-compatible server makes agent tests repeatable |
| Measuring quality | [`evals/run.py`](../../evals/run.py) | Success rate, turns, tokens and tool choice over 20 tasks, mock or real gateway |
| Security | [`docs/SECURITY-REVIEW.md`](../SECURITY-REVIEW.md) | Prompt injection through tool output, interrupt bypass, the Bash sandbox |

**Lab.** Guide exercise 9: write a callback handler that prints each tool's
duration, and check that a subagent's tools show up too. Then run the evals:
`uv run python evals/run.py` (mock) and against your gateway.

**You can now** test an agent, observe it in production and reason about its
security.

---

## Capstone: build a feature end to end

Pick one and do it the way the code base does things: a design note, the
change, tests against the mock server, an eval task, docs.

1. **A `Today`/`GitLog` tool** (guide exercise 2): the `@tool` decorator, then
   the full path as a cmcoder `Tool` with permissions and a test.
2. **A reviewer step in the team** (guide exercises 7–8): a new node, a
   structured `Review` output, a conditional edge back to the developer.
3. **A "test failure explainer" middleware**: after a Bash call whose output
   contains a failing test, add a short hint to the tool result pointing at
   the test file (`awrap_tool_call`), with a test.
4. **Your own agent, outside cmcoder**: a 100-line `create_agent` script for a
   task at work (with one custom tool, one middleware, a SQLite checkpointer
   and an interrupt before anything is written). This is the real graduation:
   everything above, on your own.

## Concept index

| Concept | Level | Where in cmcoder |
|---|---|---|
| Chat model, messages, streaming | 1 | `models/chat.py` |
| Tools, `args_schema` | 2 | `tools/adapter.py`, `tools/files.py` |
| `create_agent`, the agent graph | 3 | `agents/main.py` |
| Stream modes (`messages`, `updates`, `custom`) | 3, 12 | `runtime/session.py`, `labs/watch_graph.py` |
| `interrupt()`, `Command(resume=...)` | 4 | `middleware/permission.py`, `labs/interrupt.py` |
| `jump_to` from a hook | 4, 7, 10 | `middleware/permission.py`, `hooks.py`, `critic.py` |
| Checkpointer, `thread_id` | 5 | `runtime/store.py` |
| Time travel, forking | 5 | `runtime/session.py` |
| Runtime context (`context_schema`) | 5, 10 | `agents/context.py`, `agents/team.py` |
| `SummarizationMiddleware`, `RemoveMessage` | 6 | `middleware/compaction.py` |
| Middleware hooks | 9 | `middleware/*`, `labs/my_middleware.py` |
| MCP adapters, tool interceptors | 8 | `mcp/manager.py` |
| Nested graphs, subagents | 10 | `agents/subagents.py` |
| Structured output (`ToolStrategy`) | 10 | `middleware/critic.py`, `agents/team.py` |
| `StateGraph`, reducers, conditional edges | 10 | `agents/team.py` |
| `Embeddings`, `VectorStore`, retriever | 11 | `rag/` |
| Callbacks | 12 | `telemetry.py` |
| Checkpoint serialisation | 12 | `runtime/serde.py` |

## Self-check: are you a hero yet?

Answer without looking (each links back to a level):

1. What ends an agent run in `create_agent`? (L3)
2. Why must the code before `interrupt()` be safe to run twice? (L4)
3. What is the difference between state, runtime context and a live object like a shell process? (L5)
4. How does `/rewind` go back without deleting the newer history? (L5)
5. Which hook would you use to (a) change every request's system prompt, (b) block a tool, (c) review the final answer? (L9)
6. When do you write your own `StateGraph` instead of using `create_agent`? (L10)
7. A permission prompt appears inside a subagent inside the team graph. What pauses, and where does it resume? (L10)
8. When is retrieve-then-generate better than a search tool, and the other way round? (L11)
9. Middleware or callback: which one for metrics, and why? (L12)
