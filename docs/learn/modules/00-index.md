# cmcoder by module: LangChain and LangGraph from the ground up

A course through cmcoder's own source code, module by module, from the
foundations to the advanced parts. Each chapter takes one area of cmcoder,
teaches the LangChain / LangGraph concepts it rests on from zero (with a small
standalone example first), then walks through how cmcoder really uses them,
with links to the exact lines.

It sits next to two other guides, which it doesn't replace:

| Guide | Organised by | Best for |
|---|---|---|
| [JOURNEY.md](../JOURNEY.md) | Using cmcoder, level by level | Learning while you use it day to day |
| [README.md](../README.md) | Reading order of 28 files, key ideas | A quick reference |
| **This course** | cmcoder's modules, basic to advanced | Understanding *how it's built*, deeply |

## Chapters

| # | Chapter | cmcoder modules | Key concepts |
|---|---|---|---|
| **Foundations** ||||
| 01 | [The big picture](01-big-picture.md) | the package layout, one prompt end to end | Runnables, the LangChain / LangGraph layers |
| 02 | [Settings and the IDE contract](02-settings-and-protocol.md) | `config/`, `protocol/` | the plain-Python layers under the graph |
| 03 | [Talking to the model](03-models.md) | `models/`, `providers/` | chat models, messages, streaming, `bind` vs. copy |
| 04 | [Tools](04-tools.md) | `tools/` | `BaseTool`, `args_schema`, `ToolRuntime`, `@tool` |
| 05 | [The safety layer](05-safety.md) | `core/permissions`, `risk`, `readonly`, `sensitive.py`, `sandbox/` | why security stays outside the framework |
| **Core** ||||
| 06 | [Building the agent](06-agent.md) | `agents/main.py`, `agents/context.py` | `create_agent`, the generated graph, runtime context |
| 07 | [Middleware 1: around the model](07-middleware-model.md) | `middleware/model.py`, `tool_text.py`, `guards.py`, `vision.py` | `awrap_model_call`, `aafter_model`, reducers |
| 08 | [Middleware 2: tools and people](08-middleware-tools.md) | `middleware/permission.py`, `checkpoint.py`, `hooks.py` | `awrap_tool_call`, `interrupt()`, `Command`, `jump_to` |
| 09 | [Runtime: sessions and streaming](09-runtime.md) | `runtime/` | stream modes, checkpointers, threads, time travel |
| 10 | [Long context](10-long-context.md) | `middleware/compaction.py`, `core/prompt.py` | summarization, context editing, token budgets |
| **Advanced** ||||
| 11 | [Extending: MCP, commands, skills](11-extending.md) | `mcp/`, `core/commands.py`, `skills.py`, `hooks.py` | MCP adapters, tool interceptors |
| 12 | [Many agents](12-multi-agent.md) | `agents/subagents.py`, `middleware/critic.py`, `agents/team.py` | nested graphs, structured output, `StateGraph` |
| 13 | [Code search (RAG)](13-rag.md) | `rag/`, `middleware/code_context.py`, `tools/code_search.py` | `Embeddings`, `VectorStore`, retrievers, LCEL |
| 14 | [Production](14-production.md) | `cli/stdio.py`, `telemetry.py`, `testing/`, `evals/` | serving a graph, callbacks, testing agents |

Foundations take about a day, the core two or three days, the advanced part
about a week at a comfortable pace.

## Every chapter has the same parts

1. **Why this module exists**: the problem it solves.
2. **The concepts**: each LangChain / LangGraph idea from zero, with a small
   standalone example.
3. **How cmcoder uses it**: a tour of the real code, with links to the lines.
4. **A diagram** where one helps.
5. **Lab**: a script to run against the mock server (no gateway or GPU).
6. **Exercises**: from easy to a stretch.
7. **Check yourself**: questions, with answers to unfold.

## Before you start

```bash
git clone -b claude/langgraph-plan https://github.com/Kathir2312/Vibecoder.git
cd Vibecoder
uv sync
uv run pytest -q          # all green: your setup works
```

Every source file of interest starts with a `LANGCHAIN CONCEPTS IN THIS FILE`
section: read it before the code.

### Running the labs

The labs are in [`labs/`](labs/). Each runs cmcoder's real code against the
**mock server**, a fake OpenAI-compatible gateway that answers from a script:

```bash
# terminal 1: the fake gateway, with the lab's script
uv run python -m cmcoder.testing.mock_server --script docs/learn/modules/labs/<script>.json --port 8765

# terminal 2: the lab
export CMCODER_API_KEY=x NO_PROXY=127.0.0.1 CMCODER_CONFIG_DIR=/tmp/cmcoder-lab
mkdir -p /tmp/lab-project && printf 'def add(a, b):\n    return a - b\n' > /tmp/lab-project/app.py
uv run python docs/learn/modules/labs/<lab>.py http://127.0.0.1:8765/v1 /tmp/lab-project
```

A script's replies are used up in order: **restart the mock server before
each run.** Each lab's first lines say which script it needs.
`CMCODER_CONFIG_DIR` keeps lab sessions out of your real `~/.cmcoder`.
(Windows PowerShell: `$env:CMCODER_API_KEY = "x"` and so on.)

## Versions

`langgraph` 1.2, `langchain` 1.4, `langchain-core` 1.6, `langchain-openai` 1.7,
`langgraph-checkpoint-sqlite` 3.1, `langchain-mcp-adapters` 0.3 (exact
versions in `uv.lock`). Line links point at the `claude/langgraph-plan` branch
as of this course; if a line moved, search for the name next to the link.
