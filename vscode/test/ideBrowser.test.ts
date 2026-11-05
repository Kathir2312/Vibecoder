// VS Code's built-in browser tools passed on to cmcoder (src/ideBrowser.ts): found by name at run
// time, run through vscode.lm.invokeTool, their text and pictures sent back.

import assert from "node:assert/strict";
import { test } from "node:test";
import { browserTools, describeTools, fromParts, isBrowserTool, runBrowserTool, type LmLike } from "../src/ideBrowser";

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3]);

function lmWith(names: string[], invoke?: LmLike["invokeTool"]): LmLike {
  return {
    tools: names.map((name) => ({
      name,
      description: `${name}: does a thing.\nSecond line.`,
      inputSchema: { type: "object", properties: { url: { type: "string" } } },
    })),
    invokeTool: invoke ?? (async () => ({ content: [] })),
  };
}

test("VS Code's browser tools are recognised by name, however they are spelt", () => {
  for (const name of ["open_browser_page", "openBrowserPage", "click_element", "type_in_page", "run_playwright_code", "integratedBrowser_open"]) {
    assert.equal(isBrowserTool(name), true, name);
  }
  for (const name of ["copilot_findFiles", "mcp_github_search", "cmcoder_browser_page", "runInTerminal"]) {
    assert.equal(isBrowserTool(name), false, name);
  }
});

test("only the browser tools are passed on, with their schemas as they are", () => {
  const lm = lmWith(["copilot_findFiles", "open_browser_page", "read_page", "runInTerminal"]);
  const defs = browserTools(lm);
  assert.deepEqual(defs.map((d) => d.name), ["open_browser_page", "read_page"]);
  assert.deepEqual(defs[0].input_schema, { type: "object", properties: { url: { type: "string" } } });
  // The user's own list wins when the names are ones we don't know.
  assert.deepEqual(browserTools(lm, ["runInTerminal", "nothing"]).map((d) => d.name), ["runInTerminal"]);
  // An older VS Code, or one whose API refuses: nothing, not a crash.
  assert.deepEqual(browserTools(undefined), []);
  const refusing = { get tools(): never { throw new Error("no"); }, invokeTool: async () => ({ content: [] }) } as unknown as LmLike;
  assert.deepEqual(browserTools(refusing), []);
});

test("a tool result's text and pictures", () => {
  const parts = [
    { value: "Opened tab 1." },
    { data: PNG, mimeType: "image/png" },
    { data: new Uint8Array([1]), mimeType: "application/zip" }, // not for the chat
    { data: new TextEncoder().encode('{"ok":true}'), mimeType: "application/json" },
    { value: { title: "App", status: 200 } },
    null,
    "stray",
  ];
  const { content, images } = fromParts(parts);
  assert.equal(content, 'Opened tab 1.\n{"ok":true}\n{"title":"App","status":200}');
  assert.deepEqual(images, [`data:image/png;base64,${Buffer.from(PNG).toString("base64")}`]);
});

test("running a tool: its result, an empty result, a failure", async () => {
  const seen: Array<[string, object]> = [];
  const lm = lmWith(["open_browser_page"], async (name, options) => {
    seen.push([name, options.input]);
    return { content: [{ value: "Opened." }, { data: PNG, mimeType: "image/png" }] };
  });
  const ok = await runBrowserTool(lm, "open_browser_page", { url: "http://localhost:3000" });
  assert.deepEqual(seen, [["open_browser_page", { url: "http://localhost:3000" }]]);
  assert.equal(ok.is_error, false);
  assert.equal(ok.content, "Opened.");
  assert.equal(ok.images.length, 1);

  const empty = await runBrowserTool(lmWith(["x"]), "x", {});
  assert.deepEqual(empty, { content: "Done.", is_error: false, images: [] });

  const failing = lmWith(["x"], async () => {
    throw new Error("The page is gone");
  });
  const bad = await runBrowserTool(failing, "x", {});
  assert.equal(bad.is_error, true);
  assert.match(bad.content, /x failed: The page is gone/);
  assert.equal((await runBrowserTool(undefined, "x", {})).is_error, true);
});

test("the command's text says what this VS Code offers, or what to try", () => {
  const some = describeTools(lmWith(["copilot_findFiles", "open_browser_page"]));
  assert.match(some, /offers 2 language-model tools; cmcoder passes on 1/);
  assert.match(some, /✓ open_browser_page\(url\): open_browser_page: does a thing\./);
  const none = describeTools(lmWith(["copilot_findFiles", "runInTerminal"]));
  assert.match(none, /None looks like a browser tool/);
  assert.match(none, /cmcoder\.integratedBrowserTools/);
  assert.match(none, /· copilot_findFiles/); // every name, to choose from
  assert.match(describeTools(undefined), /no language-model tool API/);
});
