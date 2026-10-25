"""Chapter 14 lab: be an IDE. Start `cmcoder --protocol stdio`, send a user
message, answer the permission prompt, and print the events that come back.

    python 14_stdio_client.py http://127.0.0.1:8765/v1 <project dir> [allow|deny]

Mock server script: 14_fix_add.json. The project needs the buggy app.py (see
the course index). One JSON message per line goes in on stdin; one JSON event
per line comes out on stdout (protocol/messages.py, protocol/events.py).
"""

import asyncio
import json
import os
import sys
from pathlib import Path


async def main(base_url: str, project: str, answer: str) -> None:
    env = {
        **os.environ,
        "CMCODER_BASE_URL": base_url,
        "CMCODER_MODEL": "qwen3-27b",
        "CMCODER_API_KEY": os.environ.get("CMCODER_API_KEY", "x"),
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "cmcoder", "--protocol", "stdio", "--client", "vscode",
        cwd=project, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )  # fmt: skip
    assert proc.stdin is not None and proc.stdout is not None

    async def send(**message: object) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(message).encode() + b"\n")
        await proc.stdin.drain()
        print(f">> {message['type']}")

    async for line in proc.stdout:
        event = json.loads(line)
        kind = event["type"]
        if kind == "assistant_delta":
            continue  # streamed tokens: one per chunk, too many to print
        detail = {
            "system_init": lambda e: f"model={e['model']}",
            "tool_use": lambda e: e["label"],
            "tool_result": lambda e: f"{e['name']} error={e['is_error']}",
            "permission_request": lambda e: f"{e['label']} (request_id={e['request_id'][:8]}…)",
            "assistant_message": lambda e: repr(e["text"]),
            "result": lambda e: f"{e['subtype']}: {e['result']!r}",
        }.get(kind, lambda e: "")(event)
        print(f"<< {kind:20} {detail}")
        if kind == "system_init":
            await send(type="user_message", text="fix add")
        elif kind == "permission_request":
            # The agent is paused in a LangGraph interrupt() until this arrives.
            await send(
                type="permission_response", request_id=event["request_id"], allow=answer == "allow"
            )
        elif kind == "result":
            await send(type="shutdown")
    await proc.wait()


asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "allow"))
print("\napp.py now:", Path(sys.argv[2], "app.py").read_text().strip().splitlines()[-1].strip())
