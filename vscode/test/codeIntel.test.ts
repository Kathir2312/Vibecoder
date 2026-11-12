// Code intelligence (src/codeIntel.ts): the questions cmcoder asks VS Code's language servers, run
// against a stand-in editor. VS Code itself is not needed to check what is decided here: which
// column a name is at, how locations and links are read, and that a failing server is an answer.

import assert from "node:assert/strict";
import { test } from "node:test";
import { CODE_TOOLS, columnOf, isCodeTool, runCodeTool, type CodeApi, type DocumentLike } from "../src/codeIntel";

const doc = (lines: string[]): DocumentLike => ({ lineCount: lines.length, lineAt: (n) => ({ text: lines[n] }) });

interface Call {
  name: string;
  args: unknown[];
}

function editor(files: Record<string, string[]>, answer: unknown, calls: Call[] = []): CodeApi {
  return {
    command: async (name, ...args) => {
      calls.push({ name, args });
      if (answer instanceof Error) throw answer;
      return answer;
    },
    file: (path) => ({ fsPath: path }),
    position: (line, character) => ({ line, character }),
    document: async (uri) => (files[uri.fsPath] ? doc(files[uri.fsPath]) : undefined),
    kindName: (kind) => ["File", "Module", "Namespace", "Package", "Class", "Method"][kind] ?? "Symbol",
  };
}

const results = (a: { content: string }) => JSON.parse(a.content).results;

test("the three tools are the ones cmcoder knows by name", () => {
  assert.deepEqual([...CODE_TOOLS], ["goToDefinition", "findReferences", "workspaceSymbols"]);
  assert.equal(isCodeTool("findReferences"), true);
  assert.equal(isCodeTool("getDiagnostics"), false);
});

test("a name is found as a whole word, never inside another", () => {
  assert.equal(columnOf("x = total(a) + subtotal(b)", "total"), 4);
  assert.equal(columnOf("x = subtotal(b)", "total"), -1);
  assert.equal(columnOf("$total + total_2 + total", "total"), 19);
  assert.equal(columnOf("a.b.c(d)", "b.c"), 2);
  assert.equal(columnOf("price (usd)", "(usd)"), 6, "a name with regex characters is taken literally");
});

test("a definition is asked at the name's own column, and the answer has the line it points at", async () => {
  const calls: Call[] = [];
  const api = editor(
    { "/p/app.py": ["import orders", "print(orders.total())"], "/p/orders.py": ["", "class Order:", "    def total(self):"] },
    [{ uri: { fsPath: "/p/orders.py" }, range: { start: { line: 2, character: 8 } } }],
    calls,
  );
  const answer = await runCodeTool(api, "goToDefinition", { file_path: "/p/app.py", line: 2, symbol: "total" });
  assert.equal(answer.is_error, false);
  assert.deepEqual(calls, [{ name: "vscode.executeDefinitionProvider", args: [{ fsPath: "/p/app.py" }, { line: 1, character: 13 }] }]);
  assert.deepEqual(results(answer), [{ path: "/p/orders.py", line: 3, column: 9, text: "    def total(self):" }]);
});

test("a provider may answer with links: their selection range is the place", async () => {
  const api = editor({ "/p/a.ts": ["use(thing)"], "/p/b.ts": ["// c", "export function thing() {}"] }, [
    {
      targetUri: { fsPath: "/p/b.ts" },
      targetRange: { start: { line: 1, character: 0 } },
      targetSelectionRange: { start: { line: 1, character: 16 } },
    },
  ]);
  const answer = await runCodeTool(api, "goToDefinition", { file_path: "/p/a.ts", line: 1, symbol: "thing" });
  assert.deepEqual(results(answer), [{ path: "/p/b.ts", line: 2, column: 17, text: "export function thing() {}" }]);
});

test("references use the references provider, and a file that cannot be opened is a place without its text", async () => {
  const calls: Call[] = [];
  const api = editor({ "/p/a.py": ["total()"] }, [
    { uri: { fsPath: "/p/a.py" }, range: { start: { line: 0, character: 0 } } },
    { uri: { fsPath: "/p/gone.py" }, range: { start: { line: 4, character: 2 } } },
  ], calls);
  const answer = await runCodeTool(api, "findReferences", { file_path: "/p/a.py", line: 1, symbol: "total" });
  assert.equal(calls[0].name, "vscode.executeReferenceProvider");
  assert.deepEqual(results(answer), [
    { path: "/p/a.py", line: 1, column: 1, text: "total()" },
    { path: "/p/gone.py", line: 5, column: 3, text: "" },
  ]);
});

test("a wrong line or name is answered with what is really there, so the model can correct itself", async () => {
  const api = editor({ "/p/a.py": ["x = 1", "y = 2"] }, []);
  const noName = await runCodeTool(api, "goToDefinition", { file_path: "/p/a.py", line: 2, symbol: "total" });
  assert.equal(noName.is_error, true);
  assert.equal(noName.content, "Line 2 of /p/a.py does not contain 'total': y = 2");
  const far = await runCodeTool(api, "findReferences", { file_path: "/p/a.py", line: 9, symbol: "y" });
  assert.deepEqual([far.is_error, far.content], [true, "/p/a.py has only 2 lines."]);
  const missing = await runCodeTool(api, "findReferences", { file_path: "/p/none.py", line: 1, symbol: "y" });
  assert.deepEqual([missing.is_error, missing.content], [true, "Could not open /p/none.py."]);
  const bare = await runCodeTool(api, "goToDefinition", { file_path: "/p/a.py" });
  assert.equal(bare.is_error, true);
});

test("a language server that has nothing, or fails, is an answer and not a crash", async () => {
  const none = await runCodeTool(editor({ "/p/a.py": ["x"] }, undefined), "goToDefinition", { file_path: "/p/a.py", line: 1, symbol: "x" });
  assert.deepEqual(results(none), []);
  const broken = await runCodeTool(editor({ "/p/a.py": ["x"] }, new Error("server crashed")), "findReferences", { file_path: "/p/a.py", line: 1, symbol: "x" });
  assert.deepEqual([broken.is_error, broken.content], [true, "The language server failed: server crashed"]);
});

test("workspace symbols carry their kind, container and place, and a long list is cut", async () => {
  const calls: Call[] = [];
  const many = Array.from({ length: 150 }, (_, n) => ({
    name: `S${n}`,
    kind: 4,
    containerName: n === 0 ? "Orders" : undefined,
    location: { uri: { fsPath: "/p/m.py" }, range: { start: { line: n, character: 0 } } },
  }));
  const answer = await runCodeTool(editor({}, many, calls), "workspaceSymbols", { query: "S" });
  assert.deepEqual(calls, [{ name: "vscode.executeWorkspaceSymbolProvider", args: ["S"] }]);
  const items = results(answer);
  assert.equal(items.length, 100);
  assert.deepEqual(items[0], { name: "S0", kind: "Class", container: "Orders", path: "/p/m.py", line: 1 });
  assert.equal(items[1].container, "");
  const empty = await runCodeTool(editor({}, []), "workspaceSymbols", {});
  assert.deepEqual([empty.is_error, empty.content], [true, "query is required."]);
});
