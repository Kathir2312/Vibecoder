# Security review: the LangChain / LangGraph engine (Phase 4)

What was reviewed: everything the rebuild changed. The permission rules, the
high-risk command checks, the sandbox, settings layering and project trust
came over from cmcoderagent unchanged and had their own reviews there; their
test suites were ported and run against the new engine (below).

Focus, from the plan: interrupt bypass, prompt injection through tool and MCP
output, the checkpoint database. Plus what is new here: LangGraph's
serializer, the middleware stack, LangChain callbacks and response metadata,
nested graphs (subagents, the critic, the team).

## How it was tested

| Kind | Tool | Scope | Result |
|---|---|---|---|
| SAST | Semgrep 1.181.0, `semgrep-rules` `17e31e1` (python, generic/secrets) | `src/`, `packaging/`: 82 files | 44 results, all reviewed: no finding (see "Checked, no change needed") |
| SCA | pip-audit (OSV / PyPI advisories), against `uv.lock` | 285 locked packages | No known vulnerabilities |
| Tests | pytest | ported: `test_permissions_shell`, `test_sandbox` (bubblewrap, for real), `test_tls`, `test_auth`, `test_managed`, `test_project_files`; new: `test_hardening`, the tool-call id regression in `test_hooks` | pass |
| Manual | reading the new code paths | middleware, session, stdio, gateway, store | 2 findings, fixed |

## Findings

| # | Severity | Finding | Status |
|---|---|---|---|
| 1 | Medium | A PreToolUse hook's "allow" could carry over to a different tool call that got the same id from the model server | Fixed |
| 2 | Low | Checkpoints could rebuild any importable class named in the database | Hardened |

### 1. Tool-call ids from the server keyed per-call caches

LangGraph runs a tool call's middleware again when it resumes after a
permission prompt, so three middlewares remembered per call what they had
already done: the PreToolUse hook's answer (`PermissionMiddleware`), the Bash
file-work redirect, and the loop guard's count. They keyed it by the tool
call's id, which comes from the model server. Some servers number tool calls
`call_0`, `call_1`, ... in every reply. Then a hook that allowed one call
(say, a write to `ok.txt`, which skips the prompt) also "allowed" a later
call with the same id (a write to another file) without asking.

Fixed: the key is the id plus the tool's name and its arguments
(`middleware/permission.py: call_key`). A resumed call has all three the
same; another call never does. Regression test:
`test_hooks.py::test_a_reused_tool_call_id_doesnt_reuse_a_hooks_answer` (fails
before the fix).

### 2. Checkpoint deserialisation

`JsonPlusSerializer` (LangGraph's default) saves a class in the state as its
module, name and data, and by default builds any importable class when it
loads. The state holds a few of ours (the critic's `Verdict`, the team's
`Stories` and `TestReport`). The database is owner-only, so changing it means
already having the user's rights, but a tampered or copied database should
still not make cmcoder build arbitrary objects.

Hardened: both checkpointers get `JsonPlusSerializer(allowed_msgpack_modules=
[...])` with exactly our types (`runtime/serde.py`); LangGraph's own safe
types (messages, `Send`, `Interrupt`, ...) stay allowed. Test:
`test_hardening.py`.

## Checked, no change needed

**Interrupts (permission prompts).**
- Every pending interrupt is asked for on its own, and the answer goes back
  under that interrupt's id (`Command(resume={id: answer})`). Two parallel
  tool calls can't get each other's answers.
- An interrupt of an unknown kind, or with nobody to ask (`-p` mode), is
  answered "deny".
- `remember` ("always allow") from a client counts only where the middleware
  itself decided the call may be remembered. Not for high-risk commands, a
  hook's "ask", or locked rules. So an IDE can't make a high-risk command
  permanent.
- A `permission_response` with an unknown `request_id` is a protocol error.
- The checked arguments are the executed ones: repair middlewares run after
  the model call and before the tool node, the permission check is in
  `awrap_tool_call` on the final arguments, and nothing inside it changes
  them.

**Subagents, the critic, the team.**
- They run with the same permission policy, hooks (SubagentStop) and sandbox.
- Their permission prompts pause the main graph, so the user still answers
  each one.
- The critic's tools are read-only (test: `test_critic.py`).
- Project agent definitions, including the team's roles, are used only in a
  trusted project.

**Prompt injection through tool and MCP output.** Output the model reads can
contain instructions. As in cmcoderagent, nothing the model does because of
it bypasses the permission rules, the high-risk checks or the sandbox.
- MCP tools are named and described with their server
  (`mcp__server__tool`, "[MCP server X]"), so prompts and rules show where
  they come from.
- A project's MCP server needs approval first. The approval shows its
  command, `env`, `headers` and the environment variables it reads, in the
  REPL and in IDEs (cmcoderagent's Phase 3 finding 2 still holds in the
  rewritten manager).
- Text tool calls (`<tool_call>` in prompted mode) are read only from the
  model's own replies, never from tool output.

**The checkpoint database and other files.**
- `~/.cmcoder/projects/<hash>/` is 0700, `sessions.db` 0600; SQLite gives its
  journal files the database's permissions.
- File snapshots are written into 0700 folders.
- The caches (model info, context windows) hold model names and numbers only.

**What leaves the machine.**
- LangSmith tracing is removed from the environment at start unless
  `CMCODER_ENABLE_LANGSMITH=1` (test: `test_hardening.py`).
- OpenTelemetry stays off by default and sends counts and timings only. A
  project can't set its endpoint (test: `test_telemetry.py`).
- `include_response_headers` is on, for LiteLLM's cost header. The headers
  are reduced to the cost before they reach `response_metadata`, which is
  saved with the conversation, so a gateway's cookies or internal headers
  never land in the database.
- The API key is not in the sandboxed shell's environment (test:
  `test_sandbox.py`).

