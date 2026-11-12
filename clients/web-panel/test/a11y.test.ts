// Accessibility of the chat panel, checked by axe-core in a real browser (dark and light theme values from
// theme.json), plus what axe cannot see: the keyboard, the landmarks, and what a screen reader is told.
// The page is the same in every IDE, so one connection (VS Code's) is enough here.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import type { Page } from "playwright";
import type { AgentEvent } from "../src/protocol";
import { ask, panel, result, root, startBrowser, stopBrowser } from "./harness";

before(startBrowser);
after(stopBrowser);

const theme = JSON.parse(readFileSync(join(root, "theme.json"), "utf8")).variables as Record<string, { dark: string; light: string }>;
const axeSource = readFileSync(join(root, "node_modules", "axe-core", "axe.min.js"), "utf8");
const PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

/** The IDE's colours: VS Code sets these itself; here they are the values theme.json lists. */
async function themed(page: Page, mode: "dark" | "light"): Promise<void> {
  const vars = Object.entries(theme).map(([name, v]) => `${name}:${v[mode]}`).join(";");
  await page.addStyleTag({ content: `:root{${vars}}body{background:var(--vscode-sideBar-background);color:var(--vscode-foreground)}` });
}

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

const STATES: Record<string, (ev: (e: AgentEvent) => Promise<void>) => Promise<void>> = {
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
  "every kind of todo, and a stop that offers Continue": async (ev) => {
    await ev({ type: "todo_update", todos: [{ content: "Done thing", status: "completed" }, { content: "Doing thing", status: "in_progress" }, { content: "Next thing", status: "pending" }] });
    await ev({ type: "agent_changed", agent: "explore", reason: "" });
    await ev({ ...result, subtype: "max_turns", is_error: true, result: "Stopped after 50 model calls. Say continue, or raise the limit with /turns 100." });
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
  "a permission question": async (ev) => {
    await ev(ask({ request_id: "r1", tool_use_id: "9", name: "Bash", label: "Bash(npm test)", input: { command: "npm test" }, suggested_rule: "Bash(npm test:*)" }));
  },
  "an error and a warning": async (ev) => {
    await ev({ type: "error", kind: "internal", message: "The gateway returned 502", hint: null });
    await ev({ type: "warning", message: "Context is 90% full" });
  },
  "the agent list": async (ev) => {
    await ev({ type: "agent_list", agents: [{ name: "explore", description: "Search", origin: "built-in", usable: true, hint: "", group: "" }], current: null });
  },
};

for (const mode of ["dark", "light"] as const) {
  for (const [name, setup] of Object.entries(STATES)) {
    test(`axe finds nothing in ${name}, ${mode} theme`, async () => {
      const { page, ev } = await panel("vscode", undefined, true, "stopTool sessionRules agents");
      await themed(page, mode);
      await setup(ev);
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
  await ev({ type: "error", kind: "internal", message: "The gateway returned 502", hint: null });
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
