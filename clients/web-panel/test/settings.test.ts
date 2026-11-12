// The settings page in a real browser: it draws what the engine lists, says which control was changed
// as the message `/settings set <key> <value>`, tries the gateway, and never asks for an API key.

import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import type { AgentEvent } from "../src/protocol";
import { FLAVOURS, panel, startBrowser, stopBrowser } from "./harness";

before(startBrowser);
after(stopBrowser);

type State = Extract<AgentEvent, { type: "settings_state" }>;
type Field = State["fields"][number];

const field = (over: Partial<Field> & Pick<Field, "key" | "label" | "group" | "kind">): Field => ({
  help: "",
  value: null,
  choices: [],
  locked: false,
  note: "",
  ...over,
});

export const PAGE: State = {
  type: "settings_state",
  fields: [
    field({ key: "model", label: "Main model", group: "Models", kind: "text", value: "corp:qwen3-27b", help: "The model that does the work." }),
    field({ key: "fallbackModel", label: "Fallback model", group: "Models", kind: "text" }),
    field({ key: "permissions.defaultMode", label: "Permission mode", group: "Behaviour", kind: "choice", value: "default", choices: ["default", "acceptEdits", "plan"] }),
    field({ key: "maxTurns", label: "Model calls per request", group: "Behaviour", kind: "number", value: 50 }),
    field({ key: "critic.enabled", label: "Critique every answer", group: "Behaviour", kind: "bool", value: false }),
    field({ key: "verify.commands", label: "Check commands", group: "Checks", kind: "lines", value: ["npm test", "npm run build"] }),
    field({ key: "audit.enabled", label: "Audit log", group: "Records", kind: "bool", value: true, locked: true, note: "set by your organisation" }),
  ],
  gateways: [{ name: "corp", base_url: "https://gw.example/v1", key: "set" }],
  file: "/home/u/.cmcoder/settings.json",
  notice: "",
  models: [],
};

async function open(flavour: (typeof FLAVOURS)[number], state: State = PAGE) {
  const p = await panel(flavour);
  await p.page.click(".settings-open");
  await p.ev(state);
  return p;
}

for (const flavour of FLAVOURS) {
  test(`${flavour}: the Settings button opens the page in place of the conversation and asks the engine for it`, async () => {
    const { page, ev, sent } = await panel(flavour);
    await page.click(".settings-open");
    assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/settings", includeContext: false });
    assert.equal(await page.locator(".log-wrap").isVisible(), false);
    assert.equal(await page.locator(".settings-view").isVisible(), true);
    await ev(PAGE);
    assert.deepEqual(await page.locator(".settings-heading").allTextContents(), ["Gateway", "Models", "Behaviour", "Checks", "Records"]);
    assert.equal(await page.inputValue("#setting-0"), "corp:qwen3-27b");
    assert.equal(await page.inputValue("#setting-2"), "default");
    assert.equal(await page.inputValue("#setting-3"), "50");
    assert.equal(await page.isChecked("#setting-4"), false);
    assert.equal(await page.inputValue("#setting-5"), "npm test\nnpm run build");
    assert.equal(await page.locator(".settings-label", { hasText: "Main model" }).getAttribute("for"), "setting-0");
    assert.match((await page.getAttribute("#setting-0", "aria-describedby")) ?? "", /setting-0-help/);
    assert.equal(await page.textContent(".gw-key"), "✓ API key found");
    await page.click(".settings-back");
    assert.equal(await page.locator(".log-wrap").isVisible(), true);
    assert.equal(await page.locator(".settings-view").isVisible(), false);
    await page.close();
  });

  test(`${flavour}: each control says what changed as /settings set <key> <value>`, async () => {
    const { page, sent } = await open(flavour);
    const last = async () => (await sent()).at(-1);
    await page.fill("#setting-1", "corp:qwen3-7b");
    await page.press("#setting-1", "Tab");
    assert.deepEqual(await last(), { kind: "send", text: "/settings set fallbackModel corp:qwen3-7b", includeContext: false });
    await page.fill("#setting-0", ""); // emptied: back to the default
    await page.press("#setting-0", "Tab");
    assert.deepEqual(await last(), { kind: "send", text: "/settings set model null", includeContext: false });
    await page.selectOption("#setting-2", "acceptEdits");
    assert.deepEqual(await last(), { kind: "send", text: "/settings set permissions.defaultMode acceptEdits", includeContext: false });
    await page.fill("#setting-3", "75");
    await page.press("#setting-3", "Tab");
    assert.deepEqual(await last(), { kind: "send", text: "/settings set maxTurns 75", includeContext: false });
    await page.check("#setting-4");
    assert.deepEqual(await last(), { kind: "send", text: "/settings set critic.enabled true", includeContext: false });
    await page.fill("#setting-5", "pytest -q\n\n  ruff check .  ");
    await page.press("#setting-5", "Tab");
    assert.deepEqual(await last(), { kind: "send", text: '/settings set verify.commands ["pytest -q","ruff check ."]', includeContext: false });
    await page.close();
  });
}

