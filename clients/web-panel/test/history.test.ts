// A long conversation opens fast: the newest messages are drawn at once, older ones on demand (or by
// scrolling up), and what is being read never moves while they are drawn.

import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import type { Page } from "playwright";
import type { AgentEvent } from "../src/protocol";
import { panel, startBrowser, stopBrowser } from "./harness";

before(startBrowser);
after(stopBrowser);

type Item = Extract<AgentEvent, { type: "history" }>["messages"][number];
const messages = (n: number): Item[] =>
  Array.from({ length: n }, (_, i) => ({ role: (["user", "tool", "assistant"] as const)[i % 3], text: i % 3 === 1 ? `Read(file${i}.cs)` : `message ${i}` }));
const history = (n: number): AgentEvent => ({ type: "history", messages: messages(n) });
const drawn = (page: Page) => page.locator(".log > .msg, .log > .tool").count();
const label = (page: Page) => page.textContent(".earlier");

test("the newest messages are drawn at once, with a button for the rest", async () => {
  const { page, ev } = await panel("vscode");
  await ev(history(500));
  assert.equal(await drawn(page), 80, "80 messages (the 'resumed' note is not one of them)");
  assert.equal(await label(page), "↑ Show earlier messages (420 not shown)");
  const texts = await page.locator(".log > .msg").allTextContents();
  assert.equal(texts.at(-1), "message 498", "the newest is last (499 is a tool line)");
  assert.ok(await page.evaluate(() => { const l = document.querySelector(".log")!; return l.scrollHeight - l.scrollTop - l.clientHeight < 40; }), "the end is in view");
  await page.close();
});

test("a short conversation has no button at all", async () => {
  const { page, ev } = await panel("vscode");
  await ev(history(80));
  assert.equal(await page.locator(".earlier").count(), 0);
  assert.equal(await drawn(page), 80);
  await page.close();
});

/** The first message fully in view, and how far below the top of the conversation it is. */
const probe = (page: Page) =>
  page.evaluate(() => {
    const log = document.querySelector(".log")!.getBoundingClientRect();
    const first = Array.from(document.querySelectorAll(".log > .msg")).find((m) => m.getBoundingClientRect().top >= log.top)!;
    return { text: first.textContent, top: Math.round(first.getBoundingClientRect().top - log.top) };
  });

test("Show earlier draws more above, and the message being read stays where it is", async () => {
  const { page, ev } = await panel("vscode");
  await ev(history(500));
  await page.mouse.move(300, 300);
  await page.mouse.wheel(0, -700); // the user scrolls up to read: the panel stops following the end
  await page.waitForTimeout(400);
  const before = await probe(page);
  // Not Playwright's click: it would first scroll the button into view, which is not what is tested.
  await page.evaluate(() => (document.querySelector(".earlier") as HTMLElement).click());
  assert.equal(await drawn(page), 230);
  assert.equal(await label(page), "↑ Show earlier messages (270 not shown)");
  const after = await probe(page);
  assert.equal(after.text, before.text, "the same message is at the top of the view");
  assert.ok(Math.abs(after.top - before.top) <= 2, `it moved ${after.top - before.top}px`);
  await page.close();
});

test("at the very top, where a browser does not anchor the scroll, the message being read still stays", async () => {
  const { page, ev } = await panel("vscode");
  await page.evaluate(() => { (window as unknown as { IntersectionObserver?: unknown }).IntersectionObserver = undefined; }); // only the button draws more
  await ev(history(500));
  await page.mouse.move(300, 300);
  await page.mouse.wheel(0, -100000);
  await page.waitForTimeout(500);
  assert.equal(await page.evaluate(() => document.querySelector(".log")!.scrollTop), 0, "at the top");
  const where = (text: string) =>
    page.evaluate((t) => {
      const log = document.querySelector(".log")!.getBoundingClientRect();
      const m = Array.from(document.querySelectorAll(".log > .msg")).find((x) => x.textContent === t)!;
      return Math.round(m.getBoundingClientRect().top - log.top);
    }, text);
  const before = await where("message 420"); // the oldest one drawn, just under the button
  await page.click(".earlier");
  assert.equal(await drawn(page), 230);
  const after = await where("message 420");
  assert.ok(Math.abs(after - before) <= 2, `message 420 moved ${after - before}px`);
  await page.close();
});

test("scrolling up to the button draws more without clicking, until everything is drawn, in order", async () => {
  const { page, ev } = await panel("vscode");
  await ev(history(400));
  for (let i = 0; i < 10 && (await page.locator(".earlier").count()) > 0; i++) {
    await page.evaluate(() => { document.querySelector(".log")!.scrollTop = 0; });
    await page.waitForTimeout(120);
  }
  assert.equal(await page.locator(".earlier").count(), 0, "all drawn");
  assert.equal(await drawn(page), 400);
  const users = await page.locator(".log > .msg.user").allTextContents();
  assert.deepEqual(users, messages(400).filter((m) => m.role === "user").map((m) => m.text), "every message, in order, once");
  await page.close();
});

test("a new conversation forgets the older messages of the last one", async () => {
  const { page, ev, send } = await panel("vscode");
  await ev(history(300));
  await send({ kind: "reset" });
  assert.equal(await page.locator(".earlier").count(), 0);
  await ev(history(200));
  assert.equal(await label(page), "↑ Show earlier messages (120 not shown)");
  await page.close();
});

test("drawing older messages is not announced to a screen reader, and the log goes back to its state", async () => {
  const { page, ev } = await panel("vscode");
  await ev(history(300));
  await page.evaluate(() => {
    const seen: (string | null)[] = [];
    (window as unknown as { seen: typeof seen }).seen = seen;
    const log = document.querySelector(".log")!;
    new MutationObserver(() => seen.push(log.getAttribute("aria-busy"))).observe(log, { attributes: true, attributeFilter: ["aria-busy"] });
  });
  await page.click(".earlier");
  await page.waitForFunction(() => (window as unknown as { seen: unknown[] }).seen.includes("false"));
  const seen = await page.evaluate(() => (window as unknown as { seen: (string | null)[] }).seen);
  assert.deepEqual(seen, ["true", "false"], "quiet while it draws, then as before");
  await page.close();
});

test("2,000 messages open in well under a second or two, not three seconds", async () => {
  const { page, ev } = await panel("vscode");
  const t0 = Date.now();
  await ev(history(2000));
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  const took = Date.now() - t0;
  assert.ok(took < 1500, `took ${took} ms`);
  assert.equal(await drawn(page), 80);
  await page.close();
});
