// Accessibility of the chat panel, checked by axe-core in a real browser (dark and light theme values from
// theme.json), plus what axe cannot see: the keyboard, the landmarks, and what a screen reader is told.
// The page is the same in every IDE, so one connection (VS Code's) is enough here.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import type { Page } from "playwright";
import type { AgentEvent } from "../src/protocol";
import { ask, panel, result, root, startBrowser, stopBrowser, themed } from "./harness";

before(startBrowser);
after(stopBrowser);

const axeSource = readFileSync(join(root, "node_modules", "axe-core", "axe.min.js"), "utf8");
const PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

interface Violation {
  id: string;
  impact: string | null;
  help: string;
  nodes: { target: string[]; any?: { message: string }[] }[];
}

async function violations(page: Page): Promise<string[]> {
  await page.addScriptTag({ content: axeSource });
  const found = await page.evaluate(() => (window as unknown as { axe: { run: (c: Document, o: object) => Promise<{ violations: Violation[] }> } }).axe.run(document, { resultTypes: ["violations"] }));
  return found.violations.map((v) => `${v.id} (${v.impact}): ${v.help} — ${v.nodes.slice(0, 3).map((n) => `${n.target.join(" ")} [${n.any?.[0]?.message ?? ""}]`).join(" | ")}`);
}

const toolResult: Extract<AgentEvent, { type: "tool_result" }> = { type: "tool_result", id: "", name: "Bash", content: "a\nb", is_error: false, summary: "exit 0", parent_tool_use_id: null, exit_code: null, duration_ms: null, total_lines: null, stopped: false, images: [] };

