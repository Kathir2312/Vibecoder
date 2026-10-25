# 07 · Middleware 1: around the model

[← 06 Building the agent](06-agent.md) · [Index](00-index.md) · Next: [08 Middleware 2: tools and people →](08-middleware-tools.md)

**You will learn:** how to write an `AgentMiddleware`; how `wrap_model_call`
sees and changes each request (`ModelRequest`, `override`, `model_settings`)
and its response (`ModelResponse`); how `after_model` corrects a reply in the
state through the `add_messages` reducer; and how cmcoder uses these hooks
to make a 27B open model behave: a context budget per call, tool calls
written as text, repaired tool calls, reasoning kept out of the answer,
images described for a model that can't see them, and small guards around
tool calls.

## Why this module exists

A frontier model behind a cloud API mostly just works. A 27B model on a
company gateway has rough edges: a 32K window that a long request overflows,
servers without a tool-call parser, arguments that aren't quite JSON,
`<think>` text in the answer, no vision. cmcoderagent handled all of that
inside its hand-written loop. With `create_agent` each fix is a small
middleware that does one thing:

| File | Middleware | Hook |
|---|---|---|
| [`middleware/model.py`](../../../src/cmcoder/middleware/model.py) | `ModelProfileMiddleware`: `max_tokens` per call, window learning | `wrap_model_call` |
| | `ReasoningMiddleware`: `<think>` out of the answer | `after_model` |
| [`middleware/tool_text.py`](../../../src/cmcoder/middleware/tool_text.py) | `TextToolCallsMiddleware`: tool calls written as text | `wrap_model_call` |
| [`middleware/guards.py`](../../../src/cmcoder/middleware/guards.py) | `ToolCallRepairMiddleware`: broken tool calls | `after_model` |
| | `LoopGuard`, `BashFileWork`, `PlainToolText` | `wrap_tool_call` |
| [`middleware/vision.py`](../../../src/cmcoder/middleware/vision.py) | `VisionMiddleware`: image descriptions | `wrap_model_call` |

(The three tool guards live in `guards.py` with the repair, so they are
covered here; chapter 08 does the tool side in depth.)

## The concepts

### Writing a middleware

Subclass `AgentMiddleware` and override the hooks you need (chapter 06 lists
them). Everything else is inherited as "do nothing":

```python
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import ModelRequest, ModelResponse

class MyMiddleware(AgentMiddleware):
    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        ...                                   # before the call
        response = await handler(request)     # the call (and every inner middleware)
        ...                                   # after the call
        return response

    async def aafter_model(self, state: AgentState, runtime) -> dict | None:
        return None                           # or a state update
```

A middleware object is created once per agent and used for every run, so it
can keep state of its own across calls (cmcoder's do: a calibrated
characters-per-token ratio, a "this server can't do tools" flag).

### `wrap_model_call`: the request and the response

The `request` is a **`ModelRequest`** with what is about to be sent:

| Field | What |
|---|---|
| `model` | the chat model |
| `system_message` | the `SystemMessage` (kept apart from `messages`) |
| `messages` | the conversation |
| `tools` | the tools offered |
| `tool_choice`, `response_format` | forcing a tool, structured output (chapter 12) |
| `model_settings` | extra call options, passed on as `model.bind_tools(tools, **model_settings)`: `max_tokens`, `parallel_tool_calls`, … |
| `state`, `runtime` | the graph state and the runtime (with `runtime.context`) |

Requests are **immutable**: `request.override(field=...)` returns a changed
*copy*. That keeps middleware honest: an inner layer can't change what an
outer one is looking at.

`handler(request)` returns a **`ModelResponse`**: `result` (a list of
messages, normally one `AIMessage`) and `structured_response`. A wrapper may
call `handler` once, twice (a retry), or not at all, and may return a
different `ModelResponse` (for example one whose `AIMessage` has tool calls
it parsed from text).

What a `wrap_model_call` changes affects *this call only*. The state keeps
the original messages; only the reply the handler returns goes into the
state.

### `after_model` and the `add_messages` reducer

An `after_model` hook runs as a node after the `model` node and returns a
**state update**. The `messages` key has a **reducer**, `add_messages`, which
decides how an update is merged:

* a message with a **new id** is appended;
* a message with an **id already in the list replaces** that message.

```python
from langgraph.graph.message import add_messages
state = [HumanMessage("hi", id="1"), AIMessage("helo", id="2")]
state = add_messages(state, [AIMessage("hello", id="2")])   # same id: replaced
# [('1', 'hi'), ('2', 'hello')]
state = add_messages(state, [AIMessage("anything else?")])   # new id: appended
# ['hi', 'hello', 'anything else?']
```

