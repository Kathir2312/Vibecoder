// The agent's plan as a card, in a real browser: read it, edit it, approve it or send it back.

import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { linesOf, planOf } from "../src/chat/plancard";
import { ask, panel, result, startBrowser, stopBrowser } from "./harness";

before(startBrowser);
after(stopBrowser);

const PLAN = {
  title: "Orders status filter",
  steps: [
    { text: "Add a status parameter to Index", files: ["Controllers/OrdersController.cs"] },
    { text: "Add the dropdown to the view", files: ["Views/Orders/Index.cshtml", "Views/Shared/_Pager.cshtml"] },
    { text: "Add tests", files: [] },
  ],
  out_of_scope: "The database",
};
const request = (id = "p1") =>
  ask({ request_id: id, tool_use_id: `t-${id}`, name: "ExitPlanMode", label: "Plan: Orders status filter", input: PLAN, suggested_rule: "", can_remember: false, reasons: ["plan mode: nothing changes until you approve"] });
const FEATURES = "stopTool sessionRules";

test("a plan is a card: title, a count of steps and files, numbered steps with their files, what is out of scope", async () => {
  const { page, ev } = await panel("vscode", undefined, true, FEATURES);
  await ev(request());
  assert.equal(await page.locator(".permission.plan").count(), 1);
  assert.equal(await page.textContent(".plan .ask-title"), "Plan · Orders status filter");
  assert.equal(await page.textContent(".plan-meta"), "3 steps · 3 files");
  assert.deepEqual(await page.locator(".plan-text").allTextContents(), PLAN.steps.map((s) => s.text));
  assert.deepEqual(await page.locator(".plan-step:nth-child(2) .plan-file").allTextContents(), ["Views/Orders/Index.cshtml", "Views/Shared/_Pager.cshtml"]);
  assert.equal(await page.locator(".plan-step:nth-child(3) .plan-file").count(), 0);
  assert.equal(await page.textContent(".plan-scope"), "Out of scope: The database");
  assert.deepEqual(await page.locator(".plan .ask-actions button").allTextContents(), ["Approve and run", "Approve, accept edits", "Edit steps", "Revise…", "Reject"]);
  await page.close();
});

test("Approve and run is an approval; the card says so and keeps the plan", async () => {
  const { page, ev, sent } = await panel("vscode", undefined, true, FEATURES);
  await ev(request());
  await page.click('.plan button:has-text("Approve and run")');
  assert.deepEqual((await sent()).at(-1), { kind: "permission", requestId: "p1", allow: true, remember: false });
  assert.equal(await page.textContent(".plan .answer"), "└ Approved");
  assert.equal(await page.locator(".plan button").count(), 0, "the buttons are gone");
  assert.equal(await page.locator(".plan-step").count(), 3, "the plan stays to be read");
  await page.close();
});

test("Approve, accept edits asks for the session flag, and only where the IDE can", async () => {
  const { page, ev, sent } = await panel("vscode", undefined, true, FEATURES);
  await ev(request());
  await page.click('.plan button:has-text("Approve, accept edits")');
  assert.deepEqual((await sent()).at(-1), { kind: "permission", requestId: "p1", allow: true, remember: false, rememberSession: true });
  assert.match((await page.textContent(".plan .answer")) ?? "", /Approved · edits are accepted/);
  await page.close();

  const plain = await panel("vscode", undefined, true, "stopTool"); // an IDE without session rules
  await plain.ev(request());
  assert.equal(await plain.page.locator('.plan button:has-text("accept edits")').count(), 0);
  await plain.page.close();
});

test("edited steps are drawn at once and travel with the approval, one per line", async () => {
  const { page, ev, sent } = await panel("vscode", undefined, true, FEATURES);
  await ev(request());
  await page.click('.plan button:has-text("Edit steps")');
  assert.equal(await page.inputValue(".plan-edit-box"), "1. Add a status parameter to Index\n2. Add the dropdown to the view\n3. Add tests");
  assert.equal(await page.locator(".plan .ask-actions").isVisible(), false, "the buttons make room for the box");
  await page.fill(".plan-edit-box", "1. Add a status parameter to Index\n- Keep the filter in the paging links\n\n3) Add tests");
  await page.click('.plan button:has-text("Use these steps")');
  assert.deepEqual(await page.locator(".plan-text").allTextContents(), ["Add a status parameter to Index", "Keep the filter in the paging links", "Add tests"]);
  assert.equal(await page.locator(".plan-step:nth-child(1) .plan-file").count(), 1, "a step left as it was keeps its files");
  assert.equal(await page.locator(".plan-step:nth-child(2) .plan-file").count(), 0);
  assert.equal(await page.textContent(".plan-meta"), "3 steps · 1 file · edited by you");
  await page.click('.plan button:has-text("Approve and run")');
  assert.deepEqual((await sent()).at(-1), {
    kind: "permission", requestId: "p1", allow: true, remember: false,
    feedback: "Add a status parameter to Index\nKeep the filter in the paging links\nAdd tests",
  });
  assert.match((await page.textContent(".plan .answer")) ?? "", /with your changes/);
  await page.close();
});

