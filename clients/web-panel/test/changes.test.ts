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
  index: 0, path: "Controllers/OrdersController.cs", status: "modified", added: 2, removed: 1, binary: false, truncated: false, reverted: false, kept: false, note: "",
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
  assert.deepEqual(await page.locator(".changes-bar button").allTextContents(), ["Review changes", "Keep all", "Reject all"]);
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
  await page.click('.review-file:nth-of-type(1) .hunk:nth-child(2) .hunk-revert');
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/revert 1 0 1", includeContext: false });
  assert.equal(await page.locator(".reject-file:not([disabled]), .hunk-revert:not([disabled]), .review-reject-all:not([disabled])").count(), 0, "Reject waits while the engine is busy");
  await ev(result);
  await page.click('.review-file:nth-of-type(2) .reject-file');
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/revert 1 1", includeContext: false });
  await ev(result);
  await page.click('.changes-view .review-reject-all');
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/revert 1 all", includeContext: false });
  assert.equal(await page.locator(".msg.user").count(), 0, "no bubble: it is not a message to the model");
  await page.close();
});

test("a file with one hunk has no hunk button: the file's is the same", async () => {
  const { page, ev } = await panel("vscode");
  await ev(changes([file({ hunks: [hunk(0, 1, [["del", "a"], ["add", "b"]])] }), added()]));
  await page.click('.changes-bar button:has-text("Review changes")');
  assert.equal(await page.locator('button:has-text("Reject hunk")').count(), 0);
  assert.equal(await page.locator(".reject-file").count(), 2, "one Reject for each file");
  assert.equal(await page.locator(".keep-file").count(), 2, "and one Keep");
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
  assert.equal(await page.locator(".changes-undo").evaluate((e) => (e as HTMLElement).hidden), true, "nothing left to reject");
  assert.equal(await page.locator(".review-file.back").count(), 2);
  assert.equal(await page.locator(".file-back").nth(1).textContent(), "deleted");
  await page.close();
});

test("a part that is back, and a file with only some back, are said in the bar", async () => {
  const { page, ev } = await panel("vscode");
  await ev(changes([file({ reverted: true }), added()]));
  assert.match((await page.textContent(".changes-text")) ?? "", /2 files changed/);
  assert.match((await page.textContent(".changes-sub")) ?? "", /1 taken back of 2/);
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

test("each file has Keep and Reject, and the review says why Reject is not available while a request runs", async () => {
  const { page, ev } = await panel("vscode");
  await ev(changes([file(), added()]));
  await page.click('.changes-bar button:has-text("Review changes")');
  assert.deepEqual(await page.locator(".review-file:nth-of-type(1) .file-head button").allTextContents(), ["Keep", "Reject"]);
  assert.equal(await page.locator(".review-busy").count(), 0, "nothing to explain while idle");
  assert.equal(await page.locator(".reject-file").first().getAttribute("aria-label"), "Reject Controllers/OrdersController.cs");
  assert.equal(await page.locator(".keep-file").first().getAttribute("aria-label"), "Keep Controllers/OrdersController.cs");
  await page.fill("footer textarea", "go on");
  await page.press("footer textarea", "Enter"); // a request is running now (and the review closes: the answer comes in the chat)
  await ev(changes([file(), added()]));
  await page.click('.changes-bar button:has-text("Review changes")');
  assert.match((await page.textContent(".review-busy")) ?? "", /Keep works now; Reject waits until it ends/);
  assert.equal(await page.locator(".reject-file:not([disabled])").count(), 0);
  assert.equal(await page.locator(".keep-file:not([disabled])").count(), 2, "approving does not wait");
  assert.match((await page.getAttribute(".reject-file", "title")) ?? "", /Waits until the request ends/);
  await ev(result);
  assert.equal(await page.locator(".review-busy").count(), 0, "gone when the request ends");
  assert.equal(await page.locator(".reject-file:not([disabled])").count(), 2);
  await page.close();
});

test("Keep sends /keep with numbers, needs no waiting, and the file shows as kept when the engine says so", async () => {
  const { page, ev, sent } = await panel("vscode");
  await ev(changes([file(), added()]));
  await page.click('.changes-bar button:has-text("Review changes")');
  await page.click(".review-file:nth-of-type(2) .keep-file");
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/keep 1 1", includeContext: false });
  assert.equal(await page.locator(".review-back").isEnabled(), true);
  assert.equal(await page.locator(".keep-file:not([disabled])").count(), 2, "the panel did not lock up waiting");
  await ev(changes([file(), added({ kept: true })]));
  assert.equal(await page.locator(".review-file.kept").count(), 1);
  assert.equal(await page.textContent(".review-file.kept .file-kept"), "✓ kept");
  assert.equal(await page.locator(".review-file.kept .keep-file").count(), 0, "kept: no Keep again");
  assert.equal(await page.locator(".review-file.kept .reject-file").count(), 1, "but it can still be rejected");
  assert.match((await page.textContent(".changes-sub")) ?? "", /1 kept of 2/);
  await page.click(".review-keep-all");
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/keep 1 all", includeContext: false });
  await ev(changes([file({ kept: true }), added({ kept: true })]));
  assert.match((await page.textContent(".changes-text")) ?? "", /All 2 files reviewed/);
  // (the bar is in the conversation, which is out of sight while the review is open: ask the buttons themselves)
  const hidden = (sel: string) => page.locator(sel).evaluate((e) => (e as HTMLElement).hidden);
  assert.equal(await hidden(".changes-keep"), true, "nothing left to keep");
  assert.equal(await hidden(".changes-undo"), false, "but all can still be rejected");
  assert.equal(await page.locator(".review-keep-all").isDisabled(), true);
  assert.equal(await page.locator(".msg.user").count(), 0, "no bubble: it is not a message to the model");
  await page.close();
});

test("the bar's Keep all and Reject all send the whole-request commands", async () => {
  const { page, ev, sent } = await panel("vscode");
  await ev(changes([file(), added()]));
  await page.click(".changes-keep");
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/keep 1 all", includeContext: false });
  await page.click(".changes-undo");
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/revert 1 all", includeContext: false });
  await page.close();
});
