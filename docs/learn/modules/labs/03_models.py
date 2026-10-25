"""Chapter 03 lab: the chat model up close.

    python 03_models.py http://127.0.0.1:8765/v1 <project dir>

Start the mock server WITH a key, so part 5 can show an auth error:

    python -m cmcoder.testing.mock_server --script labs/03_models.json --port 8765 --api-key x

and run the lab with CMCODER_API_KEY=x. (<project dir> isn't used here; it's
kept so every lab is run the same way.)
"""

import asyncio
import os
import sys

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from cmcoder.config.settings import Settings
from cmcoder.models.chat import REASONING_KEY, build_chat_model
from cmcoder.models.errors import classify
from cmcoder.models.windows import parse_context_window


async def main(base_url: str) -> None:
    settings = Settings.model_validate(
        {"providers": {"mock": {"baseUrl": base_url}}, "model": "mock:qwen3-27b"}
    )
    bundle = build_chat_model(settings)
    model = bundle.model
    p = bundle.profile

    print("== 1. What cmcoder knows about the model (its profile)")
    print(f"ref {bundle.ref}: window {p.context_window} ({p.context_window_source}),")
    print(f"  thinking switch {p.thinking_switch}, output limit sent as {p.max_tokens_param}")
    print("LangChain's own profile:", model.profile)

    print("\n== 2. Streaming: AIMessageChunks, added together")
    chunks: list[AIMessageChunk] = []
    async for chunk in model.astream([HumanMessage("What is 2+2?")]):
        chunks.append(chunk)
    print(f"{len(chunks)} chunks; text pieces: {[c.text for c in chunks if c.text]}")
    full = chunks[0]
    for c in chunks[1:]:
        full = full + c  # merges text, tool-call pieces, additional_kwargs, usage
    print("text          :", repr(full.text))
    print("reasoning     :", repr(full.additional_kwargs.get(REASONING_KEY)))
    print("usage_metadata:", full.usage_metadata)
    print("cost (header) :", full.response_metadata.get("cost"))

    print("\n== 3. A variant for quick jobs: model_copy vs. bind")
    quick = bundle.quick()
    bound = model.bind(max_tokens=40)
    print(
        "quick() extra_body:", quick.extra_body, "| a chat model:", isinstance(quick, BaseChatModel)
    )
    print(
        "bind()            :",
        type(bound).__name__,
        "| a chat model:",
        isinstance(bound, BaseChatModel),
    )
    reply = await bound.ainvoke([HumanMessage("Title for: fix the login bug")])
    print("bound reply       :", repr(reply.text))

    print("\n== 4. Learning the context window from a server's error")
    vllm = "This model's maximum context length is 40960 tokens. However, you requested 41000"
    print("parse_context_window ->", parse_context_window(vllm))

    print("\n== 5. Errors: the OpenAI SDK's exceptions, turned into protocol events")
    wrong = ChatOpenAI(
        base_url=base_url, api_key=SecretStr("wrong"), model="qwen3-27b", max_retries=0
    )
    try:
        await wrong.ainvoke("hi")
    except Exception as e:
        err = classify(e)
        print(f"{type(e).__name__} -> kind={err.kind!r}, hint={err.hint!r}")
    assert os.environ.get("CMCODER_API_KEY"), "run with CMCODER_API_KEY=x"


asyncio.run(main(sys.argv[1]))
