// The Jobs panel of the dock: background jobs the agent started, with how they stand, their last
// lines and a Stop that is only a message (`/jobs stop <n>`), so every IDE gets it without changes.

import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import type { AgentEvent } from "../src/protocol";
import { FLAVOURS, panel, startBrowser, stopBrowser } from "./harness";

before(startBrowser);
after(stopBrowser);

type Jobs = Extract<AgentEvent, { type: "jobs_changed" }>;
type Job = Jobs["jobs"][number];

const job = (over: Partial<Job> & Pick<Job, "id" | "command">): Job => ({
  state: "running",
  exit_code: null,
  elapsed_ms: 65_000,
  tail: "",
  outside_sandbox: false,
  note: "",
  ...over,
});
const jobs = (...list: Job[]): Jobs => ({ type: "jobs_changed", jobs: list });

for (const flavour of FLAVOURS) {
  test(`${flavour}: a job started in the background shows in the dock, open, with its last lines and a Stop`, async () => {
    const { page, ev } = await panel(flavour);
    assert.equal(await page.locator('.dock-panel[data-panel="jobs"]').isVisible(), false, "nothing to show yet");
    await ev(jobs(job({ id: 1, command: "npm run dev", tail: "ready on http://localhost:3000" })));
    const dock = page.locator('.dock-panel[data-panel="jobs"]');
    assert.equal(await dock.isVisible(), true);
    assert.equal(await dock.locator(".dock-count").textContent(), "1 running");
    assert.match((await dock.locator(".dock-now").textContent()) ?? "", /npm run dev/);
    assert.equal(await dock.locator(".job-cmd").textContent(), "npm run dev");
    assert.equal(await dock.locator(".job-detail").textContent(), "running · 1m05s");
    assert.equal(await dock.locator(".job-tail").textContent(), "ready on http://localhost:3000");
    assert.equal(await dock.locator(".stop-job").getAttribute("aria-label"), "Stop job 1: npm run dev");
    assert.equal(await dock.getAttribute("class").then((c) => c?.includes("open")), true, "it opens when a job starts");
    await page.close();
  });

  test(`${flavour}: Stop is the message /jobs stop <n>, and it works while a request is running`, async () => {
    const { page, ev, sent } = await panel(flavour);
    await page.fill("textarea", "start the server");
    await page.press("textarea", "Enter"); // a request is now running
    await ev(jobs(job({ id: 2, command: "npm run dev" })));
    await page.click(".stop-job");
    assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/jobs stop 2", includeContext: false });
    assert.equal(await page.locator(".stop-job").isDisabled(), true, "a second click does nothing");
    await ev(jobs(job({ id: 2, command: "npm run dev", state: "stopped", elapsed_ms: 70_000 })));
    assert.equal(await page.locator(".job-detail").textContent(), "stopped · 1m10s");
    assert.equal(await page.locator(".stop-job").isVisible(), false, "nothing left to stop");
    assert.equal(await page.locator(".dock-count").last().textContent(), "0 running · 1 finished");
    await page.close();
  });

  test(`${flavour}: a job that ended says how, several running can be stopped at once, and rows are updated in place`, async () => {
    const { page, ev, sent } = await panel(flavour);
    await ev(jobs(job({ id: 1, command: "dotnet watch test" }), job({ id: 2, command: "npm run dev" })));
    assert.equal(await page.locator(".stop-all").isVisible(), true);
    const row = await page.locator('.job[data-job="1"]').elementHandle();
    await ev(jobs(job({ id: 1, command: "dotnet watch test", tail: "Passed: 12" }), job({ id: 2, command: "npm run dev" })));
    assert.equal(await row?.evaluate((e) => e.isConnected), true, "the row is the same element, not redrawn");
    await page.click(".stop-all");
    assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/jobs stop all", includeContext: false });

    await ev(
      jobs(
        job({ id: 1, command: "dotnet watch test", state: "exited", exit_code: 0, elapsed_ms: 3000 }),
        job({ id: 2, command: "npm run dev", state: "exited", exit_code: 3, elapsed_ms: 2000, tail: "Error: port in use" }),
        job({ id: 3, command: "make serve", state: "failed", note: "the shell ended", elapsed_ms: 1000, outside_sandbox: true }),
      ),
    );
    assert.equal(await page.locator('.job[data-job="1"] .job-detail').textContent(), "done · 3s");
    assert.equal(await page.locator('.job[data-job="2"] .job-detail').textContent(), "exit 3 · 2s");
    assert.equal(await page.locator('.job[data-job="3"] .job-detail').textContent(), "failed · 1s · the shell ended · outside the sandbox");
    // The mark says the same as the words: done is a tick, a failing exit code or a failure is a cross.
    assert.deepEqual(await page.locator(".job-mark").allTextContents(), ["✓", "✗", "✗"]);
    assert.equal(await page.locator('.job[data-job="2"]').getAttribute("class"), "job exited bad");
    assert.equal(await page.locator(".stop-all").isVisible(), false);
    await page.close();
  });
}

test("a job's command and output are text, never markup", async () => {
  const { page, ev } = await panel("vscode");
  await ev(jobs(job({ id: 1, command: "echo <img src=x onerror=alert(1)>", tail: "<b>bold</b> <script>alert(2)</script>" })));
  assert.equal(await page.locator(".job img, .job b, .job script").count(), 0);
  assert.match((await page.textContent(".job-cmd")) ?? "", /<img src=x/);
  await page.close();
});

test("the elapsed time of a running job keeps counting between two events", async () => {
  const { page, ev } = await panel("vscode");
  await ev(jobs(job({ id: 1, command: "sleep 99", elapsed_ms: 58_000 })));
  assert.equal(await page.textContent(".job-detail"), "running · 58s");
  await page.waitForFunction(() => /^running · 1m0[0-9]s$/.test(document.querySelector(".job-detail")?.textContent ?? ""), undefined, { timeout: 8000 });
  await page.close();
});
