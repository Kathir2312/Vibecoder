"""The interactive REPL in a real pseudo-terminal (POSIX only)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from .conftest import API_KEY

pexpect = pytest.importorskip("pexpect")
pytestmark = pytest.mark.skipif(os.name != "posix", reason="pseudo-terminals are POSIX-only")

PROMPT = r"\] >"  # "[default] > "


def test_repl_permission_prompt_and_rewind(mock_server, project: Path) -> None:
    (project / "app.py").write_text("x = 1\n")
    server = mock_server(
        [
            {"tool_calls": [{"name": "Read", "arguments": {"file_path": "app.py"}}]},
            {
                "tool_calls": [
                    {
                        "name": "Edit",
                        "arguments": {
                            "file_path": "app.py",
                            "old_string": "x = 1",
                            "new_string": "x = 2",
                        },
                    }
                ]
            },
            {"content": "Changed x."},
        ]
    )
    env = {
        **os.environ,
        "CMCODER_BASE_URL": server.base_url,
        "CMCODER_MODEL": "qwen3-27b",
        "CMCODER_API_KEY": API_KEY,
        "NO_PROXY": "127.0.0.1",
        "TERM": "xterm",
    }
    child = pexpect.spawn(
        sys.executable, ["-m", "cmcoder"], cwd=str(project), env=env, encoding="utf-8", timeout=30
    )
    try:
        child.expect(PROMPT)
        child.sendline("set x to 2")
        child.expect("1 yes")  # the Edit asks in the default mode
        child.sendline("1")
        child.expect("Changed x")
        child.expect(PROMPT)
        assert (project / "app.py").read_text() == "x = 2\n"
        child.sendline("/rewind 1 code")
        child.expect("Rewound")
        child.expect(PROMPT)
        assert (project / "app.py").read_text() == "x = 1\n"
        child.sendline("/exit")
        child.expect(pexpect.EOF)
    finally:
        child.close(force=True)