So to *correct* the model's reply, return a copy of it with the same id:
`{"messages": [last.model_copy(update={...})]}`. That is the whole trick
behind `ToolCallRepairMiddleware` and `ReasoningMiddleware`.

### `wrap_model_call` vs `after_model`

Both can change a reply. The difference:

* `wrap_model_call` sees the **request** too, can retry, and its changes to
  the request never reach the state. Use it for how the call is made.
* `after_model` sees the **state**, and its change is saved like any other
  step. Use it to fix what the conversation keeps.

## How cmcoder uses it

### `ModelProfileMiddleware`: a budget for every call

vLLM rejects a request when prompt + `max_tokens` exceeds the window, and
with 32K that happens quickly. [`ModelProfileMiddleware`](../../../src/cmcoder/middleware/model.py#L64):

1. [Turns a system message made of content blocks into one string](../../../src/cmcoder/middleware/model.py#L121)
   (`TodoListMiddleware`, an outer layer, appends its instructions as a
   block; self-hosted servers want a plain string).
2. [Estimates the prompt's size](../../../src/cmcoder/middleware/model.py#L83)
   from its characters and sets
   [`max_tokens`](../../../src/cmcoder/middleware/model.py#L92) to the room
   that is left (at least 512, at most the model's `max_output`), and
   [`parallel_tool_calls`](../../../src/cmcoder/middleware/model.py#L95)
   from the model profile, through `request.override(model_settings=...)`.
3. After the reply, [calibrates](../../../src/cmcoder/middleware/model.py#L98)
   its characters-per-token ratio from the server's real token count
   (`AIMessage.usage_metadata`).
4. If the server still says "maximum context length is N",
   [learns the window](../../../src/cmcoder/middleware/model.py#L125),
   saves it, tells compaction, and **retries once** with old tool outputs
   cleared in *this request only*:
   [`request.override(messages=clear_old_tool_outputs(...))`](../../../src/cmcoder/middleware/model.py#L139).
   The state still has the full outputs.

That is a `wrap_model_call` doing all three things a wrapper can: change the
request, call the handler twice, read the response.

### `TextToolCallsMiddleware`: tool calls without a tool-call parser

Qwen's chat template has the model write tool calls as text:

```text
<tool_call>
{"name": "Read", "arguments": {"file_path": "app.py"}}
</tool_call>
```

A server with a tool-call parser turns that into native `tool_calls`; one
without passes the text through. [`TextToolCallsMiddleware`](../../../src/cmcoder/middleware/tool_text.py#L136)
handles both:

* **Native mode** (the default): it calls the handler and, if the reply has
  no native tool calls but contains such blocks for *known* tools,
  [rewrites the response](../../../src/cmcoder/middleware/tool_text.py#L167):
  a copy of the `AIMessage` with the text taken out and real `tool_calls`
  in. The rest of the agent can't tell the difference.
* **Prompted mode** (the profile says `toolCalling: "prompted"`, or the
  server [rejected the `tools` parameter](../../../src/cmcoder/middleware/tool_text.py#L152)
  once): it rewrites the whole
  [request](../../../src/cmcoder/middleware/tool_text.py#L157):
  `tools=[]`, the tools described in the system message in Qwen's own format
  ([`tools_prompt`](../../../src/cmcoder/middleware/tool_text.py#L101), built
  with LangChain's `convert_to_openai_tool`, the same JSON schema
  `bind_tools` would send), and the history
  [rewritten as text](../../../src/cmcoder/middleware/tool_text.py#L114)
  (tool calls as `<tool_call>` blocks, results as `<tool_response>`).

It is the innermost model wrapper, so no other middleware needs to know
which mode is on. While the reply streams, the session holds back
`<tool_call>` text from the screen with
[`ToolCallHoldback`](../../../src/cmcoder/middleware/tool_text.py#L178).

### `ToolCallRepairMiddleware`: arguments that aren't quite JSON

When a tool call's arguments don't parse, `ChatOpenAI` puts the call in
`AIMessage.invalid_tool_calls`, and the agent would answer it with an error,
costing a whole model turn. [`ToolCallRepairMiddleware.aafter_model`](../../../src/cmcoder/middleware/guards.py#L100)
tries [`parse_tool_arguments`](../../../src/cmcoder/middleware/guards.py#L53)
(code fences, trailing commas, Python-style dicts, double-encoded JSON),
moves the ones it can repair into `tool_calls`, gives calls without an id
one of their own (every `ToolMessage` must name its call's id), and
[returns the fixed message with the same id](../../../src/cmcoder/middleware/guards.py#L122):
`add_messages` replaces the reply before the `tools` node runs.

### `ReasoningMiddleware`: thinking out of the answer

Qwen3 may write `<think>…</think>` before its answer. The user shouldn't see
it as the answer, and Qwen's guidance is not to send old reasoning back.
[`ReasoningMiddleware.aafter_model`](../../../src/cmcoder/middleware/model.py#L156)
splits it off, keeps it in `additional_kwargs["reasoning_content"]`, and
[replaces the reply by id](../../../src/cmcoder/middleware/model.py#L165)
with only the visible text as `content`. (Servers that send reasoning as a
separate field are handled in the chat model itself, chapter 03.)

### `VisionMiddleware`: images for a model that can't see

Your images go into the conversation as LangChain's standard multimodal
content blocks:

```python
HumanMessage(content=[
    {"type": "text", "text": "what's wrong here?"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
])
```

A model that sees images gets them as they are. For one that doesn't, the
session first has the `visionModel` describe each image,
[before the turn starts](../../../src/cmcoder/runtime/session.py#L263),
with [`describe`](../../../src/cmcoder/middleware/vision.py#L105) (a
multimodal `HumanMessage` streamed with an idle timeout in
[`ask`](../../../src/cmcoder/middleware/vision.py#L87)), and saves the
descriptions on your message (`additional_kwargs`, so they're written once,
even across `--resume`). Then
[`VisionMiddleware.awrap_model_call`](../../../src/cmcoder/middleware/vision.py#L129)
replaces each image block with its description,
[in the request only](../../../src/cmcoder/middleware/vision.py#L136). The
state keeps the images, so switching to a model that sees them later still
has them.

### The tool guards

Three small `wrap_tool_call` examples in `guards.py`, each showing one way to
use the hook:

* [`LoopGuardMiddleware`](../../../src/cmcoder/middleware/guards.py#L164):
  the same call with the same arguments 3 times in a row is
  [answered with a `ToolMessage`](../../../src/cmcoder/middleware/guards.py#L184)
  telling the model to try something else. The tool never runs: *not calling
  the handler*.
* [`BashFileWorkMiddleware`](../../../src/cmcoder/middleware/guards.py#L126):
  `cat > file << EOF` or `sed -i` in Bash gets a hint to use Edit/Write the
  first time; the same command again runs (the model may have a reason).
* [`PlainToolTextMiddleware`](../../../src/cmcoder/middleware/guards.py#L193):
  calls the handler, then turns a result made of content blocks (MCP tools
  return those) into a plain string: *post-processing the result*.

Both guards that refuse remember the calls they've already let through
(`call_key`): LangGraph runs the tools node again when it resumes after a
permission prompt (chapter 08), and the same call must not be counted or
refused twice.

### The order, once more

From chapter 06, the model side outer to inner:
`TodoList → Vision → ContextEditing → ModelProfile → TextToolCalls → model`.
Read it with this chapter in mind: images are text before anything measures
the request; old outputs are cleared before `ModelProfile` sets the budget;
the text tool format is applied last, just before the wire. And the
`after_model` nodes run `ToolCallRepair` then `Reasoning`: the reply is fixed
before anything reads its text.

## Lab: a model wrapper of your own, inside cmcoder

[`labs/07_model_middleware.py`](labs/07_model_middleware.py) adds two
middlewares to cmcoder's agent with `extra_middleware` (so they're the
innermost model wrapper and the first `after_model` node):

* `ShowAndRewrite.awrap_model_call` prints the `model_settings` the outer
  layers set, then sends a copy with a line added to the system message and
  `max_tokens=300`.
* `Stamp.aafter_model` replaces the final answer with a copy that ends in
  `[checked]`: same id, so it's replaced, not appended.

The script ([`labs/07_model_middleware.json`](labs/07_model_middleware.json))
has the model write its tool call as *text*, then answer with a `<think>`
block. Run it with `--record`, to see what the server received:

```bash
uv run python -m cmcoder.testing.mock_server --script docs/learn/modules/labs/07_model_middleware.json \
    --port 8765 --record /tmp/lab-07.jsonl
uv run python docs/learn/modules/labs/07_model_middleware.py http://127.0.0.1:8765/v1 /tmp/lab-project /tmp/lab-07.jsonl
```

```text
[wrap_model_call] 1 messages, 8 tools
                  model_settings from outer middleware: {'max_tokens': 8192, 'parallel_tool_calls': True}
                  reply: 'Let me look.\n<tool_call>\n{"name": "Read", "arguments": {"file_path": "app.py"}}\n</tool_call>' tool_calls=[]
[wrap_model_call] 3 messages, 8 tools
                  model_settings from outer middleware: {'max_tokens': 8192, 'parallel_tool_calls': True}
                  reply: '<think>It returns a - b, so add() subtracts.</think>add() subtracts instead of adding.' tool_calls=[]

final state:
  HumanMessage 'what does add() do?'
  AIMessage    'Let me look.' ['Read']
  ToolMessage  '     1\tdef add(a, b):\n     2\t    return a - b'
  AIMessage    'add() subtracts instead of adding. [checked]'  reasoning: 'It returns a - b, so add() subtracts.'

what the server received in the last request:
  max_tokens: 300
  system message ends with: '\nAnswer in one short sentence.'
  assistant message sent back: {'content': 'Let me look.', 'name': 'cmcoder', 'role': 'assistant', 'tool_calls': [{'type': 'function', 'id': 'call_a7c5250a45ba', 'function': {'name': 'Read', 'arguments': '{"file_path": "app.py"}'}}]}
```

What to notice:

* `ModelProfileMiddleware` (outer) set `max_tokens` and
  `parallel_tool_calls`; our inner override won: the server got 300.
* Our wrapper saw the *raw* reply, `<tool_call>` text and no `tool_calls`.
  `TextToolCallsMiddleware` is *outside* ours, so it parsed the reply after we
  returned it: the state has a real `Read` call, and the tool ran.
* In the next request, that call went back to the server as a native
  `tool_calls` entry, as if the model had made it natively.
* The `<think>` text is in `reasoning_content`, not in the answer
  (`ReasoningMiddleware`), and `[checked]` replaced the answer: 4 messages,
  not 5.
* 8 tools: cmcoder's 7 plus `write_todos`, added by `TodoListMiddleware`.

## Exercises

1. **Watch the budget.** In `ShowAndRewrite`, print
   `request.model_settings["max_tokens"]` for a long conversation. Then set
   `"modelProfiles"` with a small `contextWindow` for the model in the lab's
   settings and see the number drop.
2. **Prompted mode.** Build the agent with a model profile that has
   `toolCalling: "prompted"` and print the `tools` and the end of the system
   message the server received (`--record`). Where did the tools go?
3. **A repair test.** Write a mock-server script whose first reply has
   arguments with a trailing comma, as a native tool call
   (`"arguments": "{\"file_path\": \"app.py\",}"` as a string). Does the tool
   run? Which middleware fixed it?
4. **Stretch: your own guard.** Write a `wrap_tool_call` middleware that
   refuses `Bash` commands containing `git push` with a `ToolMessage` (no
   handler call), add it with `extra_middleware`, and test it with a script.
   Which existing guard did you just copy?

## Check yourself

1. What is in a `ModelRequest`, and how do you change one?
2. A `wrap_model_call` replaces old tool outputs in `request.messages`. Are
   they gone from the conversation?
3. How does an `after_model` hook change the model's reply instead of adding
   a second one?
4. Why is `TextToolCallsMiddleware` the innermost model wrapper?
5. In the lab, why did our wrapper see no `tool_calls` when the state ended
   up with one?
6. How does a `wrap_tool_call` stop a tool from running?

<details><summary>Answers</summary>

1. The model, the system message, the messages, the tools, `tool_choice`,
   `response_format`, `model_settings`, the state and the runtime. Requests
   are immutable: `request.override(...)` returns a changed copy.
2. No. The change is in that request only; the state still holds them (that
   is how `ModelProfileMiddleware`'s retry works).
3. It returns `{"messages": [copy]}` where the copy has the reply's id: the
   `add_messages` reducer replaces the message with that id.
4. It changes the wire format (no `tools`, tools in the prompt, history as
   text) just before the server. Inside everything else, so no other
   middleware has to know which mode is on.
5. `TextToolCallsMiddleware` wraps ours from the outside: it parsed the text
   into a tool call after our wrapper returned the raw response.
6. By not calling `handler` and returning a `ToolMessage` itself (with the
   call's id): the model sees it as the tool's result.

</details>

[← 06 Building the agent](06-agent.md) · [Index](00-index.md) · Next: [08 Middleware 2: tools and people →](08-middleware-tools.md)