const STATES: Record<string, (ev: (e: AgentEvent) => Promise<void>, page: Page) => Promise<void>> = {
  "an empty conversation with its starters": async (ev) => {
    await ev({ type: "command_list", commands: [{ name: "playwright", description: "", argument_hint: "", origin: "built-in" }, { name: "pick", description: "", argument_hint: "", origin: "built-in" }] });
  },
  "a reply with code, a list and a link, tool cards and a todo list": async (ev) => {
    await ev({ type: "assistant_message", text: "Here is **the fix**:\n\n- one\n- two\n\n```csharp\nvar x = 1;\n```\n\nSee [docs](https://example.com).", reasoning: "", tool_calls: [], model: null });
    await ev({ type: "tool_use", id: "1", name: "Bash", input: {}, label: "Bash(pytest -q)", parent_tool_use_id: null });
    await ev({ ...toolResult, id: "1" });
    await ev({ type: "tool_use", id: "s1", name: "Browser", input: {}, label: "Browser(screenshot)", parent_tool_use_id: null });
    await ev({ ...toolResult, id: "s1", name: "Browser", summary: "screenshot", images: [PNG] });
    await ev({ type: "todo_update", todos: [{ content: "Fix", status: "in_progress" }, { content: "Test", status: "pending" }] });
  },
  "code in every language the highlighter knows": async (ev) => {
    const fence = (lang: string, code: string) => `\`\`\`${lang}\n${code}\n\`\`\``;
    await ev({
      type: "assistant_message",
      reasoning: "",
      tool_calls: [],
      model: null,
      text: [
        fence("csharp", 'public async Task<IActionResult> Index(int page = 1) // list\n{ return View(@"x", 42, true, null); }'),
        fence("ts", 'export function f(a: string): Promise<void> { return g(`t`, 1.5, undefined); } // c'),
        fence("python", 'def f(self):\n    """doc"""  # c\n    return None'),
        fence("json", '{"a": "b", "n": 1, "t": true}'),
        fence("sql", "SELECT Name FROM Orders WHERE Id = 3 -- c"),
        fence("html", '<div class="x">@Model.Name<!-- c --></div>'),
        fence("diff", "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new"),
        fence("bash", 'dotnet test --filter "Name=x" $HOME # go'),
        fence("yaml", "name: x # c\nok: true"),
        fence("css", "a { max-width: 12px; color: #fff } /* c */"),
      ].join("\n\n"),
    });
  },
  "every kind of todo, and a stop that offers Continue": async (ev) => {
    await ev({ type: "todo_update", todos: [{ content: "Done thing", status: "completed" }, { content: "Doing thing", status: "in_progress" }, { content: "Next thing", status: "pending" }] });
    await ev({ type: "agent_changed", agent: "explore", reason: "" });
    await ev({ ...result, subtype: "max_turns", is_error: true, result: "Stopped after 50 model calls. Say continue, or raise the limit with /turns 100." });
  },
  "the changes bar and the review of every file": async (ev, page) => {
    const lines = (a: [string, string][]) => a.map(([kind, text]) => ({ kind: kind as "ctx" | "add" | "del", text }));
    const files = [
      { index: 0, path: "Controllers/OrdersController.cs", status: "modified" as const, added: 2, removed: 1, binary: false, truncated: false, reverted: false, kept: false, note: "changed since cmcoder wrote it, so it is left alone", hunks: [
        { id: 0, old_start: 24, new_start: 24, reverted: false, lines: lines([["ctx", "[HttpGet]"], ["del", "Index(int page = 1)"], ["add", "Index(OrderStatus? status, int page = 1)"]]) },
        { id: 1, old_start: 40, new_start: 40, reverted: true, lines: lines([["ctx", "return View(model);"], ["add", "ViewBag.Status = status;"]]) },
      ] },
      { index: 1, path: "Tests/OrdersControllerTests.cs", status: "added" as const, added: 34, removed: 0, binary: false, truncated: false, reverted: true, kept: false, note: "", hunks: [] },
      { index: 2, path: "Views/Orders/Index.cshtml", status: "modified" as const, added: 12, removed: 1, binary: false, truncated: false, reverted: false, kept: true, note: "", hunks: [
        { id: 0, old_start: 3, new_start: 3, reverted: false, lines: lines([["ctx", "<h1>Orders</h1>"], ["add", "<select id=\"status-filter\"></select>"]]) },
      ] },
    ];
    await ev({ type: "turn_changes", turn: 1, files, added: 36, removed: 1 });
    await page.click('.changes-bar button:has-text("Review changes")');
  },
  "picked elements waiting to be sent": async (ev) => {
    await ev({ type: "attachments", items: [{ index: 0, kind: "element", label: 'button[data-testid="save"] "Save" on http://localhost:5080/Orders' }, { index: 1, kind: "element", label: "h1 on /" }] });
  },
  "the agent's plan": async (ev) => {
    await ev(ask({ request_id: "plan1", tool_use_id: "7", name: "ExitPlanMode", label: "Plan: Orders status filter", input: { title: "Orders status filter", steps: [{ text: "Add a status parameter to Index", files: ["Controllers/OrdersController.cs"] }, { text: "Add tests", files: [] }], out_of_scope: "The database" }, suggested_rule: "", can_remember: false }));
  },
  "a permission question with a diff": async (ev) => {
    await ev(ask({ request_id: "r2", tool_use_id: "8", name: "Edit", label: "Edit(src/app.py)", input: {}, suggested_rule: "Edit(src/app.py)", change: { path: "/p/src/app.py", before: "x\na\ny\n", after: "x\nb\ny\n" } }));
  },
  "a failed command and a failed edit": async (ev) => {
    await ev({ type: "tool_use", id: "f1", name: "Bash", input: {}, label: "Bash(dotnet test)", parent_tool_use_id: null });
    await ev({ ...toolResult, id: "f1", is_error: true, content: "Failed: Index_FiltersByStatus", summary: "exit 1", exit_code: 1 });
    await ev({ type: "tool_use", id: "f2", name: "Edit", input: {}, label: "Edit(src/app.py)", parent_tool_use_id: null });
    await ev({ ...toolResult, id: "f2", name: "Edit", is_error: true, content: "old_string not found", summary: "error" });
  },
  "a check after an edit that failed, next to a command that passed": async (ev) => {
    await ev({ type: "tool_use", id: "c1", name: "Verify", input: { command: "pytest -q" }, label: "Verify(pytest -q)", parent_tool_use_id: null });
    await ev({ ...toolResult, id: "c1", name: "Verify", is_error: true, content: "F\nFAILED test_orders", summary: "exit 1", exit_code: 1, duration_ms: 1200, total_lines: 2 });
    await ev({ type: "tool_use", id: "c2", name: "Verify", input: { command: "ruff check ." }, label: "Verify(ruff check .)", parent_tool_use_id: null });
    await ev({ ...toolResult, id: "c2", name: "Verify", content: "All checks passed!", summary: "ok", exit_code: 0, duration_ms: 300, total_lines: 1 });
  },
  "background jobs: one running with output, one finished, one failed": async (ev) => {
    const job = { exit_code: null, elapsed_ms: 65000, tail: "", outside_sandbox: false, note: "" };
    await ev({
      type: "jobs_changed",
      jobs: [
        { ...job, id: 1, command: "npm run dev", state: "running", tail: "ready on http://localhost:3000" },
        { ...job, id: 2, command: "dotnet watch test", state: "exited", exit_code: 0, elapsed_ms: 3000 },
        { ...job, id: 3, command: "make serve", state: "failed", note: "the shell ended", outside_sandbox: true, tail: "Error: port in use" },
      ],
    });
  },
  "the settings page with a missing key and a failed connection test": async (ev, page) => {
    const base = { help: "Help text for the setting.", value: null, choices: [] as string[], locked: false, note: "" };
    await page.click(".settings-open");
    await ev({
      type: "settings_state",
      fields: [
        { ...base, key: "model", label: "Main model", group: "Models", kind: "text", value: "corp:qwen3-27b" },
        { ...base, key: "permissions.defaultMode", label: "Permission mode", group: "Behaviour", kind: "choice", value: "default", choices: ["default", "plan"] },
        { ...base, key: "maxTurns", label: "Model calls per request", group: "Behaviour", kind: "number", value: 50 },
        { ...base, key: "critic.enabled", label: "Critique every answer", group: "Behaviour", kind: "bool", value: true },
        { ...base, key: "verify.commands", label: "Check commands", group: "Checks", kind: "lines", value: ["npm test"] },
        { ...base, key: "audit.enabled", label: "Audit log", group: "Records", kind: "bool", value: true, locked: true, note: "set by your organisation" },
      ],
      gateways: [{ name: "corp", base_url: "https://gw.example/v1", key: "missing" }],
      file: "/home/u/.cmcoder/settings.json",
      notice: "Saved Main model: corp:qwen3-27b. In use now.",
      models: ["qwen3-27b"],
    });
    await ev({ type: "connection_tested", ok: false, gateway: "corp", message: "The gateway refused the key: run `cmcoder login` in a terminal.", elapsed_ms: 20 });
  },
  "a permission question": async (ev) => {
    await ev(ask({ request_id: "r1", tool_use_id: "9", name: "Bash", label: "Bash(npm test)", input: { command: "npm test" }, suggested_rule: "Bash(npm test:*)" }));
  },
  "an error card with all its buttons": async (ev) => {
    await ev({ type: "error", kind: "gateway", message: "Error code: 502 - {'error': {'message': 'Bad Gateway'}}\nsecond line", hint: "The gateway is down or overloaded: that is on its side. Retry in a moment, or switch to another model.", title: "The model gateway is not answering (502)", actions: ["retry", "switch_model", "compact", "new_conversation", "open_log"] });
  },
  "an error and a warning": async (ev) => {
    await ev({ type: "error", kind: "internal", message: "The gateway returned 502", hint: null, title: "", actions: [] });
    await ev({ type: "warning", message: "Context is 90% full" });
  },
  "the agent list": async (ev) => {
    await ev({ type: "agent_list", agents: [{ name: "explore", description: "Search", origin: "built-in", usable: true, hint: "", group: "" }], current: null });
  },
};

