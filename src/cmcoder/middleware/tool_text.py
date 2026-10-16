"""Tool calls written as text, for servers without a tool-call parser.

Qwen's chat template has the model write tool calls like this:

    <tool_call>
    {"name": "Read", "arguments": {"file_path": "app.py"}}
    </tool_call>

A server with a tool-call parser (vLLM `--enable-auto-tool-choice
--tool-call-parser hermes`) turns that into native `tool_calls`. Without one,
the text arrives as ordinary content. This middleware handles both cases:

* "native" mode: if a reply has no native tool calls but contains these
  blocks for known tools, they become real `tool_calls`;
* "prompted" mode (profile `toolCalling: "prompted"`, or switched on by itself
  when the server rejects the `tools` parameter): no `tools` are sent. They are
  described in the system prompt instead, in the same format, and the history
  is rewritten as text, the way Qwen's template does it.

LANGCHAIN CONCEPTS IN THIS FILE

* `awrap_model_call` can rewrite the whole request, not only settings:
  `request.override(tools=[], system_message=..., messages=...)`.
* `convert_to_openai_tool(tool)`: the JSON Schema LangChain would have sent
  for a tool. We reuse it to describe the tools in the prompt.
* The wrapper can also change the *response*: we return a new `ModelResponse`
  whose `AIMessage` carries the parsed `tool_calls`. The rest of the agent
  can't tell these apart from native tool calls.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import openai
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
)
from langchain_core.utils.function_calling import convert_to_openai_tool

OPEN, CLOSE = "<tool_call>", "</tool_call>"
_BLOCK = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.S)
_NO_TOOL_SUPPORT = re.compile(
    r"enable-auto-tool-choice|tool.?choice.{0,40}(not supported|requires)|"
    r"does not support (tools|function calling|tool)|tools? (are|is) not supported",
    re.I,
)


def _loads(text: str) -> Any:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    for candidate in (text, re.sub(r",\s*([}\]])", r"\1", text)):
        try:
            return json.loads(candidate, strict=False)
        except ValueError:
            continue
    return None


def extract(content: str, known_tools: set[str]) -> tuple[str, list[ToolCall]]:
    """Split text tool calls out of `content`: (remaining text, calls).

    Only blocks naming a known tool with valid JSON count; anything else stays
    in the text, so prose that merely mentions <tool_call> is left alone.
    """
    calls: list[ToolCall] = []
    kept: list[str] = []
    pos = 0
    for m in _BLOCK.finditer(content):
        data = _loads(m.group(1))
        name = data.get("name") if isinstance(data, dict) else None
        if not isinstance(data, dict) or name not in known_tools:
            continue
        args = data.get("arguments", data.get("parameters", {}))
        if isinstance(args, str):
            args = _loads(args) or {}
        calls.append(
            ToolCall(
                name=str(name), args=args, id=f"call_{uuid.uuid4().hex[:12]}", type="tool_call"
            )
        )
        kept.append(content[pos : m.start()])
        pos = m.end()
    if not calls:
        return content, []
    kept.append(content[pos:])
    return "".join(kept).strip(), calls


def tools_prompt(tools: Sequence[Any]) -> str:
    """The tool description Qwen's own chat template uses."""
    lines = [json.dumps(convert_to_openai_tool(t), ensure_ascii=False) for t in tools]
    return (
        "# Tools\n\nYou may call one or more functions to assist with the user query.\n\n"
        "You are provided with function signatures within <tools></tools> XML tags:\n<tools>\n"
        + "\n".join(lines)
        + "\n</tools>\n\nFor each function call, return a json object with function name and "
        "arguments within <tool_call></tool_call> XML tags:\n<tool_call>\n"
        '{"name": <function-name>, "arguments": <args-json-object>}\n</tool_call>'
    )


def as_text_history(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    """Rewrite tool calls and results as plain text, as Qwen's template does."""
    out: list[AnyMessage] = []
    for m in messages:
        if isinstance(m, AIMessage) and m.tool_calls:
            blocks = [
                f"{OPEN}\n{json.dumps({'name': c['name'], 'arguments': c['args']}, ensure_ascii=False)}\n{CLOSE}"
                for c in m.tool_calls
            ]
            text = "\n".join(x for x in (m.text, *blocks) if x)
            out.append(AIMessage(content=text, id=m.id))
        elif isinstance(m, ToolMessage):
            out.append(HumanMessage(content=f"<tool_response>\n{m.text}\n</tool_response>"))
        else:
            out.append(m)
    return out


def no_tool_support(error: Exception) -> bool:
    return isinstance(error, openai.BadRequestError) and bool(_NO_TOOL_SUPPORT.search(str(error)))


class TextToolCallsMiddleware(AgentMiddleware):
    """Native tool calls when the server supports them, text tool calls when not."""

    def __init__(self, prompted: bool = False) -> None:
        super().__init__()
        self.prompted = prompted

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        known = {getattr(t, "name", "") for t in request.tools}
        if not self.prompted:
            try:
                return _parse_text_calls(await handler(request), known)
            except openai.BadRequestError as e:
                if not request.tools or not no_tool_support(e):
                    raise
                self.prompted = True  # from now on, for this session
        system = request.system_message.text if request.system_message else ""
        prompted = request.override(
            tools=[],
            system_message=SystemMessage(
                "\n\n".join(x for x in (system, tools_prompt(request.tools)) if x)
            ),
            messages=as_text_history(request.messages),
        )
        return _parse_text_calls(await handler(prompted), known)


def _parse_text_calls(response: ModelResponse, known: set[str]) -> ModelResponse:
    result = list(response.result)
    for i, m in enumerate(result):
        if not isinstance(m, AIMessage) or m.tool_calls or OPEN not in m.text:
            continue
        text, calls = extract(m.text, known)
        if calls:
            result[i] = m.model_copy(update={"content": text, "tool_calls": calls})
    return ModelResponse(result=result, structured_response=response.structured_response)


class ToolCallHoldback:
    """Stream filter for the UI: pass text through until a <tool_call> tag
    starts, then hold everything back, so tool-call JSON isn't shown as the reply."""

    def __init__(self) -> None:
        self.held = ""
        self.inside = False

    def feed(self, text: str) -> str:
        if self.inside:
            self.held += text
            return ""
        buf = self.held + text
        i = buf.find(OPEN)
        if i >= 0:
            self.inside = True
            self.held = buf[i:]
            return buf[:i]
        for k in range(min(len(OPEN) - 1, len(buf)), 0, -1):  # a partial "<tool_ca"
            if OPEN.startswith(buf[-k:]):
                self.held = buf[-k:]
                return buf[:-k]
        self.held = ""
        return buf

    def flush(self) -> str:
        out = "" if self.inside else self.held
        self.held = ""
        self.inside = False
        return out
