"""Chapter 04 lab: cmcoder's tools as LangChain tools, called by hand.

No mock server needed: we call the tools directly, as LangGraph's tools node
would, and look at what the model is shown and what it gets back.

    python 04_tools.py <project dir>

The project dir needs an app.py (see 00-index.md, "Running the labs").
"""

import asyncio
import json
import sys
from pathlib import Path

from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from cmcoder.tools.adapter import as_langchain_tools
from cmcoder.tools.base import ToolContext
from cmcoder.tools.registry import default_tools


async def main(project: str) -> None:
    root = Path(project)
    # The session's live context: a shell (started on first use), the files
    # read so far, the output budget. It is not part of the graph state.
    ctx = ToolContext(cwd=root, project_root=root)
    tools = {t.name: t for t in as_langchain_tools(default_tools(), ctx)}
    edit, read = tools["Edit"], tools["Read"]

    # 1. What the model is shown: the JSON schema built from args_schema.
    print("1. Edit as the model sees it:")
    print(json.dumps(convert_to_openai_tool(edit), indent=2)[:700], "...\n")

    # 2. A tool call, as the tools node runs it: a ToolCall in, a ToolMessage out.
    def call(name: str, args: dict, n: int) -> dict:
        return {"name": name, "args": args, "id": f"call_{n}", "type": "tool_call"}

    change = {"file_path": "app.py", "old_string": "a - b", "new_string": "a + b"}
    msg = await edit.ainvoke(call("Edit", change, 1))
    print("2. Edit before Read ->", msg.status, "|", msg.content)

    msg = await read.ainvoke(call("Read", {"file_path": "app.py"}, 2))
    print("3. Read ->", msg.status, "|", msg.content.replace("\n", " / "))
    print("   ctx.read_files now holds:", [p.name for p in ctx.read_files])

    msg = await edit.ainvoke(call("Edit", change, 3))
    print("4. Edit after Read ->", msg.status, "|", msg.content.splitlines()[0])

    # 3. Bad arguments never reach the tool: pydantic rejects them first.
    try:
        await read.ainvoke(call("Read", {"file_path": "app.py", "offset": 0}, 4))
    except ValidationError as e:
        print("5. Read with offset=0 ->", type(e).__name__, "|", e.errors()[0]["msg"])

    # 4. Output limits: a long result is cut in the middle, not dropped.
    many = root / "many"
    many.mkdir(exist_ok=True)
    for i in range(30):
        (many / f"file{i:02}.txt").write_text("x")
    ctx.max_output_chars = 160
    msg = await tools["Glob"].ainvoke(call("Glob", {"pattern": "many/*.txt"}, 5))
    print("6. Glob with a 160-char budget ->", msg.content.replace("\n", " "))
    for f in many.iterdir():
        f.unlink()
    many.rmdir()
    ctx.max_output_chars = 30_000

    # The persistent shell keeps state between calls (cd, variables).
    await tools["Bash"].ainvoke(call("Bash", {"command": "export GREETING=hi && cd /"}, 6))
    msg = await tools["Bash"].ainvoke(call("Bash", {"command": "echo $GREETING from $PWD"}, 7))
    print("7. Second Bash call ->", msg.content)
    await ctx.close_shells()
    (root / "app.py").write_text("def add(a, b):\n    return a - b\n")  # put the bug back


asyncio.run(main(sys.argv[1]))
