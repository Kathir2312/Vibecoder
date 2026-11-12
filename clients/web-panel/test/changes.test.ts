// What a request changed, in a real browser: the bar that ends it, the review of every file and hunk, and
// the buttons that take parts back (which only send numbers; the engine does the rest).

import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import type { AgentEvent } from "../src/protocol";
import { panel, result, startBrowser, stopBrowser } from "./harness";

before(startBrowser);
after(stopBrowser);

type Changes = Extract<AgentEvent, { type: "turn_changes" }>;
type File = Changes["files"][number];

const hunk = (id: number, start: number, lines: [string, string][], reverted = false) => ({
  id, old_start: start, new_start: start, reverted,
  lines: lines.map(([kind, text]) => ({ kind: kind as "ctx" | "add" | "del", text })),
});
const file = (over: Partial<File> = {}): File => ({
  index: 0, path: "Controllers/OrdersController.cs", status: "modified", added: 2, removed: 1, binary: false, truncated: false, reverted: false, note: "",
  hunks: [
    hunk(0, 24, [["ctx", "[HttpGet]"], ["del", "public async Task<IActionResult> Index(int page = 1)"], ["add", "public async Task<IActionResult> Index("], ["add", "    OrderStatus? status, int page = 1)"], ["ctx", "{"]]),
    hunk(1, 40, [["ctx", "return View(model);"], ["add", "ViewBag.Status = status;"]]),
  ],
  ...over,
});
const added = (over: Partial<File> = {}): File => file({ index: 1, path: "Tests/OrdersControllerTests.cs", status: "added", added: 34, removed: 0, hunks: [hunk(0, 1, [["add", "public class OrdersControllerTests {}"]])], ...over });
const changes = (files: File[], turn = 1): Changes => ({
  type: "turn_changes", turn, files, added: files.reduce((n, f) => n + f.added, 0), removed: files.reduce((n, f) => n + f.removed, 0),
});

test("a request that changed files ends with a bar: how many files, how many lines, and what to do", async () => {
  const { page, ev } = await panel("vscode");
  await ev(changes([file(), added()]));
  assert.equal(await page.locator(".changes-bar").count(), 1);
  assert.match((await page.textContent(".changes-text")) ?? "", /2 files changed\s+\+36 −1/);
  assert.deepEqual(await page.locator(".changes-bar button").allTextContents(), ["Review changes", "Undo all"]);
  await page.close();
});

test("Review changes replaces the conversation with every file and hunk, and Chat goes back", async () => {
  const { page, ev } = await panel("vscode");
  await ev(changes([file(), added()]));
  await page.click('.changes-bar button:has-text("Review changes")');
  assert.equal(await page.isVisible(".changes-view"), true);
  assert.equal(await page.isVisible(".log-wrap"), false);
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "← Chat", "the keyboard is in the view");
  assert.deepEqual(await page.locator(".file-path").allTextContents(), ["Controllers/OrdersController.cs", "Tests/OrdersControllerTests.cs"]);
  assert.deepEqual(await page.locator(".file-status").allTextContents(), ["M", "A"]);
  assert.equal(await page.locator(".review-file:nth-of-type(1) .hunk").count(), 2);
  assert.deepEqual(await page.locator(".review-file:nth-of-type(1) .hunk:nth-child(1) .dl.del .dl-text").allTextContents(), ["public async Task<IActionResult> Index(int page = 1)"]);
  assert.equal(await page.locator(".review-file:nth-of-type(1) .hunk:nth-child(1) .dl.add").count(), 2);
  assert.equal(await page.locator(".review-file:nth-of-type(1) .hunk:nth-child(1) .dl.ctx").count(), 2);
  await page.click(".review-back");
  assert.equal(await page.isVisible(".changes-view"), false);
  assert.equal(await page.isVisible(".log-wrap"), true);
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Review changes", "the keyboard is back where it was");
  await page.close();
});

test("a hunk, a file and the whole request are taken back by number, and the panel waits for the engine", async () => {
  const { page, ev, sent } = await panel("vscode");
  await ev(changes([file(), added()]));
  await page.click('.changes-bar button:has-text("Review changes")');
  await page.click('.review-file:nth-of-type(1) .hunk:nth-child(2) button:has-text("Revert hunk")');
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/revert 1 0 1", includeContext: false });
  assert.equal(await page.locator(".changes-view button:not([disabled])").count(), 1, "only Chat works while the engine is busy");
  await ev(result);
  await page.click('.review-file:nth-of-type(2) button:has-text("Delete file")');
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/revert 1 1", includeContext: false });
  await ev(result);
  await page.click('.changes-view button:has-text("Revert all")');
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/revert 1 all", includeContext: false });
  assert.equal(await page.locator(".msg.user").count(), 0, "no bubble: it is not a message to the model");
  await page.close();
});

