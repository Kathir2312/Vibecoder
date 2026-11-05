// The Tasks tab (dist/tasks.js) in a real browser, once per way an IDE connects: the todo list in,
// the grouped task list and messages to the extension out. Needs `npm run build` first.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { chromium, type Browser, type Page } from "playwright";
import type { FromTasks, ToTasks } from "../src/messages";

const root = join(__dirname, "..");
let browser: Browser;

before(async () => {
  browser = await chromium.launch();
});

after(async () => {
  await browser?.close();
});

const FLAVOURS = ["vscode", "webview2", "function"] as const;
type Flavour = (typeof FLAVOURS)[number];
const HOSTS: Record<Flavour, string> = {
  vscode: "window.sent=[];function acquireVsCodeApi(){return{postMessage:(m)=>window.sent.push(m)}}",
  webview2: "window.sent=[];window.chrome={webview:{postMessage:(m)=>window.sent.push(m)}}",
  function: "window.sent=[];setTimeout(()=>{window.cmcoderHostPost=(j)=>window.sent.push(JSON.parse(j))},150)",
};

async function tasks(flavour: Flavour): Promise<{ page: Page; send: (m: ToTasks) => Promise<void>; sent: () => Promise<FromTasks[]> }> {
  const page = await browser.newPage({ viewport: { width: 1000, height: 700 } });
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.setContent(`<!DOCTYPE html><html><head><style>${readFileSync(join(root, "dist", "tasks.css"), "utf8")}</style>
    <script>${HOSTS[flavour]}</script>
    </head><body><div id="app"></div></body></html>`);
  await page.addScriptTag({ content: readFileSync(join(root, "dist", "tasks.js"), "utf8") });
  if (flavour === "function") await page.waitForFunction(() => (window as unknown as { sent: unknown[] }).sent.length > 0);
  const send = (m: ToTasks) =>
    page.evaluate(
      (m) =>
        new Promise<void>((resolve) => {
          const done = (e: MessageEvent) => {
            if ((e.data as { testFlush?: boolean })?.testFlush) {
              window.removeEventListener("message", done);
              resolve();
            }
          };
          window.addEventListener("message", done);
          window.postMessage(m, "*");
          window.postMessage({ testFlush: true }, "*");
        }),
      m,
    );
  const sent = () => page.evaluate(() => (window as unknown as { sent: FromTasks[] }).sent);
  page.on("close", () => assert.deepEqual(errors, []));
  return { page, send, sent };
}

const list = (done: number, active: number, pending: number) => [
  ...Array.from({ length: done }, (_, i) => ({ content: `Done task ${i + 1}`, status: "completed" })),
  ...Array.from({ length: active }, (_, i) => ({ content: `Active task ${i + 1}`, status: "in_progress" })),
  ...Array.from({ length: pending }, (_, i) => ({ content: `Pending task ${i + 1}`, status: "pending" })),
];

for (const flavour of FLAVOURS) {
  test(`${flavour}: tasks says ready, and says what to expect while there are none`, async () => {
    const { page, sent } = await tasks(flavour);
    assert.deepEqual((await sent())[0], { kind: "ready" });
    assert.match((await page.textContent("main")) ?? "", /No tasks yet/);
    await page.close();
  });

  test(`${flavour}: the whole list, numbered and grouped, with a meter`, async () => {
    const { page, send } = await tasks(flavour);
    await send({ kind: "todos", todos: list(3, 1, 16) });
    assert.equal(await page.textContent(".count"), "3 of 20 done");
    assert.equal(await page.locator(".fill").evaluate((e) => (e as HTMLElement).style.width), "15%");
    assert.deepEqual(await page.locator(".group h2").allTextContents(), ["In progress · 1", "To do · 16"]);
    assert.equal(await page.locator(".task").count(), 3 + 1 + 16, "every task, even the done ones (folded)");
    assert.equal(await page.locator("details.done").getAttribute("open"), null, "done tasks fold away while there is more to do");
    assert.equal(await page.textContent(".task.in_progress"), "4.►Active task 1");
    assert.equal(await page.textContent(".task.pending .no"), "5.");
    await page.click("details.done summary");
    assert.equal(await page.locator("details.done").getAttribute("open"), "");
    // The agent goes on: the list follows, and the user's choice for Done stays.
    await send({ kind: "todos", todos: list(4, 1, 15) });
    assert.equal(await page.textContent(".count"), "4 of 20 done");
    assert.equal(await page.locator("details.done").getAttribute("open"), "");
    await page.close();
  });

  test(`${flavour}: a long list can be filtered; a short one has no filter`, async () => {
    const { page, send } = await tasks(flavour);
    await send({ kind: "todos", todos: list(0, 1, 3) });
    assert.equal(await page.isVisible(".filter"), false);
    await send({ kind: "todos", todos: list(2, 1, 17) });
    assert.equal(await page.isVisible(".filter"), true);
    await page.fill(".filter", "pending task 1");
    // "Pending task 1", "…10" to "…17": the filter matches text, not only the whole word.
    assert.equal(await page.locator(".task.pending").count(), 9);
    // A number picks that task (no. 7) and, as text, "Pending task 7" (no. 10) and "…17" (no. 20).
    await page.fill(".filter", "7");
    assert.deepEqual(await page.locator(".task .no").allTextContents(), ["7.", "10.", "20."]);
    await page.fill(".filter", "no such thing");
    assert.match((await page.textContent("main")) ?? "", /No task matches/);
    await page.close();
  });

  test(`${flavour}: everything done opens the Done group; a new conversation clears; Chat goes back`, async () => {
    const { page, send, sent } = await tasks(flavour);
    await send({ kind: "todos", todos: list(3, 0, 0) });
    assert.equal(await page.locator("details.done").getAttribute("open"), "");
    assert.equal(await page.textContent(".count"), "3 of 3 done");
    await send({ kind: "reset" });
    assert.match((await page.textContent("main")) ?? "", /No tasks yet/);
    assert.equal(await page.textContent(".count"), "");
    await page.click(".back");
    assert.deepEqual((await sent()).at(-1), { kind: "openChat" });
    await page.close();
  });

  test(`${flavour}: task text is never run as HTML`, async () => {
    const { page, send } = await tasks(flavour);
    await send({ kind: "todos", todos: [{ content: "<img src=x onerror=alert(1)>", status: "pending" }] });
    assert.equal(await page.locator("main img").count(), 0);
    assert.match((await page.textContent(".task .text")) ?? "", /<img src=x/);
    await page.close();
  });
}
