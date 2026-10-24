"""Chapter 09 lab: a conversation saved in SQLite, its checkpoint history,
and a fork (what /rewind does to the conversation).

Script: labs/09_two_turns.json. Two user turns go through cmcoder's own
`Session` on an `AsyncSqliteSaver` (the same store `cmcoder --continue`
uses, under $CMCODER_CONFIG_DIR). Then we list the thread's checkpoints, open
the database again as if cmcoder had restarted, and rewind before turn 2.

    python 09_threads_and_time_travel.py http://127.0.0.1:8765/v1 <project dir>
"""

import asyncio
import sys
from pathlib import Path

from cmcoder.agents.main import AgentOptions, build_agent
from cmcoder.config.settings import Settings
from cmcoder.runtime.session import Session
from cmcoder.runtime.store import DB_NAME, SessionStore, sessions_dir


def texts(messages: list) -> list[str]:
    return [f"{type(m).__name__[:-7]}: {m.text[:34]}" for m in messages]


async def main(base_url: str, root: Path) -> None:
    settings = Settings.model_validate(
        {"providers": {"mock": {"baseUrl": base_url}}, "model": "mock:qwen3-27b"}
    )
    opts = AgentOptions(cwd=root, interactive=False)

    # 1. Two turns on one thread, saved in SQLite.
    async with SessionStore.open(root) as store:
        session = Session(build_agent(settings, opts, checkpointer=store.checkpointer), store=store)
        for prompt in ("what does add do?", "how do I fix it?"):
            async for _ in session.run(prompt):
                pass
        thread = session.session_id
        print("database:", sessions_dir(root) / DB_NAME)
        print("thread  :", thread[:8], "| index row:", (await store.list())[0].turns, "turns")
        await session.close()

    # 2. A new process would do exactly this: open the file, point at the thread.
    async with SessionStore.open(root) as store:
        agent = build_agent(settings, opts, checkpointer=store.checkpointer)
        session = Session(agent, store=store)
        await session.resume(thread)
        print("\nreopened:", texts(await session.messages()))

        print("\ncheckpoint history (newest first):")
        history = [s async for s in agent.graph.aget_state_history(session.config)]
        for snap in history:
            n = len(snap.values.get("messages", []))
            step = (snap.metadata or {}).get("step")
            print(f"  step {step:>3}  {n} messages  next={snap.next}")
        before_rewind = len(history)

        # 3. /rewind 2 (conversation only): fork from the checkpoint before turn 2.
        await session.rewind(2, code=False)
        print("\nafter /rewind 2:", texts(await session.messages()))
        history = [s async for s in agent.graph.aget_state_history(session.config)]
        print(f"checkpoints: {before_rewind} before the fork, {len(history)} after (none deleted)")
        # The newest checkpoint of the old branch is still there, and readable.
        old = max(history, key=lambda s: len(s.values.get("messages", [])))
        print("old branch still readable:", len(old.values["messages"]), "messages")


asyncio.run(main(sys.argv[1], Path(sys.argv[2]).resolve()))
