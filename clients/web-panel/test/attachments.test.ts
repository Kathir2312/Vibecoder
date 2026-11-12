// What waits to go with the next message, as chips above the message box: picked page elements,
// each with a way to take it off (the engine keeps them and does the taking off).

import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import type { AgentEvent } from "../src/protocol";
import { panel, result, startBrowser, stopBrowser } from "./harness";

before(startBrowser);
after(stopBrowser);

const chips = (...labels: string[]): AgentEvent => ({
  type: "attachments",
  items: labels.map((label, index) => ({ index, kind: "element" as const, label })),
});

test("picked elements show as chips above the box, in the order they were picked", async () => {
  const { page, ev } = await panel("vscode");
  assert.equal(await page.isVisible(".attachments"), false, "nothing waits: no row");
  await ev(chips('button[data-testid="save"] "Save" on /Orders', "h1 on /"));
  assert.equal(await page.isVisible(".attachments"), true);
  assert.equal(await page.textContent(".attachments-title"), "With your next message:");
  assert.deepEqual(await page.locator(".attachments .chip-text").allTextContents(), ['⌖ button[data-testid="save"] "Save" on /Orders', "⌖ h1 on /"]);
  assert.equal(await page.getAttribute(".attachments", "role"), "group");
  assert.equal(await page.getAttribute(".attachments .chip-x >> nth=1", "aria-label"), "Remove h1 on /");
  await page.close();
});

test("the cross takes one off by its number, without a bubble, and the engine's list replaces the chips", async () => {
  const { page, ev, sent } = await panel("vscode");
  await ev(chips("first", "second", "third"));
  await page.click('.attachments button[aria-label="Remove second"]');
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/pick drop 1", includeContext: false });
  assert.equal(await page.locator(".msg.user").count(), 0, "not a message to the model");
  assert.equal(await page.locator(".attachments .chip-x:not([disabled])").count(), 0, "they wait while the engine works");
  await ev(chips("first", "third"));
  await ev(result);
  assert.deepEqual(await page.locator(".attachments .chip-text").allTextContents(), ["⌖ first", "⌖ third"]);
  assert.equal(await page.locator(".attachments .chip-x:not([disabled])").count(), 2);
  await page.close();
});

test("when the message takes them the row goes, and a new conversation has none", async () => {
  const { page, ev, send } = await panel("vscode");
  await ev(chips("first"));
  await page.fill("textarea", "make this button red");
  await page.press("textarea", "Enter");
  await ev(chips()); // the engine: the message took them
  assert.equal(await page.isVisible(".attachments"), false);
  await ev(chips("again"));
  await send({ kind: "reset" });
  assert.equal(await page.isVisible(".attachments"), false);
  await page.close();
});

test("a label is shown as text, never as markup, and a long one is cut by the chip", async () => {
  const { page, ev } = await panel("vscode");
  const evil = '<img src=x onerror="window.pwned=1"> ' + "x".repeat(400);
  await ev(chips(evil));
  assert.equal(await page.locator(".attachments img").count(), 0);
  assert.ok(((await page.textContent(".attachments .chip-text")) ?? "").startsWith('⌖ <img src=x onerror="window.pwned=1">'));
  assert.equal(await page.evaluate(() => (window as unknown as { pwned?: number }).pwned), undefined);
  const chip = await page.locator(".attachments .chip").boundingBox();
  const row = await page.locator(".attachments").boundingBox();
  assert.ok(chip !== null && row !== null && chip.width <= row.width, "the chip stays inside its row, the label is cut with an ellipsis");
  await page.close();
});
