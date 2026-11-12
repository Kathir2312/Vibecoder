// An error as a card, in a real browser: what happened, the engine's words, and the buttons it chose.

import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import type { AgentEvent } from "../src/protocol";
import { panel, result, startBrowser, stopBrowser } from "./harness";

before(startBrowser);
after(stopBrowser);

type Err = Extract<AgentEvent, { type: "error" }>;
const gateway: Err = {
  type: "error",
  kind: "gateway",
  message: "Error code: 502 - {'error': {'message': 'Bad Gateway'}}",
  hint: "The gateway is down or overloaded: that is on its side, not in your project. Retry in a moment, or switch to another model.",
  title: "The model gateway is not answering (502)",
  actions: ["retry", "switch_model", "open_log"],
};
const buttons = (page: import("playwright").Page) => page.locator(".alert.error .alert-actions button").allTextContents();

test("an error with a title and actions is a card: headline, the engine's words and its buttons", async () => {
  const { page, ev } = await panel("vscode");
  await ev(gateway);
  assert.equal(await page.getAttribute(".alert.error", "role"), "alert");
  assert.match((await page.textContent(".alert-title")) ?? "", /The model gateway is not answering \(502\)/);
  assert.match((await page.textContent(".alert-message")) ?? "", /502/);
  assert.match((await page.textContent(".alert-hint")) ?? "", /on its side/);
  assert.deepEqual(await buttons(page), ["Retry", "Switch model"], "no Open log: this IDE can't show it");
  assert.equal(await page.locator(".note.error").count(), 0, "not also a plain line");
  await page.close();
});

test("Open log is there only where the IDE can show its log, and asks for it", async () => {
  const { page, ev, sent } = await panel("vscode", undefined, true, "stopTool openLog");
  await ev(gateway);
  assert.deepEqual(await buttons(page), ["Retry", "Switch model", "Open log"]);
  await page.click('.alert button[data-action="open_log"]');
  assert.deepEqual((await sent()).at(-1), { kind: "openLog" });
  assert.equal(await page.locator(".alert.error.resolved").count(), 0, "looking at the log does not end the card");
  await page.close();
});

test("Retry runs /retry without a second copy of the message, and ends the card", async () => {
  const { page, ev, sent } = await panel("vscode");
  await page.fill("textarea", "refactor the controller");
  await page.press("textarea", "Enter");
  await ev(gateway);
  await ev({ ...result, subtype: "error", is_error: true, result: gateway.message });
  assert.equal(await page.locator(".msg.user").count(), 1);
  await page.click('.alert button[data-action="retry"]');
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/retry", includeContext: false });
  assert.equal(await page.locator(".msg.user").count(), 1, "no new bubble: the same request is going again");
  assert.equal(await page.isVisible(".stop"), true, "it is working again");
  assert.equal(await page.locator(".alert.error.resolved").count(), 1);
  assert.equal(await page.textContent(".alert-status"), "Retrying…");
  assert.equal(await page.locator(".alert-actions button:not([disabled])").count(), 0);
  await page.close();
});

test("Switch model puts /model in the box for the user to finish", async () => {
  const { page, ev } = await panel("vscode");
  await ev(gateway);
  await page.click('.alert button[data-action="switch_model"]');
  assert.equal(await page.inputValue("textarea"), "/model ");
  assert.equal(await page.evaluate(() => document.activeElement?.tagName), "TEXTAREA");
  await page.close();
});

test("a conversation that is too long offers Compact and a new conversation", async () => {
  const { page, ev, sent } = await panel("vscode");
  await ev({ type: "error", kind: "context_length", message: "maximum context length is 4096", hint: null, title: "The conversation is too long for the model", actions: ["compact", "new_conversation"] });
  assert.deepEqual(await buttons(page), ["Compact", "New conversation"]);
  await page.click('.alert button[data-action="new_conversation"]');
  assert.deepEqual((await sent()).at(-1), { kind: "newConversation" });
  assert.equal(await page.textContent(".alert-status"), "Started a new conversation.");
  await page.close();

  const again = await panel("vscode");
  await again.ev({ type: "error", kind: "context_length", message: "x", hint: null, title: "Too long", actions: ["compact"] });
  await again.page.click('.alert button[data-action="compact"]');
  assert.deepEqual((await again.sent()).at(-1), { kind: "send", text: "/compact", includeContext: false });
  await again.page.close();
});