for (const mode of ["dark", "light"] as const) {
  for (const [name, setup] of Object.entries(STATES)) {
    test(`axe finds nothing in ${name}, ${mode} theme`, async () => {
      const { page, ev } = await panel("vscode", undefined, true, "stopTool sessionRules agents openLog");
      await themed(page, mode);
      await setup(ev, page);
      assert.deepEqual(await violations(page), []);
      await page.close();
    });
  }
}

test("one landmark, one heading, and the groups inside are not landmarks of their own", async () => {
  const { page } = await panel("vscode");
  assert.equal(await page.locator('[role="main"]').count(), 1);
  assert.equal(await page.locator("h1").count(), 1);
  assert.match((await page.textContent("h1")) ?? "", /chat/);
  assert.equal(await page.getAttribute(".log", "role"), "log");
  assert.equal(await page.getAttribute("select.mode", "aria-label"), "Permission mode");
  await page.close();
});

test("a picture that opens bigger can be used from the keyboard", async () => {
  const { page, ev } = await panel("vscode");
  await ev({ type: "tool_use", id: "s1", name: "Browser", input: {}, label: "Browser(screenshot)", parent_tool_use_id: null });
  await ev({ ...toolResult, id: "s1", name: "Browser", summary: "screenshot", images: [PNG] });
  await page.focus(".tool img.shot");
  assert.equal(await page.getAttribute(".tool img.shot", "role"), "button");
  assert.equal(await page.locator(".tool img.shot.big").count(), 0);
  await page.keyboard.press("Enter");
  assert.equal(await page.locator(".tool img.shot.big").count(), 1);
  await page.keyboard.press("Space");
  assert.equal(await page.locator(".tool img.shot.big").count(), 0);
  await page.close();
});

test("a screen reader is told when work starts, when a question needs an answer and when it stops", async () => {
  const { page, ev } = await panel("vscode");
  const said = async () => {
    await page.waitForTimeout(120); // the announcer sets its text a moment after clearing it
    return (await page.textContent(".announcer")) ?? "";
  };
  assert.equal(await page.getAttribute(".log", "aria-busy"), null, "nothing running yet");
  await page.fill("textarea", "go");
  await page.press("textarea", "Enter");
  assert.match(await said(), /is working/);
  assert.equal(await page.getAttribute(".log", "aria-busy"), "true", "a reader waits for the finished text");
  await ev(ask({ request_id: "r1", tool_use_id: "9", name: "Bash", label: "Bash(npm test)", input: { command: "npm test" }, suggested_rule: "Bash(npm test:*)" }));
  assert.match(await said(), /needs your answer: Bash\(npm test\)/);
  await ev({ ...result, subtype: "interrupted", is_error: false });
  assert.match(await said(), /Stopped/);
  assert.equal(await page.getAttribute(".log", "aria-busy"), "false");
  await ev({ type: "error", kind: "internal", message: "The gateway returned 502", hint: null, title: "", actions: [] });
  assert.match(await said(), /Error: The gateway returned 502/);
  await page.close();
});

test("the progress line is not read out second by second", async () => {
  const { page } = await panel("vscode");
  await page.fill("textarea", "go");
  await page.press("textarea", "Enter");
  assert.equal(await page.getAttribute(".progress", "aria-hidden"), "true");
  assert.equal(await page.getAttribute(".progress", "role"), null);
  await page.close();
});
