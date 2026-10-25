"""Chapter 05 lab: run commands in cmcoder's Bash sandbox and see what it blocks.

Linux/WSL2 needs bubblewrap (`sudo apt install bubblewrap`); macOS uses
sandbox-exec; native Windows has no sandbox (the lab says so and stops).

    python 05_sandbox.py <project dir>

It makes a pretend credentials folder next to the project (with a token in
it) and hides it from the sandbox with `denyReadPaths`.
"""

import asyncio
import sys
from pathlib import Path

from cmcoder.compat import find_shell
from cmcoder.config.settings import SandboxConfig
from cmcoder.sandbox import Sandbox, detect
from cmcoder.tools.shell import PersistentShell

COMMANDS = [
    "echo inside > note.txt && cat note.txt",  # the project is writable
    "echo outside > ~/cmcoder-lab-escape.txt",  # the home folder is not
    "cat ../lab-credentials/token.txt",  # denyReadPaths (and ~/.ssh, ~/.aws, ...) are hidden
    "touch .git/config",  # .git is read-only inside the sandbox
    "echo $CMCODER_API_KEY | wc -c",  # cmcoder's key isn't passed in
    "curl -sS -m 5 -o /dev/null https://example.com && echo reached",  # not an allowed host
]


async def main(root: Path, creds: Path) -> None:
    avail = detect()
    print("sandbox here:", avail.kind or f"none ({avail.reason})")
    shell = find_shell()
    if not avail.ok or shell is None:
        return
    cfg = SandboxConfig.model_validate(
        {"network": {"allowedHosts": ["pypi.org"]}, "denyReadPaths": [str(creds)]}
    )
    sandbox = Sandbox(cfg, root, avail.kind or "")
    print("summary     :", sandbox.summary(), "\n")
    sh = PersistentShell(root, shell, sandbox=sandbox)
    try:
        for command in COMMANDS:
            res = await sh.run(command, timeout=20)
            out = res.output.strip().replace("\n", " | ")[:110]
            print(f"$ {command}\n  exit {res.exit_code}: {out}")
    finally:
        await sh.close()
        await sandbox.close()


project = Path(sys.argv[1]).resolve()
credentials = project.parent / "lab-credentials"
credentials.mkdir(exist_ok=True)
(credentials / "token.txt").write_text("not-a-real-token\n")
print(
    "outside the sandbox: cat ../lab-credentials/token.txt ->",
    (credentials / "token.txt").read_text().strip(),
)
try:
    asyncio.run(main(project, credentials))
finally:
    (project / "note.txt").unlink(missing_ok=True)
    (credentials / "token.txt").unlink()
    credentials.rmdir()