**The gateway module.**
- It uses the same TLS context (OS trust store plus `caCertPath`), proxy and
  auth as the chat model. Python 3.13's strict certificate checks apply, and
  the TLS tests now use a properly formed test CA.
- The doctor fetches an untrusted certificate without verifying it only to
  show its issuer. It sends no data on that connection.

**Semgrep's results.**
- `dangerous-subprocess-use-audit` / `dangerous-asyncio-create-exec-audit`
  (9): the Bash tool, ripgrep, git status for the prompt, the sandbox and the
  packaging gate start programs by design, with argument lists, behind the
  permission rules.
- `return-not-in-function` (29) and `is-function-without-parentheses` (5):
  lambdas and the `is_error` field (false positives).
- `string-concat-in-list` (2): intended f-string continuations.
- `tempfile-without-flush` (1): the file is closed (and flushed) before it is
  read.
- `arbitrary-sleep` (1): a retry back-off.

## Re-scan after the later work (November 2026)

The Phase 4 review predates the Browser tool, code search with LanceDB, notes, `/review`,
`/webtest` and the IDE chat's new consent and Bash cards. Everything below was run on `main`
at `7ac3ddb`.

| Kind | Tool | Scope | Result |
|---|---|---|---|
| SAST | Bandit 1.x | `src/`, `packaging/`: 116 files | 61 results, all *low*, none medium or high: no finding (below) |
| SAST | Semgrep 1.183.0, `semgrep-rules` `16b9e6d` (python, generic/secrets, javascript, typescript, github-actions) | `src/`, `packaging/`, the chat panel, `vscode/src`, `.github/`: 170 files | 83 results: **1 real (fixed)**, the rest false positives (below). The secrets rules matched nothing |
| SCA | pip-audit 2.10.1 (OSV / PyPI advisories) against `uv.lock`, platform markers removed so Windows and macOS packages count too | 116 pins (114 names) | **Found 2 packages, fixed**: Pillow 12.3.0 (39 advisories, fixed in 13.0.0) and certifi 2026.7.22 (ships the `emSign ECC Root CA - G3` root certificate, which 2026.10.18 removes). Now none |
| SCA | `npm audit` | `clients/web-panel` | none |
| SCA | `npm audit` | `vscode` | 5 (2 high, 3 moderate), all in the build tool `@vscode/vsce`, none in the `.vsix`: `npm audit --omit=dev` reports none. 3 fixed by `npm audit fix`; 2 *moderate* `minimatch` DoS advisories remain (below) |
| SCA | `packaging/osv_check.py` (OSV for the NuGet and OpenJFX packages in the IDE plugins) | Visual Studio extension, NetBeans plugin | **Not run**: this session cannot reach `api.osv.dev`. Newtonsoft.Json 13.0.3, WebView2 1.0.2903.40 and OpenJFX 21.0.12 look current; run it where the network allows |
| Tests | pytest, panel tests, VS Code tests | | 739 passed; 112; 7 |

