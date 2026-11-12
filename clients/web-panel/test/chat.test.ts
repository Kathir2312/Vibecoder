// The chat panel (dist/chat.js) in a real browser, once per way an IDE connects
// (src/bridge.ts): events in, rendered DOM and messages to the IDE out.
// Needs `npm run build` first and a Chromium for Playwright
// (`npx playwright install chromium`, or PLAYWRIGHT_BROWSERS_PATH).

import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import type { AgentEvent } from "../src/protocol";
import type { Page } from "playwright";
import { FLAVOURS, panel, result, ask, startBrowser, stopBrowser, type Flavour } from "./harness";

before(startBrowser);
after(stopBrowser);

for (const flavour of FLAVOURS) {
  test(`${flavour}: a turn: prompt, streamed Markdown, tool card, todo list`, async () => {
    const { page, ev, sent } = await panel(flavour);
    await page.fill("textarea", "fix it");
    await page.press("textarea", "Enter");
    assert.deepEqual((await sent()).at(-1), { kind: "send", text: "fix it", includeContext: false });
    assert.equal(await page.isVisible(".stop"), true);

    await ev({ type: "todo_update", todos: [{ content: "Fix", status: "in_progress" }, { content: "Test", status: "pending" }] });
    await ev({ type: "assistant_delta", text: "Done **now**" });
    await ev({ type: "assistant_delta", text: " <img src=x onerror=alert(1)>" });
    await ev({ type: "assistant_message", text: "Done **now** <img src=x onerror=alert(1)>", reasoning: "", tool_calls: [{}], model: null });
    await ev({ type: "tool_use", id: "1", name: "Bash", input: {}, label: "Bash(pytest -q)", parent_tool_use_id: null });
    await ev({ type: "tool_result", id: "1", name: "Bash", content: "a\nb\nc\nd\ne", is_error: false, summary: "exit 0", parent_tool_use_id: null, exit_code: null, duration_ms: null, total_lines: null, stopped: false, images: [] });
    await ev(result);

    assert.equal(await page.textContent(".todos .dock-title"), "Tasks");
    assert.equal(await page.textContent(".todos .dock-summary"), "0/2► 1. Fix");
    assert.equal(await page.innerHTML(".msg.assistant strong"), "now");
    assert.equal(await page.locator(".msg.assistant img").count(), 0, "raw HTML from the model is not rendered");
    assert.equal(await page.textContent(".tool.bash .bash-cmd"), "pytest -q");
    assert.equal(await page.textContent(".tool.bash .bash-badge"), "✓ done");
    assert.equal(await page.textContent(".tool.bash .bash-out"), "a\nb\nc\nd\ne");
    assert.equal(await page.isVisible(".send"), true);
    await page.close();
  });

  test(`${flavour}: a browser screenshot is shown under the tool result, small until clicked`, async () => {
    const { page, ev } = await panel(flavour);
    const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
    await ev({ type: "tool_use", id: "s1", name: "Browser", input: {}, label: "Browser(screenshot)", parent_tool_use_id: null });
    await ev({
      type: "tool_result", id: "s1", name: "Browser", content: "Took a screenshot.", is_error: false,
      summary: "screenshot", parent_tool_use_id: null,
      exit_code: null, duration_ms: null, total_lines: null, stopped: false,
      images: [png, "javascript:alert(1)", "data:text/html;base64,PHNjcmlwdD4="],
    });
    assert.equal(await page.locator(".tool img.shot").count(), 1, "only image data URLs are shown");
    assert.equal(await page.getAttribute(".tool img.shot", "src"), png);
    assert.equal(await page.locator(".tool img.shot.big").count(), 0);
    await page.click(".tool img.shot");
    assert.equal(await page.locator(".tool img.shot.big").count(), 1);
    await page.click(".tool img.shot");
    assert.equal(await page.locator(".tool img.shot.big").count(), 0);
    await page.close();
  });

  test(`${flavour}: an edit request: headline, why, diff, a denial with a note, no duplicate note`, async () => {
    const { page, send, ev, sent } = await panel(flavour);
    await send({ kind: "context", label: "app.py:3-5" });
    await page.fill("textarea", "edit");
    await page.press("textarea", "Enter");
    assert.equal(((await sent()).at(-1) as { includeContext: boolean }).includeContext, true);
    await ev(ask({
      request_id: "r1", tool_use_id: "t1", name: "Edit", label: "Edit(app.py)",
      input: { file_path: "src/app.py", old_string: "a", new_string: "b" }, suggested_rule: "Edit(app.py)",
      reasons: ["edits ask first in default mode"],
      change: { path: "/p/src/app.py", before: "x\na\ny\n", after: "x\nb\ny\n" },
    }));
    assert.equal(await page.textContent(".ask-title"), "Edit app.py");
    assert.equal(await page.textContent(".ask-chips .chip"), "edits ask first in default mode");
    assert.equal(await page.textContent(".drow.del .txt"), "a");
    assert.equal(await page.textContent(".drow.add .txt"), "b");
    assert.equal(await page.textContent(".drow.del .no"), "2");
    assert.equal(await page.textContent(".diff-add"), "+1");
    assert.equal(await page.textContent(".diff-del"), "−1");
    assert.equal(await page.locator(".ask-deny input").isVisible(), false);
    await page.click("text=Open in diff editor");
    await page.click("text=Deny with a note…");
    await page.fill(".ask-deny input", "keep a");
    await page.click("text=Deny and send");
    await ev({ type: "permission_denied", id: "t1", name: "Edit", reason: "denied by user", parent_tool_use_id: null });
    const out = await sent();
    assert.deepEqual(out.slice(-2), [
      { kind: "showDiff", requestId: "r1" },
      { kind: "permission", requestId: "r1", allow: false, remember: false, feedback: "keep a" },
    ]);
    assert.equal(await page.locator(".note.warn").count(), 0);
    assert.match((await page.textContent(".permission")) ?? "", /└ Denied · “keep a”/);
    assert.equal(await page.locator(".permission button, .permission input").count(), 0);
    await page.close();
  });

  test(`${flavour}: a command request: the command with its folder, highlighted; Allow once`, async () => {
    const { page, ev, sent } = await panel(flavour);
    await ev(ask({
      request_id: "r3", tool_use_id: "t3", name: "Bash", label: "Bash(git commit -m 'x' && echo $HOME)",
      input: { command: "git commit -m 'x' && echo $HOME" }, suggested_rule: "Bash(git commit:*)",
      reasons: ["not on your allow list"], cwd: "/work/app",
    }));
    assert.equal(await page.textContent(".ask-title"), "Run a command");
    assert.equal(await page.textContent(".ask-chips .chip"), "not on your allow list");
    assert.equal(await page.textContent(".cmd-line .prompt"), "$");
    assert.equal(await page.textContent(".cmd-line .bash-cmd"), "git commit -m 'x' && echo $HOME");
    assert.equal(await page.textContent(".cmd-cwd"), "in /work/app");
    assert.equal(await page.textContent(".cmd-line .sh-flag"), "-m");
    assert.equal(await page.textContent(".cmd-line .sh-string"), "'x'");
    assert.equal(await page.textContent(".cmd-line .sh-var"), "$HOME");
    assert.equal(await page.locator(".permission.risk").count(), 0);
    assert.equal(await page.evaluate(() => document.activeElement === document.body || document.activeElement?.tagName === "TEXTAREA"), true, "no button takes the focus");
    await page.click("button:text-is('Allow')");
    assert.deepEqual((await sent()).at(-1), { kind: "permission", requestId: "r3", allow: true, remember: false });
    assert.match((await page.textContent(".permission")) ?? "", /└ Allowed$/);
    await page.close();
  });

  test(`${flavour}: a high-risk request is red, names the danger and can be allowed once only`, async () => {
    const { page, send, ev } = await panel(flavour);
    await ev(ask({
      request_id: "r2", tool_use_id: "t2", name: "Bash", label: "Bash(rm -rf build)",
      input: { command: "rm -rf build" }, suggested_rule: "Bash(rm:*)", reason: "high-risk command",
      reasons: ["high-risk: recursive delete"], can_remember: false, cwd: "/p", high_risk: true,
    }));
    assert.equal(await page.locator(".permission.risk").count(), 1);
    assert.equal(await page.textContent(".ask-icon"), "⚠");
    assert.equal(await page.locator("text=Always allow").count(), 0);
    assert.equal(await page.locator("button:text-is('Allow once')").count(), 1);
    assert.match((await page.textContent(".ask-note")) ?? "", /Asked every time/);
    assert.match((await page.textContent(".danger-parts")) ?? "", /Look at: rm -rf/);
    assert.equal((await page.locator(".cmd-line .sh-danger").allTextContents()).join(""), "rm -rf");
    await send({ kind: "permissionAnswered", requestId: "r2", text: "Allowed" });
    assert.match((await page.textContent(".permission")) ?? "", /└ Allowed/);
    assert.equal(await page.locator(".permission button").count(), 0);
    await page.close();
  });

  test(`${flavour}: Always allow shows the rule, then asks for the session or the project`, async () => {
    const { page, ev, sent } = await panel(flavour);
    const request = (id: string) => ask({
      request_id: id, tool_use_id: `u${id}`, name: "Bash", label: "Bash(npm test)",
      input: { command: "npm test" }, suggested_rule: "Bash(npm test:*)", reasons: ["not on your allow list"], cwd: "/p",
    });
    await ev(request("a1"));
    assert.equal(await page.locator(".ask-always").isVisible(), false);
    await page.click("text=Always allow…");
    assert.equal(await page.locator(".ask-actions").isVisible(), false);
    assert.match((await page.textContent(".ask-rule")) ?? "", /Allow from now on: Bash\(npm test:\*\)/);
    await page.click("text=For this session");
    assert.deepEqual((await sent()).at(-1), { kind: "permission", requestId: "a1", allow: true, remember: false, rememberSession: true });
    assert.match((await page.textContent(".permission")) ?? "", /└ Allowed for this session/);

    await ev(request("a2"));
    await page.locator(".permission").last().locator("text=Always allow…").click();
    await page.locator(".permission").last().locator("text=Cancel").click();
    assert.equal(await page.locator(".permission").last().locator(".ask-actions").isVisible(), true, "Cancel goes back");
    await page.locator(".permission").last().locator("text=Always allow…").click();
    await page.locator(".permission").last().locator("text=In this project").click();
    assert.deepEqual((await sent()).at(-1), { kind: "permission", requestId: "a2", allow: true, remember: true });
    assert.match((await page.locator(".permission").last().textContent()) ?? "", /└ Allowed · rule saved for this project/);
    await page.close();
  });

  test(`${flavour}: without the IDE's or cmcoder's support there is no session choice and no Stop`, async () => {
    for (const [host, engine] of [["", ["images", "stopTool", "sessionRules"]], ["stopTool sessionRules", ["images"]]] as const) {
      const { page, ev, send } = await panel(flavour, undefined, false, host);
      await send({ kind: "state", state: "ready" });
      await ev({ type: "system_init", protocol_version: 1, session_id: "s", cwd: "/p", model: "m", provider: "corp", tools: [], permission_mode: "default", critique: false, version: "0.1.0", features: [...engine] });
      await ev({ type: "tool_use", id: "b9", name: "Bash", input: { command: "sleep 5" }, label: "Bash(sleep 5)", parent_tool_use_id: null });
      await ev({ type: "tool_progress", id: "b9", name: "Bash", text: "x\n", elapsed_ms: 10, parent_tool_use_id: null });
      assert.equal(await page.locator(".stop-cmd").isVisible(), false, "no Stop this command");
      await ev(ask({ request_id: "g1", tool_use_id: "g1", name: "Edit", label: "Edit(a.py)", input: { file_path: "a.py", old_string: "a", new_string: "b" }, suggested_rule: "Edit(a.py)" }));
      await page.click("text=Always allow…");
      assert.equal(await page.locator("text=For this session").count(), 0);
      assert.equal(await page.locator("button:text-is('In this project')").count(), 1);
      await page.close();
    }
  });

  test(`${flavour}: a new file and an edit without the whole file show their lines`, async () => {
    const { page, ev } = await panel(flavour);
    await ev(ask({
      request_id: "n1", tool_use_id: "n1", name: "Write", label: "Write(new.py)", input: { file_path: "pkg/new.py", content: "a\nb\n" },
      suggested_rule: "Write(new.py)", change: { path: "/p/pkg/new.py", before: null, after: "a\nb\n" },
    }));
    assert.equal(await page.textContent(".ask-title"), "Create new.py");
    assert.equal(await page.textContent(".diff-new"), "new file");
    assert.equal(await page.textContent(".diff-add"), "+2");
    assert.equal(await page.locator(".diff-del").count(), 0, "no −0 for a new file");
    assert.equal(await page.locator(".drow.add").count(), 2);
    await ev(ask({
      request_id: "n2", tool_use_id: "n2", name: "Edit", label: "Edit(big.py)", input: { file_path: "big.py", old_string: "one\ntwo", new_string: "one\n2" },
      suggested_rule: "Edit(big.py)", change: null,
    }));
    const card = page.locator(".permission").last();
    assert.equal(await card.locator(".ask-diff.plain").count(), 1, "no line numbers for a snippet");
    assert.equal(await card.locator(".drow.del .txt").textContent(), "two");
    assert.equal(await card.locator(".drow.add .txt").textContent(), "2");
    await page.close();
  });

  test(`${flavour}: with several requests waiting each says which it is`, async () => {
    const { page, ev, send } = await panel(flavour);
    for (const id of ["q1", "q2"]) {
      await ev(ask({ request_id: id, tool_use_id: id, name: "Bash", label: "Bash(touch a)", input: { command: "touch a" }, suggested_rule: "Bash(touch:*)", cwd: "/p" }));
    }
    assert.deepEqual(await page.locator(".ask-count").allTextContents(), ["1 of 2 waiting", "2 of 2 waiting"]);
    await send({ kind: "permissionAnswered", requestId: "q1", text: "Allowed" });
    assert.deepEqual(await page.locator(".ask-count").allTextContents(), [""], "the answered one drops its count");
    await page.close();
  });

  test(`${flavour}: the Bash card: waits for the answer, shows output live, stops, ends as stopped`, async () => {
    const { page, send, ev, sent } = await panel(flavour);
    await ev({ type: "tool_use", id: "b1", name: "Bash", input: { command: "make build\nmake test" }, label: "Bash(make build)", parent_tool_use_id: null });
    assert.match((await page.textContent(".bash-badge")) ?? "", /^running · \d+s$/);
    assert.equal(await page.textContent(".bash-cmd"), "make build …", "the first line, with a mark for the rest");
    await ev(ask({ request_id: "w1", tool_use_id: "b1", name: "Bash", label: "Bash(make build)", input: { command: "make build\nmake test" }, suggested_rule: "Bash(make:*)", cwd: "/p" }));
    assert.equal(await page.textContent(".bash-badge"), "waiting for your answer");
    assert.equal(await page.locator(".stop-cmd").isVisible(), false, "nothing to stop before it runs");
    await page.click("button:text-is('Allow')");
    assert.match((await page.textContent(".bash-badge")) ?? "", /^running/);
    await ev({ type: "tool_progress", id: "b1", name: "Bash", text: "compiling\n", elapsed_ms: 1200, parent_tool_use_id: null });
    await ev({ type: "tool_progress", id: "b1", name: "Bash", text: "linking\n", elapsed_ms: 2400, parent_tool_use_id: null });
    await page.waitForFunction(() => document.querySelector(".bash-out")?.textContent === "compiling\nlinking");
    assert.equal(await page.locator(".stop-cmd").isVisible(), true);
    await page.click(".stop-cmd");
    assert.deepEqual((await sent()).at(-1), { kind: "stopTool", toolUseId: "b1" });
    assert.match((await page.textContent(".bash-badge")) ?? "", /^stopping… · \d+s$/);
    assert.equal(await page.locator(".stop-cmd").isVisible(), false, "once");
    await ev({
      type: "tool_result", id: "b1", name: "Bash", is_error: true, summary: "stopped", parent_tool_use_id: null, images: [],
      content: "(The user stopped this command before it finished. Its output so far follows.)\ncompiling\nlinking",
      exit_code: null, duration_ms: 2600, total_lines: 2, stopped: true,
    });
    assert.equal(await page.textContent(".bash-badge"), "■ stopped · 2.6 s");
    assert.equal(await page.textContent(".bash-out"), "compiling\nlinking");
    assert.equal(await page.locator(".tool.bash").getAttribute("data-state"), "stopped");
    await send({ kind: "reset" });
    await page.close();
  });

  test(`${flavour}: the Bash card: exit code and time, only the end of long output, Show all, Copy`, async () => {
    const { page, ev } = await panel(flavour);
    await ev({ type: "tool_use", id: "c1", name: "Bash", input: { command: "pytest" }, label: "Bash(pytest)", parent_tool_use_id: null });
    const lines = Array.from({ length: 20 }, (_, i) => `\u001b[32mline ${i + 1}\u001b[0m`);
    await ev({
      type: "tool_result", id: "c1", name: "Bash", is_error: true, summary: "exit 3", parent_tool_use_id: null, images: [],
      content: `${lines.join("\n")}\n\nExit code 3`, exit_code: 3, duration_ms: 1500, total_lines: 20, stopped: false,
    });
    assert.equal(await page.textContent(".bash-badge"), "✗ exit 3 · 1.5 s");
    assert.equal(await page.locator(".tool.bash").getAttribute("data-state"), "fail");
    const shown = ((await page.textContent(".bash-out")) ?? "").split("\n");
    assert.deepEqual([shown[0], shown[1], shown.at(-1)], ["… 12 earlier lines", "line 13", "line 20"], "the end, without colour codes or the exit-code trailer");
    assert.equal(await page.textContent(".bash-foot .more"), "Show all (20 lines)");
    await page.click(".bash-foot .more");
    assert.equal(((await page.textContent(".bash-out")) ?? "").split("\n").length, 20);
    assert.equal(await page.textContent(".bash-foot .more"), "Show less");
    await page.evaluate(() => {
      (window as unknown as { copied: string }).copied = "";
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async (t: string) => void ((window as unknown as { copied: string }).copied = t) } });
    });
    await page.click(".bash-foot .copy");
    await page.waitForFunction(() => document.querySelector(".bash-foot .copy")?.textContent === "Copied");
    const copied = await page.evaluate(() => (window as unknown as { copied: string }).copied);
    assert.equal(copied.split("\n").length, 20);
    assert.ok(!copied.includes("\u001b") && !copied.includes("Exit code"));
    await ev({ type: "tool_use", id: "c2", name: "Bash", input: { command: "true" }, label: "Bash(true)", parent_tool_use_id: null });
    await ev({ type: "tool_result", id: "c2", name: "Bash", is_error: false, summary: "exit 0", parent_tool_use_id: null, images: [], content: "(no output)", exit_code: 0, duration_ms: 40, total_lines: 0, stopped: false });
    const second = page.locator(".tool.bash").nth(1);
    assert.equal(await second.locator(".bash-badge").textContent(), "✓ exit 0 · 40 ms");
    assert.equal(await second.locator(".bash-out").textContent(), "(no output)");
    assert.equal(await second.locator(".bash-foot").isVisible(), false);
    await page.close();
  });

  test(`${flavour}: a command still running when the turn ends is shown as interrupted`, async () => {
    const { page, ev, send } = await panel(flavour);
    await page.fill("textarea", "go");
    await page.press("textarea", "Enter");
    await ev({ type: "tool_use", id: "d1", name: "Bash", input: { command: "sleep 60" }, label: "Bash(sleep 60)", parent_tool_use_id: null });
    await ev({ ...result, subtype: "interrupted" } as AgentEvent);
    assert.equal(await page.textContent(".bash-badge"), "■ interrupted");
    assert.equal(await page.locator(".stop-cmd").isVisible(), false);
    // cmcoder itself ending takes its commands with it.
    await ev({ type: "tool_use", id: "d2", name: "Bash", input: { command: "sleep 60" }, label: "Bash(sleep 60)", parent_tool_use_id: null });
    await send({ kind: "state", state: "exited", message: "cmcoder stopped." });
    assert.deepEqual(await page.locator(".bash-badge").allTextContents(), ["■ interrupted", "■ interrupted"]);
    await page.close();
  });

  test(`${flavour}: a long todo list is one line of the dock; the chat keeps its room`, async () => {
    const { page, ev } = await panel(flavour);
    const todos = Array.from({ length: 20 }, (_, i) => ({
      content: `Generate test: ${i + 1}.1 A rather long task title that would fill the line`,
      status: i === 0 ? "in_progress" : "pending",
    }));
    assert.equal(await page.isVisible(".dock"), false, "no dock until there is something to track");
    await ev({ type: "todo_update", todos });
    assert.equal(await page.textContent(".todo-count"), "0/20");
    assert.match((await page.textContent(".todo-now")) ?? "", /^► 1\. Generate test: 1\.1 /);
    const bar = (await page.locator(".todos").boundingBox())!;
    assert.ok(bar.height < 45, `the whole list is one line, not ${bar.height}px`);
    const log = (await page.locator(".log").boundingBox())!;
    assert.ok(log.height > 300, `the chat keeps its room (${log.height}px)`);
    assert.equal(await page.locator(".todo").count(), 0, "the rows are not drawn until asked for");
    // A click on the line opens the list under it, in a box of limited height.
    await page.click(".todos .dock-toggle");
    assert.equal(await page.getAttribute(".todos .dock-toggle", "aria-expanded"), "true");
    assert.equal(await page.locator(".todo").count(), 20);
    const list = (await page.locator(".todos .dock-body").boundingBox())!;
    assert.ok(list.height <= 720 * 0.33 + 1, `the list scrolls inside ${list.height}px`);
    assert.match((await page.locator(".todo").first().textContent()) ?? "", /^► 1\. Generate test/);
    await page.click(".todos .dock-toggle");
    assert.equal(await page.locator(".todo").count(), 0);
    assert.equal(await page.getAttribute(".todos .dock-toggle", "aria-expanded"), "false");
    assert.equal(await page.locator(".todo-open").count(), 0, "no button for a tab this IDE doesn't have");
    await page.close();
  });

  test(`${flavour}: the Tasks line counts what is done and ends with All tasks done`, async () => {
    const { page, ev } = await panel(flavour);
    const make = (done: number, total: number) =>
      Array.from({ length: total }, (_, i) => ({
        content: `Task ${i + 1}`,
        status: i < done ? "completed" : i === done ? "in_progress" : "pending",
      }));
    await ev({ type: "todo_update", todos: make(3, 20) });
    assert.equal(await page.textContent(".todo-count"), "3/20");
    assert.equal(await page.locator(".todo-fill").evaluate((e) => (e as HTMLElement).style.width), "15%");
    assert.match((await page.textContent(".todo-now")) ?? "", /^► 4\. Task 4$/);
    assert.match((await page.getAttribute(".todos .dock-toggle", "title")) ?? "", /3 done · 1 in progress · 16 to do/);
    await ev({ type: "todo_update", todos: make(20, 20).map((t) => ({ ...t, status: "completed" })) });
    assert.equal(await page.textContent(".todo-count"), "20/20");
    assert.equal(await page.textContent(".todo-now"), "✓ All tasks done");
    await ev({ type: "todo_update", todos: [] });
    assert.equal(await page.isVisible(".todos"), false);
    assert.equal(await page.isVisible(".dock"), false);
    await page.close();
  });

  test(`${flavour}: with a Tasks tab ↗ opens it, and the line still opens the list here`, async () => {
    const { page, ev, sent } = await panel(flavour, undefined, true, "stopTool sessionRules tasksTab");
    const todos = [
      { content: "Plan", status: "completed" },
      { content: "Build", status: "in_progress" },
      { content: "Test", status: "pending" },
    ];
    await ev({ type: "todo_update", todos });
    await page.click(".todos .todo-open");
    assert.deepEqual((await sent()).at(-1), { kind: "openTasks" });
    assert.equal(await page.locator(".todo").count(), 0, "the list stays closed");
    await page.click(".todos .dock-toggle");
    assert.equal(await page.locator(".todo").count(), 3);
    await ev({ type: "todo_update", todos: [...todos, { content: "Ship", status: "pending" }] });
    assert.equal(await page.locator(".todo").count(), 4, "an open list stays open as the agent works");
    await page.click(".todos .dock-toggle");
    assert.equal(await page.locator(".todo").count(), 0);
    await page.close();
  });

  // What the photo of a cramped Visual Studio panel showed: a docked tool window with a task list
  // open, subagents running and a long message being typed.
  const crowd = async (flavour: Flavour) => {
    const view = await panel(flavour, undefined, true, "stopTool sessionRules agents tasksTab");
    const { page, ev } = view;
    await page.setViewportSize({ width: 470, height: 620 });
    await page.fill("textarea", "go");
    await page.press("textarea", "Enter");
    await ev({
      type: "todo_update",
      todos: Array.from({ length: 22 }, (_, i) => ({ content: `Generate test ${i + 1} for the dashboard`, status: i < 7 ? "completed" : i === 7 ? "in_progress" : "pending" })),
    });
    for (let i = 1; i <= 30; i++) {
      await ev({ type: "tool_use", id: `r${i}`, name: "Read", input: {}, label: `Read(src/file${i}.cs)`, parent_tool_use_id: null });
      await ev({ type: "tool_result", id: `r${i}`, name: "Read", content: "", is_error: false, summary: "40 lines", parent_tool_use_id: null, exit_code: null, duration_ms: null, total_lines: null, stopped: false, images: [] });
    }
    for (let i = 1; i <= 5; i++) {
      await ev({ type: "tool_use", id: `a${i}`, name: "Task", input: {}, label: `Task(Review part ${i})`, parent_tool_use_id: null });
      await ev({
        type: "subagent_status", id: `a${i}`, number: i, description: `Review part ${i}`, agent_type: "explore", model: "m",
        state: "running", steps: i, max_steps: 100, tool_uses: i, tokens: 1000 * i, elapsed_ms: 1000 * i, activity: `Read(part${i}.cs)`,
      });
    }
    return view;
  };
  const height = (page: Page, sel: string) => page.locator(sel).evaluate((e) => e.getBoundingClientRect().height);
  const pageFits = (page: Page) => page.evaluate(() => document.documentElement.scrollHeight <= window.innerHeight);

  test(`${flavour}: open lists share a part of the window; the conversation keeps its share and the page never scrolls`, async () => {
    const { page } = await crowd(flavour);
    // The subagents opened by themselves; the tasks are one line until asked for.
    assert.equal(await page.getAttribute(".agents .dock-toggle", "aria-expanded"), "true");
    assert.equal(await page.getAttribute(".todos .dock-toggle", "aria-expanded"), "false");
    await page.click(".todos .dock-toggle");
    assert.equal(await page.locator(".todo").count(), 22);
    assert.ok((await height(page, ".dock")) <= 620 * 0.38 + 1, "the dock is at most its part of the window");
    assert.ok((await height(page, ".log")) >= 620 * 0.28 - 1, `the conversation keeps its share (${await height(page, ".log")}px)`);
    assert.equal(await pageFits(page), true, "one scrollbar per list, none for the page");
    const [tasks, agents] = [await height(page, ".todos .dock-body"), await height(page, ".agents .dock-body")];
    assert.ok(tasks > 40 && agents > 40, `both lists show rows (${tasks}px, ${agents}px)`);
    assert.ok(Math.abs(tasks - agents) < 30, `two long lists get equal parts (${tasks}px, ${agents}px)`);
    assert.equal(await page.isVisible(".todo.in_progress"), true, "the task in progress is in view");
    // A long message being typed: the input grows, the dock gives way, the conversation doesn't.
    await page.fill("textarea", Array.from({ length: 14 }, (_, i) => `line ${i + 1}`).join("\n"));
    assert.ok((await height(page, "textarea")) <= 620 * 0.26 + 1, "the input stops growing at its part of the window");
    assert.ok((await height(page, ".log")) >= 620 * 0.28 - 1);
    assert.equal(await pageFits(page), true);
    // A low window: still no page scrollbar, both lines and the input stay.
    await page.fill("textarea", "");
    await page.setViewportSize({ width: 470, height: 420 });
    assert.equal(await pageFits(page), true);
    assert.equal(await page.isVisible(".todos .dock-toggle"), true);
    assert.equal(await page.isVisible(".agents .dock-toggle"), true);
    assert.equal(await page.isVisible("footer .stop"), true);
    await page.close();
  });

  test(`${flavour}: scrolling the conversation down folds the dock; output that scrolls by itself doesn't`, async () => {
    const { page, ev } = await crowd(flavour);
    await page.click(".todos .dock-toggle");
    const open = () => page.locator(".dock-panel.open").count();
    assert.equal(await open(), 2);
    const before = await height(page, ".log");
    // New output moves the view by itself: the lists the user opened stay.
    await ev({ type: "assistant_delta", text: "Working on it.\n\n".repeat(20) });
    await page.waitForTimeout(80);
    assert.equal(await open(), 2);
    // The wheel, down, over the conversation: every panel back to its line.
    const box = (await page.locator(".log").boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.wheel(0, -200); // up first: reading back doesn't fold anything
    await page.waitForTimeout(50);
    assert.equal(await open(), 2);
    await page.mouse.wheel(0, 120);
    await page.waitForFunction(() => document.querySelectorAll(".dock-panel.open").length === 0);
    assert.ok((await height(page, ".dock")) < 80, `two lines (${await height(page, ".dock")}px)`);
    assert.ok((await height(page, ".log")) > before + 100, "the conversation got the room");
    assert.match((await page.textContent(".agents .dock-now")) ?? "", /^◐ \d\. Review part \d · Read\(part\d\.cs\)$/);
    // A click opens one again; the keys fold it too (not while typing).
    await page.click(".agents .dock-toggle");
    assert.equal(await open(), 1);
    await page.focus("textarea");
    await page.keyboard.press("PageDown");
    assert.equal(await open(), 1, "PageDown in the input is typing");
    await page.locator(".log").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press("PageDown");
    assert.equal(await open(), 0);
    await page.close();
  });

  test(`${flavour}: a panel the user folded stays folded; a new conversation forgets that`, async () => {
    const { page, ev, send } = await panel(flavour);
    await page.fill("textarea", "go");
    await page.press("textarea", "Enter");
    const start = (id: string, number: number) =>
      ev({ type: "subagent_status", id, number, description: `Part ${number}`, agent_type: "explore", model: "m", state: "running", steps: 1, max_steps: 100, tool_uses: 1, tokens: 10, elapsed_ms: 10, activity: "" });
    await start("a", 1);
    assert.equal(await page.getAttribute(".agents .dock-toggle", "aria-expanded"), "true");
    await page.click(".agents .dock-toggle"); // the user folds it
    await ev(result);
    await page.fill("textarea", "again");
    await page.press("textarea", "Enter");
    await start("b", 1);
    assert.equal(await page.isVisible(".agents"), true);
    assert.equal(await page.getAttribute(".agents .dock-toggle", "aria-expanded"), "false", "it doesn't open by itself again");
    await send({ kind: "reset" });
    await page.fill("textarea", "new");
    await page.press("textarea", "Enter");
    await start("c", 1);
    assert.equal(await page.getAttribute(".agents .dock-toggle", "aria-expanded"), "true");
    await page.close();
  });

  test(`${flavour}: scrolling up stops following the output; ↓ Latest follows it again`, async () => {
    const { page, ev } = await panel(flavour);
    await page.setViewportSize({ width: 470, height: 620 });
    await page.fill("textarea", "go");
    await page.press("textarea", "Enter");
    await ev({ type: "assistant_delta", text: Array.from({ length: 80 }, (_, i) => `Paragraph ${i + 1}.`).join("\n\n") });
    const atEnd = () => page.locator(".log").evaluate((l) => l.scrollHeight - l.scrollTop - l.clientHeight < 40);
    await page.waitForFunction(() => { const l = document.querySelector(".log")!; return l.scrollHeight - l.scrollTop - l.clientHeight < 40; });
    assert.equal(await page.isVisible(".to-end"), false);
    const box = (await page.locator(".log").boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    // Even a small step up, with output still coming in, isn't pulled back to the end.
    await page.mouse.wheel(0, -30);
    await ev({ type: "assistant_delta", text: "\n\nStill writing." });
    await page.waitForTimeout(80);
    assert.equal(await atEnd(), false, "a small step up holds while output arrives");
    await page.mouse.wheel(0, -600);
    await page.waitForFunction(() => !(document.querySelector(".to-end") as HTMLElement).hidden);
    // A wheel's scroll is animated: wait until the view stands still.
    const scrollTop = () => page.locator(".log").evaluate((l) => l.scrollTop);
    let top = await scrollTop();
    for (let last = -1; last !== top; top = await scrollTop()) {
      last = top;
      await page.waitForTimeout(120);
    }
    await ev({ type: "assistant_delta", text: "\n\nMore output while reading back." });
    await page.waitForTimeout(80);
    assert.equal(await scrollTop(), top, "the view stays where the user reads");
    await page.click(".to-end");
    assert.equal(await atEnd(), true);
    assert.equal(await page.isVisible(".to-end"), false);
    await ev({ type: "assistant_delta", text: "\n\nAnd more." });
    await page.waitForTimeout(80);
    assert.equal(await atEnd(), true, "following again");
    await page.close();
  });

  test(`${flavour}: the input is as tall as its text and goes back after sending`, async () => {
    const { page } = await panel(flavour);
    await page.setViewportSize({ width: 470, height: 620 });
    const empty = await height(page, "textarea");
    assert.ok(empty < 60, `two lines when empty (${empty}px)`);
    assert.equal(await page.locator("textarea").evaluate((t) => getComputedStyle(t).resize), "none");
    await page.fill("textarea", "one\ntwo\nthree\nfour\nfive");
    const five = await height(page, "textarea");
    assert.ok(five > empty + 30, `it grew with the text (${five}px)`);
    await page.fill("textarea", Array.from({ length: 40 }, (_, i) => `line ${i}`).join("\n"));
    assert.ok((await height(page, "textarea")) <= 620 * 0.26 + 1, "up to a part of the window, then it scrolls");
    await page.press("textarea", "Enter");
    assert.equal(await height(page, "textarea"), empty);
    // Measured while the panel is hidden (an IDE resizes a hidden view), it keeps its two lines.
    await page.evaluate(() => {
      const app = document.getElementById("app")!;
      app.style.display = "none";
      window.dispatchEvent(new Event("resize"));
      app.style.display = "";
    });
    assert.ok(Math.abs((await height(page, "textarea")) - empty) < 2, "still two lines, not none");
    await page.close();
  });

  test(`${flavour}: a request that used all its model calls says so, and Continue goes on`, async () => {
    const { page, ev, sent } = await panel(flavour);
    await page.fill("textarea", "refactor everything");
    await page.press("textarea", "Enter");
    await ev({ ...result, subtype: "max_turns", is_error: true, result: 'Stopped after 50 model calls, the limit for one request (maxTurns). The work so far is kept: say "continue" to go on with a fresh budget, or raise the limit now with /turns 100 (maxTurns in your settings changes it for good).' });
    const note = (await page.textContent(".stop-note")) ?? "";
    assert.match(note, /Stopped after 50 model calls/);
    assert.match(note, /\/turns 100/);
    assert.equal(await page.isVisible("footer .send"), true, "the turn is over");
    await page.click(".stop-note .continue");
    assert.deepEqual((await sent()).at(-1), { kind: "send", text: "continue", includeContext: false });
    assert.equal(await page.locator(".stop-note .continue").count(), 0, "one click, one message");
    assert.equal(await page.isVisible("footer .stop"), true, "it is working again");
    await page.close();
  });

  test(`${flavour}: Pick element sends /pick, only when this cmcoder has it, and not during a request`, async () => {
    const { page, ev, sent } = await panel(flavour);
    const command = (name: string) => ({ name, description: "", argument_hint: "", origin: "built-in" as const });
    assert.equal(await page.isVisible(".pick"), false, "nothing to pick with before the commands are listed");
    await ev({ type: "command_list", commands: [command("help"), command("model")] });
    assert.equal(await page.isVisible(".pick"), false, "an older cmcoder without /pick: no button");
    await ev({ type: "command_list", commands: [command("help"), command("pick")] });
    assert.equal(await page.isVisible(".pick"), true);
    assert.equal(await page.textContent(".pick"), "Pick element");
    await page.fill("textarea", "half a message");
    await page.click(".pick");
    assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/pick", includeContext: false });
    assert.equal(await page.inputValue("textarea"), "half a message", "what was being typed stays");
    assert.match((await page.locator(".note.info").last().textContent()) ?? "", /Pick mode: click an element on the page in the browser/);
    assert.equal(await page.locator(".pick").isDisabled(), true, "picking is a request: it is busy until it ends");
    await ev(result);
    assert.equal(await page.locator(".pick").isDisabled(), false);
    await page.close();
  });

  test(`${flavour}: an empty conversation offers ready-made commands once cmcoder lists them`, async () => {
    const { page, ev, send, sent } = await panel(flavour);
    const command = (name: string) => ({ name, description: "", argument_hint: "", origin: "built-in" as const });
    const asked = () => sent().then((m) => m.filter((x) => x.kind === "listCommands").length);
    assert.equal(await asked(), 1, "the chat asks for the commands once, when cmcoder says hello");
    assert.equal(await page.isVisible(".starters"), false, "nothing to offer before they are listed");
    await ev({ type: "command_list", commands: [command("help"), command("model")] });
    assert.equal(await page.isVisible(".starters"), false, "an older cmcoder without the command: no chips");
    await ev({ type: "command_list", commands: [command("help"), command("playwright")] });
    assert.deepEqual(await page.locator(".starters .starter").allTextContents(), ["What's next?", "Set up", "Plan the tests", "Do it all"]);
    assert.match((await page.textContent(".starters-heading")) ?? "", /Playwright/);
    // Most chips put the command in the input for the user to finish; they send nothing.
    await page.click(".starter >> text=Plan the tests");
    assert.equal(await page.inputValue("textarea"), "/playwright plan");
    await page.click(".starter >> text=Set up");
    assert.equal(await page.inputValue("textarea"), "/playwright setup http://localhost:");
    assert.equal((await sent()).filter((x) => x.kind === "send").length, 0);
    await page.fill("textarea", "");
    // "What's next?" needs nothing from the user: it is sent, and the starters make room.
    await page.click(".starter >> text=What's next?");
    assert.deepEqual((await sent()).filter((x) => x.kind === "send"), [{ kind: "send", text: "/playwright", includeContext: false }]);
    assert.equal(await page.isVisible(".starters"), false);
    await ev(result);
    assert.equal(await page.isVisible(".starters"), false, "the conversation isn't empty any more");
    // A new conversation is empty again.
    await send({ kind: "reset" });
    assert.equal(await page.isVisible(".starters"), true);
    assert.equal(await page.inputValue("textarea"), "");
    await page.close();
  });

  const AGENTS = [
    { name: "planner", description: "Plans tests for a web app.", origin: "project" as const, usable: true, hint: "", group: "" },
    { name: "healer", description: "", origin: "project" as const, usable: false, hint: "This project's agents are used only once the project is trusted: run `cmcoder trust`.", group: "" },
    { name: "explore", description: "Read-only search.", origin: "built-in" as const, usable: true, hint: "", group: "" },
  ];
  const HOST = "stopTool sessionRules agents";

  test(`${flavour}: the agent dropdown needs the IDE and cmcoder to support it`, async () => {
    // The IDE says nothing: no dropdown, even though cmcoder lists agents.
    const plain = await panel(flavour);
    await plain.ev({ type: "agent_list", agents: AGENTS, current: null });
    assert.equal(await plain.page.isVisible(".agent"), false);
    await plain.page.close();
    // The IDE supports it but this cmcoder is older: nothing to show either.
    const old = await panel(flavour, undefined, false, HOST);
    await old.send({ kind: "state", state: "ready" });
    await old.ev({ type: "system_init", protocol_version: 1, session_id: "s", cwd: "/p", model: "m", provider: "corp", tools: [], permission_mode: "default", critique: false, version: "0.1.0", features: ["images"] });
    await old.ev({ type: "agent_list", agents: AGENTS, current: null });
    assert.equal(await old.page.isVisible(".agent"), false);
    await old.page.close();
  });

  test(`${flavour}: the agent dropdown lists them by group, greys out untrusted ones and sends the choice`, async () => {
    const { page, ev, sent } = await panel(flavour, undefined, true, HOST);
    assert.equal(await page.isVisible(".agent"), false, "nothing to choose before the list arrives");
    await ev({ type: "agent_list", agents: AGENTS, current: null });
    assert.equal(await page.isVisible(".agent"), true);
    assert.deepEqual(await page.locator(".agent optgroup").evaluateAll((g) => g.map((x) => (x as HTMLOptGroupElement).label)), ["This project", "Built in"]);
    assert.deepEqual(await page.locator(".agent option").allTextContents(), ["cmcoder (default)", "planner", "healer (trust this project)", "explore"]);
    assert.equal(await page.locator('.agent option[value="healer"]').isDisabled(), true);
    assert.match((await page.locator('.agent option[value="healer"]').getAttribute("title")) ?? "", /cmcoder trust/);
    assert.equal(await page.locator('.agent option[value="planner"]').getAttribute("title"), "Plans tests for a web app.");
    await page.selectOption(".agent", "planner");
    assert.deepEqual((await sent()).at(-1), { kind: "setAgent", agent: "planner" });
    await ev({ type: "agent_changed", agent: "planner", reason: "" });
    await ev({ type: "agent_list", agents: AGENTS, current: "planner" });
    assert.equal(await page.locator(".agent").inputValue(), "planner");
    assert.match((await page.textContent(".note.info")) ?? "", /Now talking to the planner agent/);
    assert.equal(await page.getAttribute("textarea", "placeholder"), "Ask planner… (Enter to send, Shift+Enter for a new line)");
    assert.equal(await page.getAttribute(".agent", "title"), "Plans tests for a web app.");
    await page.selectOption(".agent", "");
    assert.deepEqual((await sent()).at(-1), { kind: "setAgent", agent: null });
    await ev({ type: "agent_changed", agent: null, reason: "" });
    assert.equal(await page.locator(".agent").inputValue(), "");
    assert.match((await page.locator(".note.info").last().textContent()) ?? "", /Back to cmcoder's default agent/);
    assert.match((await page.getAttribute("textarea", "placeholder")) ?? "", /^Ask cmcoder…/);
    await page.close();
  });

  test(`${flavour}: the Playwright agents get their own heading and a step moves the dropdown there and back`, async () => {
    const { page, ev } = await panel(flavour, undefined, true, HOST);
    const roles = [
      { name: "playwright-planner", description: "Plans the tests.", origin: "built-in" as const, usable: true, hint: "", group: "Playwright" },
      { name: "playwright-healer", description: "Fixes tests.", origin: "built-in" as const, usable: true, hint: "", group: "Playwright" },
    ];
    const all = [...AGENTS, ...roles];
    await ev({ type: "agent_list", agents: all, current: "explore" });
    assert.deepEqual(await page.locator(".agent optgroup").evaluateAll((g) => g.map((x) => (x as HTMLOptGroupElement).label)), ["This project", "Built in", "Playwright"]);
    assert.deepEqual(await page.locator('.agent optgroup[label="Playwright"] option').allTextContents(), ["playwright-planner", "playwright-healer"]);
    assert.equal(await page.locator(".agent").inputValue(), "explore");
    const notes = () => page.locator(".note.info").allTextContents();
    const before = (await notes()).length;
    // `/playwright plan` runs as the planner: the list shows it, with a note that says why.
    await ev({ type: "agent_changed", agent: "playwright-planner", reason: "step" });
    await ev({ type: "agent_list", agents: all, current: "playwright-planner" });
    assert.equal(await page.locator(".agent").inputValue(), "playwright-planner");
    assert.match((await notes()).at(-1) ?? "", /This step runs as the playwright-planner agent/);
    // Afterwards the agent from before comes back, and that says nothing: it was not the user's doing.
    await ev({ type: "agent_changed", agent: "explore", reason: "restore" });
    await ev({ type: "agent_list", agents: all, current: "explore" });
    assert.equal(await page.locator(".agent").inputValue(), "explore");
    assert.equal((await notes()).length, before + 1, "only the step announced itself");
    assert.match((await page.getAttribute("textarea", "placeholder")) ?? "", /^Ask explore…/);
    await page.close();
  });

  test(`${flavour}: the agent can't be changed during a turn, and a refused choice snaps back`, async () => {
    const { page, ev } = await panel(flavour, undefined, true, HOST);
    await ev({ type: "agent_list", agents: AGENTS, current: "planner" });
    await page.fill("textarea", "go");
    await page.press("textarea", "Enter");
    assert.equal(await page.locator(".agent").isDisabled(), true);
    await ev(result);
    assert.equal(await page.locator(".agent").isDisabled(), false);
    // The user picks one, cmcoder refuses (an error event): the list shows what is really in use.
    await page.evaluate(() => ((document.querySelector(".agent") as HTMLSelectElement).value = "explore"));
    await ev({ type: "error", kind: "invalid_agent", message: "No agent named 'explore'.", hint: null });
    assert.equal(await page.locator(".agent").inputValue(), "planner");
    await page.close();
  });

  test(`${flavour}: a new conversation starts with the default agent and no list`, async () => {
    const { page, send, ev } = await panel(flavour, undefined, true, HOST);
    await ev({ type: "agent_list", agents: AGENTS, current: "planner" });
    await send({ kind: "reset" });
    assert.equal(await page.isVisible(".agent"), false);
    assert.match((await page.getAttribute("textarea", "placeholder")) ?? "", /^Ask cmcoder…/);
    await ev({ type: "agent_list", agents: AGENTS, current: null });
    assert.equal(await page.locator(".agent").inputValue(), "");
    await page.close();
  });

  test(`${flavour}: history, past conversations and resume`, async () => {
    const { page, ev, sent } = await panel(flavour);
    await ev({ type: "history", messages: [{ role: "user", text: "hi" }, { role: "tool", text: "Read(a.py)" }, { role: "assistant", text: "**hello**" }] });
    assert.equal(await page.textContent(".msg.user"), "hi");
    assert.equal(await page.innerHTML(".msg.assistant strong"), "hello");
    await page.click("text=History");
    assert.deepEqual((await sent()).at(-1), { kind: "listSessions" });
    await ev({ type: "session_list", sessions: [{ id: "abc", title: "Earlier work", updated: 1790000000, messages: 3 }] });
    await page.click("text=Earlier work");
    assert.deepEqual((await sent()).at(-1), { kind: "resume", id: "abc" });
    await page.close();
  });

  test(`${flavour}: model output can't inject markup: allowlist after escaping`, async () => {
    const { page, ev } = await panel(flavour);
    const evil = [
      "[click](javascript:alert(1)) [ok](https://example.com)",
      "<script>window.pwned=1</script><iframe src=x></iframe>",
      "![img](https://tracker.example/p.png)",
      "- [x] done",
      "| a | b |\n|:-|-:|\n| 1 | 2 |",
    ].join("\n\n");
    await ev({ type: "assistant_message", text: evil, reasoning: "", tool_calls: [], model: null });
    const html = await page.innerHTML(".msg.assistant");
    assert.doesNotMatch(html, /<(script|iframe|img)\b/i);
    assert.doesNotMatch(html, /javascript:/i);
    assert.match(html, /<a href="https:\/\/example\.com">ok<\/a>/);
    assert.match(html, /<input[^>]*type="checkbox"/);
    assert.match(html, /<td[^>]*align="right"[^>]*>2<\/td>/);
    assert.equal(await page.evaluate(() => (window as unknown as { pwned?: number }).pwned), undefined);
    await page.close();
  });

  test(`${flavour}: slash-command completion: list, filter, Tab to complete, then send`, async () => {
    const { page, ev, sent } = await panel(flavour);
    await page.type("textarea", "/");
    assert.deepEqual((await sent()).at(-1), { kind: "listCommands" });
    await ev({
      type: "command_list",
      commands: [
        { name: "compact", description: "Summarise", argument_hint: "[focus]", origin: "built-in" },
        { name: "review", description: "Review a <b>file</b>", argument_hint: "<file>", origin: "project" },
        { name: "release:notes", description: "Notes", argument_hint: "", origin: "user" },
      ],
    });
    assert.equal(await page.locator(".commands .command").count(), 3);
    await page.type("textarea", "re");
    assert.equal(await page.locator(".commands .command").count(), 2);
    assert.equal(await page.textContent(".commands .selected"), "/review <file>Review a <b>file</b> (project)");
    await page.press("textarea", "ArrowDown");
    await page.press("textarea", "Tab");
    assert.equal(await page.inputValue("textarea"), "/release:notes ");
    assert.equal(await page.isHidden(".commands"), true);
    await page.type("textarea", "v2");
    await page.press("textarea", "Enter");
    assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/release:notes v2", includeContext: false });
    await page.close();
  });

  test(`${flavour}: a subagent's steps are listed inside its Task card`, async () => {
    const { page, ev } = await panel(flavour);
    await page.fill("textarea", "find it");
    await page.press("textarea", "Enter");
    await ev({ type: "tool_use", id: "t", name: "Task", input: {}, label: "Task(explore: find the parser)", parent_tool_use_id: null });
    await ev({ type: "tool_use", id: "c1", name: "Grep", input: {}, label: "Grep(parse)", parent_tool_use_id: "t" });
    await ev({ type: "tool_result", id: "c1", name: "Grep", content: "3 files", is_error: false, summary: null, parent_tool_use_id: "t", exit_code: null, duration_ms: null, total_lines: null, stopped: false, images: [] });
    await ev({ type: "tool_use", id: "c2", name: "Read", input: {}, label: "Read(x.py)", parent_tool_use_id: "t" });
    await ev({ type: "tool_result", id: "c2", name: "Read", content: "No such file\nmore", is_error: true, summary: null, parent_tool_use_id: "t", exit_code: null, duration_ms: null, total_lines: null, stopped: false, images: [] });
    await ev({ type: "tool_result", id: "t", name: "Task", content: "Found it.", is_error: false, summary: "2 tool uses", parent_tool_use_id: null, exit_code: null, duration_ms: null, total_lines: null, stopped: false, images: [] });
    await ev(result);
    assert.equal(await page.locator(".tool").count(), 1, "one card: the subagent's steps are inside it");
    assert.deepEqual(await page.locator(".tool .steps .step").allTextContents(), ["● Grep(parse)", "● Read(x.py) — No such file"]);
    assert.match((await page.textContent(".tool")) ?? "", /└ 2 tool uses/);
    assert.equal(await page.textContent(".tool details.steps summary"), "2 subagent steps");
    assert.equal(await page.isVisible(".tool .steps .step"), false, "folded once the Task is done");
    await page.close();
  });

  test(`${flavour}: rewind: the conversation is redrawn and the message is back in the input`, async () => {
    const { page, ev } = await panel(flavour);
    await ev({ type: "history", messages: [{ role: "user", text: "first" }, { role: "user", text: "second" }] });
    assert.equal(await page.locator(".msg.user").count(), 2);
    await ev({ type: "rewind_points", points: [{ turn: 2, text: "second", files_changed: 1, outside_files: [] }] });
    assert.match((await page.textContent(".log")) ?? "", /list at the top of the window/);
    await ev({
      type: "rewound",
      code: true,
      conversation: true,
      actions: [{ path: "/p/new.txt", action: "deleted (created after that point)" }],
      prompt: "second",
    });
    await ev({ type: "history", messages: [{ role: "user", text: "first" }] });
    assert.deepEqual(await page.locator(".msg.user").allTextContents(), ["first"]);
    assert.equal(await page.inputValue("textarea"), "second");
    assert.match((await page.textContent(".log")) ?? "", /Rewound the conversation\. Files: 0 restored, 1 deleted/);
    await page.close();
  });

  test(`${flavour}: branding: the product's name and logo before the first message`, async () => {
    const { page, ev } = await panel(flavour, "Acme Coder");
    assert.equal(await page.getAttribute("textarea", "placeholder"), "Ask Acme Coder… (Enter to send, Shift+Enter for a new line)");
    const empty = () =>
      page.$eval(".log", (log) => ({
        name: getComputedStyle(log, "::after").content,
        logo: getComputedStyle(log, "::before").backgroundImage,
      }));
    assert.deepEqual(await empty(), { name: '"Acme Coder"', logo: (await empty()).logo });
    assert.match((await empty()).logo, /icon\.png/);
    await ev({ type: "assistant_message", text: "Hello.", reasoning: "", tool_calls: [], model: "m" });
    assert.equal((await empty()).name, "none"); // gone once the conversation starts
    const avatar = await page.$eval(".msg.assistant", (m) => getComputedStyle(m, "::before").backgroundImage);
    assert.match(avatar, /icon\.png/);
    await page.close();
  });

  test(`${flavour}: branding: defaults to cmcoder`, async () => {
    const { page } = await panel(flavour);
    assert.match((await page.getAttribute("textarea", "placeholder")) ?? "", /^Ask cmcoder…/);
    await page.close();
  });

  test(`${flavour}: code search: the code added from the index is noted in the panel`, async () => {
    const { page, ev } = await panel(flavour);
    await ev({
      type: "code_context",
      items: [
        { path: "auth.py", start_line: 4, end_line: 8, symbol: "refresh_auth_token", score: 0.82 },
        { path: "notes.md", start_line: 1, end_line: 3, symbol: null, score: 0.5 },
      ],
      tokens: 310,
    });
    const text = await page.textContent(".log");
    assert.match(text ?? "", /Added code from the index: auth\.py:4-8, notes\.md:1-3 \(≈310 tokens\)/);
    await page.close();
  });

  test(`${flavour}: the Subagents panel: live rows, Stop sends stop_subagent, cards show the status`, async () => {
    const { page, ev, sent } = await panel(flavour);
    await page.fill("textarea", "one subagent per project");
    await page.press("textarea", "Enter");
    const status = (id: string, number: number, description: string, state: string, extra = {}) =>
      ev({
        type: "subagent_status", id, number, description, agent_type: "explore", model: "qwen-small",
        state: state as never, steps: 3, max_steps: 100, tool_uses: 4, tokens: 31400, elapsed_ms: 72000,
        activity: "", ...extra,
      });
    for (const [id, n, d] of [["a", 1, "Analyse TW.Core"], ["b", 2, "Analyse TW.Api"]] as const) {
      await ev({ type: "tool_use", id, name: "Task", input: {}, label: `Task(explore: ${d})`, parent_tool_use_id: null });
      await status(id, n, d, "running", { activity: "Read(Program.cs)" });
    }
    await status("c", 3, "Analyse TW.Web", "queued");
    assert.equal(await page.isVisible(".agents"), true);
    const rows = await page.locator(".agents .agent .name").allTextContents();
    assert.deepEqual(rows, ["◐ 1. Analyse TW.Core", "◐ 2. Analyse TW.Api", "○ 3. Analyse TW.Web"]);
    assert.equal(await page.getAttribute(".agents .dock-toggle", "aria-expanded"), "true", "a turn's first subagent opens the list");
    assert.match((await page.textContent(".agents .agent.running .detail")) ?? "", /explore · 3\/100 steps · 4 tools · 31\.4k tokens · 1m1[23]s/);
    assert.equal(await page.locator(".agents .activity").count(), 2);
    assert.equal(await page.textContent(".agents .dock-title"), "Subagents");
    assert.equal(await page.textContent(".agents .dock-count"), "3 active");
    // The line says what the subagent heard from last is doing (shown when the list is folded).
    assert.equal(await page.textContent(".agents .dock-now"), "◐ 2. Analyse TW.Api · Read(Program.cs)");

    // A redraw (every second, and on every status) keeps the row's elements: a click is never lost to it.
    await page.locator(".agents .agent").nth(1).locator(".stop-one").evaluate((b) => ((b as HTMLElement).dataset.same = "yes"));
    await status("b", 2, "Analyse TW.Api", "running", { activity: "Grep(Main)", steps: 4 });
    assert.equal(await page.locator(".agents .agent").nth(1).locator(".stop-one").getAttribute("data-same"), "yes");
    assert.match((await page.locator(".agents .agent").nth(1).locator(".detail").textContent()) ?? "", /4\/100 steps/);

    await page.click(".agents .open-map");
    assert.deepEqual((await sent()).filter((m) => m.kind === "openNavigator"), [{ kind: "openNavigator" }]);
    await page.locator(".agents .agent").nth(1).locator(".stop-one").click();
    assert.deepEqual((await sent()).filter((m) => m.kind === "stopSubagent"), [{ kind: "stopSubagent", id: "b" }]);

    await status("b", 2, "Analyse TW.Api", "stopped");
    await ev({ type: "tool_result", id: "b", name: "Task", content: "(stopped) partial", is_error: false, summary: "4 tool uses", parent_tool_use_id: null, exit_code: null, duration_ms: null, total_lines: null, stopped: false, images: [] });
    assert.match((await page.locator(".tool").nth(1).textContent()) ?? "", /■ stopped · explore · 3\/100 steps/);
    assert.equal(await page.isVisible(".agents .agent.stopped .stop-one"), false);
    assert.equal(await page.textContent(".agents .dock-count"), "2 active · 1 finished");

    await ev(result); // the turn ended: the rest count as stopped, the map goes
    assert.equal(await page.isVisible(".agents"), false);
    assert.match((await page.locator(".tool").first().textContent()) ?? "", /■ stopped/);
    await page.close();
  });

  test(`${flavour}: progress line while working, and messages queued meanwhile are sent when the turn ends`, async () => {
    const { page, ev, sent } = await panel(flavour);
    await page.fill("textarea", "convert the files");
    await page.press("textarea", "Enter");
    assert.equal(await page.isVisible(".progress"), true);
    assert.match((await page.textContent(".progress .verb")) ?? "", /^(Considering|Pondering|Thinking|Working|Reasoning|Exploring|Analysing|Composing)…$/);
    assert.match((await page.textContent(".progress .meta")) ?? "", /\(\d+s · Esc to interrupt\)/);
    const spark1 = await page.textContent(".progress .spark");
    await page.waitForTimeout(300);
    assert.notEqual(await page.textContent(".progress .spark"), spark1, "the spark is animated");

    await ev({ type: "reasoning_delta", text: "hmm" });
    assert.equal(await page.textContent(".progress .verb"), "Thinking…");
    await ev({ type: "tool_use", id: "1", name: "Bash", input: {}, label: "Bash(dotnet build)", parent_tool_use_id: null });
    assert.equal(await page.textContent(".progress .verb"), "Running…");
    assert.equal(await page.textContent(".progress .detail"), "Bash(dotnet build)");
    await ev({ type: "usage", prompt_tokens: 5000, completion_tokens: 1234, estimated: false, cost: null, context_window: 32768 });
    assert.match((await page.textContent(".progress .meta")) ?? "", /↓ 1\.2k tokens/);

    // Typing meanwhile queues; nothing is sent until the turn ends.
    assert.match((await page.getAttribute("textarea", "placeholder")) ?? "", /^Queue another message/);
    await page.fill("textarea", "also update the README");
    await page.press("textarea", "Enter");
    await page.fill("textarea", "and the docs");
    await page.press("textarea", "Enter");
    assert.deepEqual(await page.locator(".queued .item .text").allTextContents(), ["↳ also update the README", "↳ and the docs"]);
    assert.equal((await sent()).filter((m) => m.kind === "send").length, 1);

    await ev(result);
    const sends = (await sent()).filter((m) => m.kind === "send");
    assert.equal(sends.length, 2);
    assert.equal((sends[1] as { text: string }).text, "also update the README\n\nand the docs");
    assert.equal(await page.isVisible(".queued"), false);
    assert.equal(await page.isVisible(".progress"), true, "the queued message started a new turn");
    await ev(result);
    assert.equal(await page.isVisible(".progress"), false);
    await page.close();
  });

  test(`${flavour}: an interrupted turn gives queued messages back to edit`, async () => {
    const { page, ev, sent } = await panel(flavour);
    await page.fill("textarea", "start");
    await page.press("textarea", "Enter");
    await page.fill("textarea", "next thing");
    await page.press("textarea", "Enter");
    await ev({ ...result, subtype: "interrupted" } as AgentEvent);
    assert.equal(await page.inputValue("textarea"), "next thing");
    assert.equal((await sent()).filter((m) => m.kind === "send").length, 1);
    await page.close();
  });

  test(`${flavour}: critique: the reviewer's findings while it fixes, then the verdict; the progress line says so`, async () => {
    const { page, ev } = await panel(flavour);
    await page.fill("textarea", "fix add");
    await page.press("textarea", "Enter");
    await ev({ type: "tool_use", id: "r1", name: "Task", input: { description: "Review the answer (round 1/2)", subagent_type: "critic" }, label: "Review(the answer, round 1/2)", parent_tool_use_id: null });
    assert.equal(await page.textContent(".progress .verb"), "Reviewing the answer…");
    await ev({
      type: "review_result", round: 1, max_rounds: 2, verdict: "fail", final: false, summary: "Wrong file.",
      issues: [{ severity: "high", problem: "names a.py, the code is in b.py", where: "the answer", fix: "say b.py" }],
    });
    assert.equal(await page.textContent(".progress .verb"), "Fixing what the reviewer found…");
    const fixing = (await page.locator(".note.review").first().textContent()) ?? "";
    assert.match(fixing, /↺ The reviewer found 1 problem; fixing it \(review 1\/2\):/);
    assert.match(fixing, /\[high\] names a\.py, the code is in b\.py \(the answer\)fix: say b\.py/);
    await ev({ type: "assistant_message", text: "Corrected answer.", reasoning: "", tool_calls: [], model: null });
    await ev({ type: "review_result", round: 2, max_rounds: 2, verdict: "pass", final: true, summary: "Now right.", issues: [] });
    assert.equal(await page.locator(".note.review").nth(1).textContent(), "✓ Reviewed: Now right.");
    await ev({ type: "review_result", round: 2, max_rounds: 2, verdict: "fail", final: true, summary: "x", issues: [{ severity: "low", problem: "p", where: "", fix: "" }] });
    assert.match((await page.locator(".note.review").nth(2).textContent()) ?? "", /⚠ Not validated: after 2 reviews the reviewer still found 1 problem:\[low\] p/);
    await ev(result);
    await page.close();
  });

  test(`${flavour}: critique checkbox: shows the state, switches it for every window, follows other windows`, async () => {
    const { page, send, ev, sent } = await panel(flavour);
    const box = page.locator("header .critique input");
    assert.equal(await box.isChecked(), false);
    assert.equal(await box.isEnabled(), true);
    await box.check();
    assert.deepEqual((await sent()).at(-1), { kind: "setCritique", enabled: true });
    await ev({ type: "critique_changed", enabled: true, source: "you" });
    assert.equal(await box.isChecked(), true);
    assert.equal(await page.locator(".note").count(), 0, "your own click needs no note");
    // Switched off in another window (or the terminal): the box follows, with a note.
    await ev({ type: "critique_changed", enabled: false, source: "saved" });
    assert.equal(await box.isChecked(), false);
    assert.equal(await page.textContent(".note.info"), "Critique off (switched in another window).");
    // cmcoder stopped: the box can't be used until it's back.
    await send({ kind: "state", state: "exited", message: "stopped" });
    assert.equal(await box.isDisabled(), true);
    await ev({ type: "system_init", protocol_version: 1, session_id: "t", cwd: "/p", model: "m", provider: "corp", tools: [], permission_mode: "default", critique: true, version: "0.1.0", features: [] });
    assert.equal(await box.isEnabled(), true);
    assert.equal(await box.isChecked(), true);
    await page.close();
  });
}

// --- images: pasted, dropped or picked ------------------------------------------------

// A 1×1 PNG.
const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

/** Fires a paste (or drop) event on the input (or page) carrying these files. */
async function transfer(page: Page, event: "paste" | "drop", files: { name: string; type: string; b64: string }[], text = ""): Promise<void> {
  await page.evaluate(
    ([event, files, text]) => {
      const dt = new DataTransfer();
      for (const f of files) dt.items.add(new File([Uint8Array.from(atob(f.b64), (c) => c.charCodeAt(0))], f.name, { type: f.type }));
      if (text) dt.setData("text/plain", text);
      const target = document.querySelector(event === "paste" ? "textarea" : "footer")!;
      const e = event === "paste"
        ? new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true })
        : new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true });
      target.dispatchEvent(e);
    },
    [event, files, text] as const,
  );
}

