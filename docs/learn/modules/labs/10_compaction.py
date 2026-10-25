"""Chapter 10 lab: watch auto-compaction rewrite the conversation.

A tiny context window (6000 tokens, compaction at 30%) makes a single big file
read enough to trigger LangChain's SummarizationMiddleware (cmcoder's
CompactionMiddleware) on the next turn. Script: labs/10_compaction.json.
Start the mock server with `--record <file>` and pass the same file as the
third argument to see the summary request that went to the "small model".

    python 10_compaction.py http://127.0.0.1:8765/v1 <project dir> [record file]
"""

import asyncio
import json
import sys
from pathlib import Path

from langchain_core.messages import AnyMessage

from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import Settings
from cmcoder.protocol import events as ev
from cmcoder.runtime.session import Session


def show(title: str, messages: list[AnyMessage]) -> None:
    print(f"{title}: {len(messages)} messages")
    for m in messages:
        text = m.text.replace("\n", " ")
        print(f"   {m.type:6} {text[:70]!r}")


def write_big_file(project: str) -> None:
    lines = [f"line {i}: some text to fill the context window up" for i in range(400)]
    Path(project, "big.txt").write_text("\n".join(lines))  # ~20K characters


def show_requests(record: str) -> None:
    # Every request the "gateway" got, in order: the agent's and the summariser's.
    print("\nrequests the gateway got:")
    for line in Path(record).read_text().splitlines():
        messages = json.loads(line)["messages"]
        first = str(messages[0]["content"])
        kind = "summary" if "<cmcoder-compaction>" in first else "agent"
        print(f"   {kind:8} {len(messages)} messages, starts {first[:45]!r}")


async def main(base_url: str, project: str) -> None:
    settings = Settings.model_validate(
        {
            "providers": {"mock": {"baseUrl": base_url}},
            "model": "mock:qwen3-27b",
            # A model profile that says the window is only 6000 tokens...
            "modelProfiles": [{"match": "*", "contextWindow": 6000}],
            # ...and compaction at 30% of it (the default is 80%).
            "autoCompactThreshold": 0.3,
        }
    )
    agent = build_agent(settings, AgentOptions(cwd=Path(project), interactive=False))
    session = Session(agent)

    async for _ in session.run("read big.txt"):
        pass
    show("after turn 1", await session.messages())

    async for event in session.run("and now?"):
        if isinstance(event, ev.Compacted):
            print(
                f"\nCOMPACTED ({event.trigger}): {event.summarized_messages} messages summarised, "
                f"~{event.tokens_before} -> ~{event.tokens_after} tokens\n"
            )
    show("after turn 2", await session.messages())
    await agent.close()


write_big_file(sys.argv[2])
asyncio.run(main(sys.argv[1], sys.argv[2]))
if len(sys.argv) > 3:
    show_requests(sys.argv[3])