test("a new message ends the buttons of every earlier error: they would act on the wrong request", async () => {
  const { page, ev } = await panel("vscode");
  await ev(gateway);
  await ev({ ...gateway, message: "second" });
  await page.fill("textarea", "never mind, do this instead");
  await page.press("textarea", "Enter");
  assert.equal(await page.locator(".alert.error.resolved").count(), 2);
  assert.equal(await page.locator(".alert-actions button:not([disabled])").count(), 0);
  await page.close();
});

test("a long engine message is cut for reading and complete under Full message", async () => {
  const { page, ev } = await panel("vscode");
  const long = `Error code: 502 - ${"x".repeat(900)}\nsecond line`;
  await ev({ ...gateway, message: long });
  assert.ok(((await page.textContent(".alert-message")) ?? "").length < 300);
  await page.click(".alert-more summary");
  assert.equal(await page.textContent(".alert-more pre"), long);
  await page.close();
});

test("an error without a title or buttons, or from an older cmcoder, stays a plain line", async () => {
  const { page, ev } = await panel("vscode");
  await ev({ type: "error", kind: "busy", message: "A turn is already running; interrupt it first.", hint: null, title: "", actions: [] });
  const old = { type: "error", kind: "server", message: "from an older cmcoder", hint: "a hint" } as unknown as AgentEvent; // no title, no actions at all
  await ev(old);
  assert.equal(await page.locator(".alert").count(), 0);
  assert.deepEqual(await page.locator(".note.error").allTextContents(), ["✗ A turn is already running; interrupt it first.", "✗ from an older cmcoder\na hint"]);
  await page.close();
});

test("a title without buttons is a card with no button row", async () => {
  const { page, ev } = await panel("vscode");
  await ev({ type: "error", kind: "server", message: "bad field", hint: null, title: "The model refused the request (400)", actions: [] });
  assert.equal(await page.locator(".alert.error").count(), 1);
  assert.equal(await page.locator(".alert-actions").count(), 0);
  await page.close();
});

test("a fallback model is named on the model label, the failed reply is dropped, and the usual model comes back", async () => {
  const { page, ev } = await panel("vscode");
  await ev({ type: "system_init", protocol_version: 1, session_id: "s", cwd: "/p", model: "qwen3-27b", provider: "mock", tools: [], permission_mode: "default", critique: false, version: "1", features: [] });
  assert.equal((await page.textContent(".model")) ?? "", "qwen3-27b");
  await page.fill("textarea", "go");
  await page.press("textarea", "Enter");
  await ev({ type: "assistant_delta", text: "Half of an answ" }); // the failed model's reply
  await page.waitForSelector(".msg.assistant");
  await ev({ type: "warning", message: "qwen3-27b: The model gateway is not answering (502). Finishing this request with qwen3-7b; your next message tries qwen3-27b again." });
  await ev({ type: "model_changed", model: "qwen3-7b", context_window: 32768, reason: "fallback" });
  assert.equal(await page.textContent(".model"), "qwen3-7b · fallback");
  assert.match((await page.getAttribute(".model", "title")) ?? "", /next message tries the usual model/);
  assert.equal(await page.locator(".msg.assistant").count(), 0, "what the failed model wrote is not kept");
  await ev({ type: "assistant_delta", text: "The whole answer." });
  await ev({ type: "assistant_message", text: "The whole answer.", reasoning: "", tool_calls: [], model: null });
  await page.waitForFunction(() => document.querySelector(".msg.assistant")?.textContent?.includes("The whole answer."));
  assert.equal(await page.locator(".msg.assistant").count(), 1);
  assert.match((await page.textContent(".note.warning, .alert.warning, .note")) ?? "", /Finishing this request with qwen3-7b/);
  await ev({ ...result });
  await ev({ type: "model_changed", model: "qwen3-27b", context_window: 32768, reason: "restore" });
  assert.equal(await page.textContent(".model"), "qwen3-27b");
  await page.close();
});