for (const flavour of FLAVOURS) {
  test(`${flavour}: a pasted image shows above the input and goes with the message`, async () => {
    const { page, sent } = await panel(flavour);
    await transfer(page, "paste", [{ name: "shot.png", type: "image/png", b64: PNG }]);
    await page.waitForSelector("footer .images .thumb");
    assert.equal(await page.getAttribute("footer .images .thumb", "src"), `data:image/png;base64,${PNG}`);
    await page.fill("textarea", "what's wrong here?");
    await page.press("textarea", "Enter");
    assert.deepEqual((await sent()).at(-1), {
      kind: "send",
      text: "what's wrong here?",
      includeContext: false,
      images: [{ data: PNG, mediaType: "image/png", name: "shot.png" }],
    });
    assert.equal(await page.isVisible("footer .images"), false, "sent: the strip is empty again");
    assert.equal(await page.locator(".msg.user .thumbs .thumb").count(), 1, "the sent message shows it");
    await page.close();
  });
}

test("an image alone can be sent; dropped images and the picker add to the strip; × removes", async () => {
  const { page, sent } = await panel("vscode");
  await transfer(page, "drop", [
    { name: "a.png", type: "image/png", b64: PNG },
    { name: "b.png", type: "image/png", b64: PNG },
  ]);
  await page.waitForFunction(() => document.querySelectorAll("footer .images .thumb").length === 2);
  await page.click("footer .images .item:nth-child(1) .remove");
  assert.equal(await page.locator("footer .images .thumb").count(), 1);
  await page.press("textarea", "Enter"); // no text: the image alone
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "", includeContext: false, images: [{ data: PNG, mediaType: "image/png", name: "b.png" }] });
  // The Image button opens a file picker that takes images only.
  assert.equal(await page.getAttribute("input.image-file", "accept"), "image/png,image/jpeg,image/gif,image/webp");
  await page.close();
});