### Fixed

1. **Pillow and certifi** (`pyproject.toml`: `pillow>=13.0.0`, `uv.lock`). Pillow decodes the
   images users attach and the browser's screenshots, so its decoder bugs (out-of-bounds reads,
   a use-after-free in TIFF handling) matter here. Image tests pass on 13.0.0.
2. **`ci.yml` pinned two actions by tag** (`actions/checkout@v4`, `astral-sh/setup-uv@v6`) while
   every other workflow pins by commit SHA; a moved tag could change what runs in CI. Both now
   use the same SHAs as the other workflows.
3. `middleware/permission.py`: two identical branches in `why_asked` merged (no behaviour change).

### Checked, no change needed

- **Bandit (61, all low).** `B101` (27): `assert` for type narrowing, never for a check that
  matters. `B603`/`B404`/`B607` (21): programs started with argument lists, never `shell=True`;
  the Bash tool, ripgrep, git, the sandbox and the packaging gate do it by design, behind the
  permission rules. `B110`/`B112` (13): `except … pass` in clean-up and "try the next one" code
  (closing a browser, keyring lookups). No `eval`, `exec`, `pickle`, `yaml.load`,
  `verify=False` or `os.system` anywhere in `src/` or `packaging/`.
- **Semgrep, the panel and extensions.** `insufficient-postmessage-origin-validation` (2):
  both pages call `fromHost(e)` first (`bridge.ts`), which checks the sender's window and origin.
  `jquery-insecure-method` and `prohibit-jquery-html` (10): no jQuery; they are DOM `append`
  calls with text set through `textContent`, and Markdown goes through the allow-list sanitiser.
  `html-in-template-string` (2): the pages' HTML only interpolates a random nonce, the webview's
  `cspSource`, webview URIs and `escapeHtml`'d names. `unsafe-dynamic-method` is the test driver,
  never shipped.
- **Semgrep, the rest.** `sha1` (5): used for cache keys and file fingerprints, all with
  `usedforsecurity=False` (it also keeps them working where OpenSSL is in FIPS mode).
  `regex_dos` (1): the chunker's definition pattern, tried on 300 000-character
  lines of repeated keywords: linear, no backtracking. `dynamic-urllib-use-detected`: a
  fixed https address. The remaining kinds are as in Phase 4 (lambdas, `is_error` fields,
  intended string continuations, a retry back-off) plus `useless-if-body` (fixed, above).
- **`@vscode/vsce`'s `minimatch` (2 moderate).** A denial of service through crafted glob
  patterns. `vsce` only runs on the build machine over this repository's own `.vscodeignore`;
  the fix needs `npm audit fix --force`, which replaces `vsce` with an older major version.
  Left as it is.

### Run it again

```
uvx bandit -r src packaging
git clone --depth 1 https://github.com/semgrep/semgrep-rules.git /tmp/semgrep-rules
uvx semgrep scan --metrics off --config /tmp/semgrep-rules/python --config /tmp/semgrep-rules/generic/secrets \
    --config /tmp/semgrep-rules/javascript --config /tmp/semgrep-rules/typescript \
    --config /tmp/semgrep-rules/yaml/github-actions src packaging clients/web-panel/src vscode/src .github
uv export --frozen --no-hashes --no-emit-project --all-groups > req.txt && uvx pip-audit -r req.txt --no-deps --disable-pip
(cd clients/web-panel && npm audit) ; (cd vscode && npm audit --omit=dev)
python packaging/osv_check.py
```

(`req.txt` may name one package twice with different versions, for different Python versions: split it in two.)

## Not covered here

- The IDE plugins didn't change (cmcoderagent's Phase 6 review).
- Code search (RAG) is Phase 5.
- The standalone build's own scan: no workflow runs one. `packaging/osv_check.py` (NuGet and
  OpenJFX) and the commands above are run by hand, as in the re-scan.