test("editing and changing nothing is not an edit; Escape and Cancel leave the plan alone", async () => {
  const { page, ev, sent } = await panel("vscode", undefined, true, FEATURES);
  await ev(request());
  await page.click('.plan button:has-text("Edit steps")');
  await page.click('.plan button:has-text("Use these steps")');
  assert.equal(await page.textContent(".plan-meta"), "3 steps · 3 files");
  await page.click('.plan button:has-text("Edit steps")');
  await page.fill(".plan-edit-box", "something else entirely");
  await page.press(".plan-edit-box", "Escape");
  assert.equal(await page.locator(".plan-edit").isVisible(), false);
  assert.equal(await page.locator(".plan-text").count(), 3);
  await page.click('.plan button:has-text("Approve and run")');
  assert.deepEqual((await sent()).at(-1), { kind: "permission", requestId: "p1", allow: true, remember: false });
  await page.close();
});

test("Revise sends the plan back with a note, which the model reads as a denial's feedback", async () => {
  const { page, ev, sent } = await panel("vscode", undefined, true, FEATURES);
  await ev(request());
  await page.click('.plan button:has-text("Revise")');
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "What to change in the plan");
  await page.fill(".plan .ask-deny input", "drop the tests, we have them");
  await page.press(".plan .ask-deny input", "Enter");
  assert.deepEqual((await sent()).at(-1), { kind: "permission", requestId: "p1", allow: false, remember: false, feedback: "drop the tests, we have them" });
  assert.match((await page.textContent(".plan .answer")) ?? "", /Sent back · “drop the tests, we have them”/);
  assert.equal(await page.locator(".note.warn").count(), 0, "no second line for the denial");
  await page.close();
});

test("Reject stops: a denial with no note", async () => {
  const { page, ev, sent } = await panel("vscode", undefined, true, FEATURES);
  await ev(request());
  await page.click('.plan button:has-text("Reject")');
  assert.deepEqual((await sent()).at(-1), { kind: "permission", requestId: "p1", allow: false, remember: false, feedback: "" });
  assert.equal(await page.textContent(".plan .answer"), "└ Rejected");
  await page.close();
});

test("a plan nobody answered is closed when the turn ends, and a plan with odd input still draws", async () => {
  const { page, ev } = await panel("vscode", undefined, true, FEATURES);
  await ev(request("a"));
  await ev({ ...result, subtype: "interrupted", is_error: false });
  assert.match((await page.textContent(".plan .answer")) ?? "", /Cancelled/);
  await ev(ask({ request_id: "odd", tool_use_id: "t2", name: "ExitPlanMode", label: "Plan", input: { title: 5, steps: [null, { text: 7 }, { text: "ok", files: ["a", 3] }] }, suggested_rule: "" }));
  assert.deepEqual(await page.locator(".permission.plan:last-of-type .plan-text").allTextContents(), ["ok"]);
  assert.equal(await page.locator(".permission.plan:last-of-type .plan-file").count(), 1);
  await page.close();
});

test("the mode list follows when the plan is approved", async () => {
  const { page, ev } = await panel("vscode", undefined, true, FEATURES);
  await ev({ type: "mode_changed", mode: "plan" });
  assert.equal(await page.inputValue(".mode"), "plan");
  await ev({ type: "mode_changed", mode: "default" });
  assert.equal(await page.inputValue(".mode"), "default");
  await page.close();
});

test("the parts: the plan out of anything, and the lines out of a box", () => {
  assert.deepEqual(planOf(undefined), { title: "Plan", steps: [], outOfScope: "" });
  assert.deepEqual(linesOf("1. a\n  - b\n\n3) c\n* d\n•e"), ["a", "b", "c", "d", "e"]);
});