test("only PNG, JPEG, GIF and WebP are taken, and at most 5", async () => {
  const { page } = await panel("vscode");
  await transfer(page, "paste", [{ name: "x.svg", type: "image/svg+xml", b64: btoa("<svg/>") }]);
  await page.waitForFunction(() => /isn't a PNG, JPEG, GIF or WebP/.test(document.querySelector(".status")?.textContent ?? ""));
  assert.equal(await page.isVisible("footer .images"), false);
  await transfer(page, "paste", Array.from({ length: 6 }, (_, i) => ({ name: `${i}.png`, type: "image/png", b64: PNG })));
  await page.waitForFunction(() => /At most 5 images/.test(document.querySelector(".status")?.textContent ?? ""));
  assert.equal(await page.locator("footer .images .thumb").count(), 5);
  await page.close();
});

test("a paste with no image and no text asks the IDE for the clipboard's image (JavaFX)", async () => {
  const { page, send, sent } = await panel("function");
  await transfer(page, "paste", []);
  await page.waitForFunction(() => (window as unknown as { sent: { kind: string }[] }).sent.some((m) => m.kind === "pasteImage"));
  await send({ kind: "image", data: PNG, mediaType: "image/png", name: "" });
  assert.equal(await page.locator("footer .images .thumb").count(), 1);
  // A paste of text is left to the input, and doesn't ask the IDE.
  const before = (await sent()).length;
  await transfer(page, "paste", [], "some text");
  assert.equal((await sent()).length, before);
  await page.close();
});

test("an older cmcoder that can't take images: the panel says so and keeps them", async () => {
  const { page, ev, sent } = await panel("vscode");
  assert.match((await page.getAttribute(".model", "title")) ?? "", /cmcoder 0\.1\.0 \(abc1234 2026-10-08\)$/);
  // An older cmcoder's system_init has no features (nor version).
  await ev({ type: "system_init", protocol_version: 1, session_id: "t", cwd: "/p", model: "m", provider: "corp", tools: [], permission_mode: "default" } as unknown as AgentEvent);
  assert.match((await page.getAttribute(".model", "title")) ?? "", /cmcoder \(an older version\)$/);
  await transfer(page, "paste", [{ name: "shot.png", type: "image/png", b64: PNG }]);
  await page.waitForSelector("footer .images .thumb");
  await page.fill("textarea", "what's this?");
  const before = (await sent()).length;
  await page.press("textarea", "Enter");
  assert.equal((await sent()).length, before, "not sent: it would ignore the image");
  assert.match((await page.textContent(".note.error")) ?? "", /too old for images\. Update it/);
  assert.match((await page.textContent(".status")) ?? "", /Not sent: this cmcoder program is too old for images/);
  assert.equal(await page.locator("footer .images .thumb").count(), 1, "the image is kept");
  assert.equal(await page.inputValue("textarea"), "what's this?", "and the text");
  assert.equal(await page.isVisible(".stop"), false, "not busy");
  // Without the image, the text goes.
  await page.click("footer .images .item .remove");
  await page.press("textarea", "Enter");
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "what's this?", includeContext: false });
  await page.close();
});

