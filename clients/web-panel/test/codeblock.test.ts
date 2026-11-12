// Code in replies, in a real browser: coloured by language, with Copy and (where the IDE can) Insert.

import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import type { Page } from "playwright";
import type { AgentEvent } from "../src/protocol";
import { panel, startBrowser, stopBrowser, themed } from "./harness";

before(startBrowser);
after(stopBrowser);

const CS = 'public async Task<IActionResult> Index(int page = 1)\n{\n    return View("x"); // done\n}';
const reply = (text: string): AgentEvent => ({ type: "assistant_message", text, reasoning: "", tool_calls: [], model: null });
const fenced = (lang: string, code: string): string => `Here:\n\n\`\`\`${lang}\n${code}\n\`\`\`\n\nDone.`;

async function clipboard(page: Page): Promise<void> {
  await page.evaluate(() => {
    (window as unknown as { copied: string[] }).copied = [];
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: (t: string) => { (window as unknown as { copied: string[] }).copied.push(t); return Promise.resolve(); } },
    });
  });
}

test("a fenced block gets a header with its language and is coloured, and its text is untouched", async () => {
  const { page, ev } = await panel("vscode");
  await ev(reply(fenced("csharp", CS)));
  assert.equal(await page.locator(".msg.assistant .codeblock").count(), 1);
  assert.equal(await page.textContent(".codeblock .codelang"), "C#");
  assert.equal(await page.textContent(".codeblock pre code"), CS);
  const keywords = await page.locator(".codeblock .tk-keyword").allTextContents();
  assert.ok(keywords.includes("public") && keywords.includes("async") && keywords.includes("return"), keywords.join(","));
  assert.ok((await page.locator(".codeblock .tk-comment").allTextContents()).includes("// done"));
  assert.ok((await page.locator(".codeblock .tk-type").allTextContents()).includes("IActionResult"));
  await page.close();
});

test("Copy puts the code on the clipboard and says so", async () => {
  const { page, ev } = await panel("vscode");
  await clipboard(page);
  await ev(reply(fenced("csharp", CS)));
  await page.click('.codeblock button:has-text("Copy")');
  assert.deepEqual(await page.evaluate(() => (window as unknown as { copied: string[] }).copied), [CS]);
  assert.equal(await page.textContent(".codeblock .codebtn"), "Copied");
  assert.match((await page.getAttribute(".codeblock .codebtn", "aria-label")) ?? "", /Copy the C# code/);
  await page.close();
});

test("Insert is there only where the IDE can do it, and sends the code", async () => {
  const without = await panel("vscode"); // an IDE that does not say "insertCode"
  await without.ev(reply(fenced("ts", "let a = 1;")));
  assert.equal(await without.page.locator(".codeblock button", { hasText: "Insert" }).count(), 0);
  await without.page.close();

  const { page, ev, sent } = await panel("vscode", undefined, true, "stopTool sessionRules insertCode");
  await ev(reply(fenced("ts", "let a = 1;")));
  await page.click('.codeblock button:has-text("Insert")');
  assert.deepEqual((await sent()).at(-1), { kind: "insertCode", text: "let a = 1;" });
  await page.close();
});

test("a language it does not know, or none, still gets the header and Copy but no colours", async () => {
  const { page, ev } = await panel("vscode");
  await ev(reply(fenced("brainfuck", "+++[>++<-]")));
  await ev(reply("```\nplain text\n```"));
  assert.deepEqual(await page.locator(".codeblock .codelang").allTextContents(), ["brainfuck", "code"]);
  assert.equal(await page.locator(".codeblock [class^=tk-]").count(), 0);
  assert.equal(await page.locator(".codeblock button", { hasText: "Copy" }).count(), 2);
  await page.close();
});

test("code in a reply is shown as text, never run or rendered", async () => {
  const { page, ev } = await panel("vscode");
  const evil = '<img src=x onerror="window.pwned=1"><script>window.pwned=2</script><a href="javascript:alert(1)">x</a>';
  await ev(reply(fenced("html", evil)));
  assert.equal(await page.locator(".codeblock img, .codeblock script, .codeblock a").count(), 0);
  assert.equal(await page.textContent(".codeblock pre code"), evil);
  assert.ok((await page.locator(".codeblock .tk-tag").count()) > 0, "coloured as markup, still text");
  assert.equal(await page.evaluate(() => (window as unknown as { pwned?: number }).pwned), undefined);
  await page.close();
});

test("streaming a block piece by piece, then the whole reply, leaves one block", async () => {
  const { page, ev } = await panel("vscode");
  await page.fill("textarea", "go");
  await page.press("textarea", "Enter");
  await ev({ type: "assistant_delta", text: "Here:\n\n```cs\nvar x" });
  await page.waitForSelector(".codeblock"); // streamed text is drawn on the next frame
  assert.equal(await page.locator(".codeblock").count(), 1, "an unfinished block is already one");
  await ev({ type: "assistant_delta", text: " = 1;\n```\n\nDone." });
  await ev(reply("Here:\n\n```cs\nvar x = 1;\n```\n\nDone."));
  assert.equal(await page.locator(".codeblock").count(), 1);
  assert.equal(await page.textContent(".codeblock pre code"), "var x = 1;");
  assert.equal(await page.locator(".codeblock .codeblock").count(), 0);
  await page.close();
});

test("a pasted file too long to be a snippet is not coloured, but is still a block with Copy", async () => {
  const { page, ev } = await panel("vscode");
  await ev(reply(fenced("csharp", "var a = 1;\n".repeat(2500))));
  assert.equal(await page.locator(".codeblock").count(), 1);
  assert.equal(await page.locator(".codeblock [class^=tk-]").count(), 0);
  await page.close();
});

test("blocks in an earlier conversation get the same treatment", async () => {
  const { page, ev } = await panel("vscode");
  await ev({ type: "history", messages: [{ role: "user", text: "show" }, { role: "assistant", text: fenced("python", "def f(self):\n    return None") }] });
  assert.equal(await page.locator(".codeblock .tk-keyword", { hasText: "def" }).count(), 1);
  await page.close();
});

test("the colours follow the IDE's theme, also when it changes", async () => {
  const { page, ev } = await panel("vscode");
  await themed(page, "dark");
  await ev(reply(fenced("csharp", "var x = 1;")));
  assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), "dark");
  const dark = await page.evaluate(() => getComputedStyle(document.querySelector(".tk-keyword")!).color);
  // The IDE sets its colour variables again: a light theme.
  await page.evaluate(() => document.documentElement.style.setProperty("--vscode-sideBar-background", "#ffffff"));
  await page.waitForFunction(() => document.documentElement.dataset.theme === "light");
  const light = await page.evaluate(() => getComputedStyle(document.querySelector(".tk-keyword")!).color);
  assert.notEqual(dark, light);
  await page.close();
});