test("a file with one hunk has no hunk button: the file's is the same", async () => {
  const { page, ev } = await panel("vscode");
  await ev(changes([file({ hunks: [hunk(0, 1, [["del", "a"], ["add", "b"]])] }), added()]));
  await page.click('.changes-bar button:has-text("Review changes")');
  assert.equal(await page.locator('button:has-text("Revert hunk")').count(), 0);
  assert.equal(await page.locator('button:has-text("Revert file")').count(), 1);
  await page.close();
});

test("when the engine says what is back, the review and the bar follow", async () => {
  const { page, ev } = await panel("vscode");
  await ev(changes([file(), added()]));
  await page.click('.changes-bar button:has-text("Review changes")');
  const partly = file({ hunks: [hunk(0, 24, [["del", "x"], ["add", "y"]], true), hunk(1, 40, [["add", "z"]])] });
  await ev(changes([partly, added()]));
  assert.equal(await page.locator(".review-file:nth-of-type(1) .hunk.back").count(), 1);
  assert.match((await page.textContent(".review-file:nth-of-type(1) .hunk.back .hunk-head")) ?? "", /taken back/);
  assert.equal(await page.locator(".changes-bar").count(), 1, "the same bar, updated");
  await ev(changes([file({ reverted: true }), added({ reverted: true })]));
  assert.match((await page.textContent(".changes-text")) ?? "", /All 2 files taken back/);
  assert.equal(await page.locator(".changes-undo").isVisible(), false);
  assert.equal(await page.locator(".review-file.back").count(), 2);
  assert.equal(await page.locator(".file-back").nth(1).textContent(), "deleted");
  await page.close();
});

test("a part that is back, and a file with only some back, are said in the bar", async () => {
  const { page, ev } = await panel("vscode");
  await ev(changes([file({ reverted: true }), added()]));
  assert.match((await page.textContent(".changes-text")) ?? "", /2 files changed/);
  assert.match((await page.textContent(".changes-sub")) ?? "", /1 of 2 taken back/);
  await page.close();
});

test("a refusal is shown on its file, and the odd kinds of file say what can be done", async () => {
  const { page, ev } = await panel("vscode");
  await ev(changes([
    file({ note: "changed since cmcoder wrote it, so it is left alone (edit it yourself, or use /rewind)" }),
    added({ index: 1, path: "logo.png", status: "modified", binary: true, hunks: [] }),
    added({ index: 2, path: "big.cs", status: "modified", truncated: true, hunks: [], added: 900, removed: 900 }),
  ]));
  await page.click('.changes-bar button:has-text("Review changes")');
  assert.match((await page.textContent(".review-file:nth-of-type(1) .file-note")) ?? "", /changed since cmcoder wrote it/);
  assert.match((await page.textContent(".review-file:nth-of-type(2) .file-sub")) ?? "", /Not a text file/);
  assert.match((await page.textContent(".review-file:nth-of-type(3) .file-sub")) ?? "", /too big to show/);
  await page.close();
});

test("a new message closes the review so the answer is seen, and a new conversation forgets the bars", async () => {
  const { page, ev, send } = await panel("vscode");
  await ev(changes([file()]));
  await page.click('.changes-bar button:has-text("Review changes")');
  await page.fill("textarea", "and now?");
  await page.press("textarea", "Enter");
  assert.equal(await page.isVisible(".changes-view"), false);
  assert.equal(await page.isVisible(".log-wrap"), true);
  await send({ kind: "reset" });
  assert.equal(await page.locator(".changes-bar").count(), 0);
  await page.close();
});

test("text in a file is shown as text, never run", async () => {
  const { page, ev } = await panel("vscode");
  const evil = '<img src=x onerror="window.pwned=1"><script>window.pwned=2</script>';
  await ev(changes([file({ path: '<b>x</b>.cs', hunks: [hunk(0, 1, [["add", evil]])] })]));
  await page.click('.changes-bar button:has-text("Review changes")');
  assert.equal(await page.locator(".changes-view img, .changes-view script, .changes-view b").count(), 0);
  assert.equal(await page.textContent(".dl.add .dl-text"), evil);
  assert.equal(await page.textContent(".file-path"), "<b>x</b>.cs");
  assert.equal(await page.evaluate(() => (window as unknown as { pwned?: number }).pwned), undefined);
  await page.close();
});