test("Send never does nothing silently: it says why the message didn't go", async () => {
  const { page, send, ev, sent } = await panel("vscode", undefined, false);
  await send({ kind: "state", state: "starting" });
  await transfer(page, "paste", [{ name: "shot.png", type: "image/png", b64: PNG }]);
  await page.waitForSelector("footer .images .thumb");
  await page.fill("textarea", "what the screen shows");
  await page.press("textarea", "Enter");
  assert.match((await page.textContent(".status")) ?? "", /Not sent yet: cmcoder is still starting/);
  assert.equal(await page.inputValue("textarea"), "what the screen shows", "the text stays");
  assert.equal(await page.locator("footer .images .thumb").count(), 1, "and the image");
  await send({ kind: "state", state: "exited", message: "cmcoder stopped." });
  await page.click(".send");
  assert.match((await page.textContent(".status")) ?? "", /Not sent: cmcoder isn't running\. Click Restart/);
  assert.equal((await sent()).filter((m) => m.kind === "send").length, 0);
  // Started: it goes, and the status line clears.
  await send({ kind: "state", state: "ready" });
  await ev({ type: "system_init", protocol_version: 1, session_id: "s", cwd: "/p", model: "m", provider: "corp", tools: [], permission_mode: "default", critique: false, version: "0.1.0", features: ["images"] });
  await page.press("textarea", "Enter");
  assert.equal((await sent()).filter((m) => m.kind === "send").length, 1);
  assert.equal(await page.isVisible(".status"), false);
  await page.close();
});

test("the header and the status line always say what cmcoder is doing", async () => {
  const { page, send, sent } = await panel("vscode", undefined, false);
  await page.clock.install();
  await send({ kind: "state", state: "starting" });
  assert.equal(await page.textContent(".model"), "Starting cmcoder…");
  await page.clock.runFor(12_000); // a slow start says how long, and where to look
  assert.match((await page.textContent(".status")) ?? "", /Starting cmcoder… \(1[23] s; its log says what it's doing\)/);
  await send({ kind: "state", state: "exited", message: "cmcoder stopped." });
  assert.equal(await page.textContent(".model"), "cmcoder stopped");
  // Each state it took goes back to the IDE, for its log.
  assert.deepEqual(
    (await sent()).filter((m) => m.kind === "panelState").map((m) => (m as { state: string }).state),
    ["starting", "exited"],
  );
  await page.close();
});

test("a failure in the page's script shows in the chat and goes to the IDE's log", async () => {
  const { page, sent } = await panel("vscode");
  await page.evaluate(() => window.dispatchEvent(new ErrorEvent("error", { message: "Uncaught TypeError: x is undefined", filename: "https://x/dist/chat.js", lineno: 7 })));
  assert.match((await page.textContent(".note.error")) ?? "", /The chat panel failed: Uncaught TypeError: x is undefined \(chat\.js:7\)/);
  assert.deepEqual((await sent()).at(-1), { kind: "panelError", message: "Uncaught TypeError: x is undefined (chat.js:7)" });
  await page.close();
});

test("a page that reloaded after cmcoder started still sends images", async () => {
  // The host says "ready" again, but this page never saw system_init.
  const { page, send, sent } = await panel("vscode", undefined, false);
  await send({ kind: "state", state: "ready" });
  await transfer(page, "paste", [{ name: "shot.png", type: "image/png", b64: PNG }]);
  await page.waitForSelector("footer .images .thumb");
  await page.fill("textarea", "what the screen shows");
  await page.press("textarea", "Enter");
  const last = (await sent()).at(-1) as { kind: string; images?: unknown[] };
  assert.equal(last.kind, "send");
  assert.equal(last.images?.length, 1);
  await page.close();
});

test("the vision model's description is noted in the chat", async () => {
  const { page, ev } = await panel("vscode");
  await page.fill("textarea", "what is this?");
  await page.press("textarea", "Enter");
  // While it reads the images, the progress line says so.
  await ev({ type: "images_describing", model: "qwen2.5-vl-7b", count: 2 });
  assert.equal(await page.textContent(".progress .verb"), "Reading the images…");
  assert.equal(await page.textContent(".progress .detail"), "with qwen2.5-vl-7b");
  await ev({ type: "images_described", model: "qwen2.5-vl-7b", count: 2 });
  assert.notEqual(await page.textContent(".progress .verb"), "Reading the images…");
  assert.equal(await page.textContent(".note.info"), "◦ qwen2.5-vl-7b described 2 images for the model, which can't see images");
  await page.close();
});

test("the mode picker offers auto (ask only for risky actions), each mode explained", async () => {
  const { page, sent } = await panel("vscode");
  const options = await page.$$eval("header select.mode option", (os) => (os as HTMLOptionElement[]).map((o) => [o.value, o.title]));
  assert.deepEqual(options.map((o) => o[0]), ["default", "acceptEdits", "auto", "plan", "bypassPermissions"]);
  assert.match(options[2][1], /Asks only for risky actions/);
  await page.selectOption("header select.mode", "auto");
  assert.deepEqual((await sent()).at(-1), { kind: "setMode", mode: "auto" });
  await page.close();
});