test("what the organisation fixes is shown and cannot be changed", async () => {
  const { page } = await open("vscode");
  assert.equal(await page.isDisabled("#setting-6"), true);
  assert.match((await page.textContent("#setting-6-help")) ?? "", /set by your organisation/);
  await page.close();
});

test("the engine's answer is shown, and the page is drawn again with the new values", async () => {
  const { page, ev } = await open("vscode");
  await ev({ ...PAGE, notice: "Saved Main model: corp:qwen3-7b. In use now.", fields: PAGE.fields.map((f) => (f.key === "model" ? { ...f, value: "corp:qwen3-7b" } : f)) });
  assert.equal(await page.textContent(".settings-notice"), "Saved Main model: corp:qwen3-7b. In use now.");
  assert.equal(await page.inputValue("#setting-0"), "corp:qwen3-7b");
  await page.close();
});

test("a connection test says it is running, then how it went, and fills the model suggestions", async () => {
  const { page, ev, sent } = await open("vscode");
  await page.click(".settings-test");
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/settings test", includeContext: false });
  assert.equal(await page.textContent(".settings-test"), "Testing…");
  assert.equal(await page.isDisabled(".settings-test"), true);
  await ev({ type: "connection_tested", ok: true, gateway: "corp", message: "2 models available", elapsed_ms: 143 });
  await ev({ ...PAGE, models: ["qwen3-27b", "qwen3-7b"], notice: "Connected to corp." });
  assert.equal(await page.textContent(".settings-tested"), "✓ 2 models available · 143 ms");
  assert.equal(await page.textContent(".settings-test"), "Test connection");
  assert.deepEqual(await page.locator("#settings-models option").evaluateAll((o) => o.map((x) => (x as HTMLOptionElement).value)), ["qwen3-27b", "qwen3-7b"]);
  assert.equal(await page.getAttribute("#setting-0", "list"), "settings-models");
  await ev({ type: "connection_tested", ok: false, gateway: "corp", message: "The gateway refused the key: run `cmcoder login` in a terminal.", elapsed_ms: 20 });
  assert.match((await page.textContent(".settings-tested")) ?? "", /^✗ The gateway refused the key/);
  await page.close();
});

test("a missing key is said with the fix, and the page has no place to type one", async () => {
  const { page } = await open("vscode", { ...PAGE, gateways: [{ name: "corp", base_url: "https://gw.example/v1", key: "missing" }, { name: "local", base_url: "http://localhost:11434/v1", key: "not needed" }] });
  const keys = await page.locator(".gw-key").allTextContents();
  assert.deepEqual(keys, ["✗ no API key: run cmcoder login in a terminal", "✓ no key needed"]);
  assert.equal(await page.locator('input[type="password"], [data-key*="key" i], [data-key*="token" i]').count(), 0);
  await page.close();
});

test("while a request runs the settings wait, and they say why", async () => {
  const { page, ev } = await open("vscode");
  await page.click(".settings-back");
  await page.fill("footer textarea", "go");
  await page.press("footer textarea", "Enter");
  await page.click(".settings-open");
  await ev(PAGE);
  assert.equal(await page.isDisabled("#setting-0"), true);
  assert.match((await page.textContent(".settings-notice")) ?? "", /between requests/);
  await page.close();
});

test("typing /settings opens the page without sending a message to the model", async () => {
  const { page, sent } = await panel("vscode");
  await page.fill("footer textarea", "/settings");
  await page.press("footer textarea", "Enter");
  assert.equal(await page.locator(".msg.user").count(), 0, "no chat bubble for it");
  assert.equal(await page.locator(".settings-view").isVisible(), true);
  assert.deepEqual((await sent()).at(-1), { kind: "send", text: "/settings", includeContext: false });
  await page.close();
});
