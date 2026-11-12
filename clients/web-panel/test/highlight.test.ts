// The code highlighter (src/chat/highlight.ts): which words get which colour, and above all that the
// text itself never changes: whatever it is given, the spans join back into exactly the code.

import assert from "node:assert/strict";
import { test } from "node:test";
import { languageOf, MAX_HIGHLIGHT, tokenize } from "../src/chat/highlight";

const marked = (code: string, language: string): string[] =>
  (tokenize(code, language) ?? []).filter((s) => s.kind).map((s) => `${s.kind}:${s.text}`);
const joined = (code: string, language: string): string | undefined => tokenize(code, language)?.map((s) => s.text).join("");

test("C#: keywords, types, calls, numbers, strings and comments", () => {
  const got = marked('public async Task<IActionResult> Index(int page = 1) { return View("x"); } // list', "csharp");
  for (const want of ["keyword:public", "keyword:async", "type:Task", "type:IActionResult", "function:Index", "type:int", "number:1", "keyword:return", "function:View", 'string:"x"', "comment:// list"]) {
    assert.ok(got.includes(want), `${want} in ${got.join(" ")}`);
  }
  assert.ok(!got.some((k) => k.endsWith(":page")), "an ordinary name stays plain");
});

test("C#: verbatim and interpolated strings are one string each", () => {
  assert.deepEqual(marked('var p = @"C:\\temp\\a"; var s = $"Hi {name}";', "csharp").filter((k) => k.startsWith("string")), ['string:@"C:\\temp\\a"', 'string:$"Hi {name}"']);
});

test("Python: triple-quoted strings, hash comments, self and def", () => {
  const got = marked('def f(self):\n    """doc\n    more"""  # why\n    return None', "python");
  assert.ok(got.includes("keyword:def") && got.includes("function:f") && got.includes("literal:self") && got.includes("literal:None"));
  assert.ok(got.some((k) => k.startsWith('string:"""doc')) && got.includes("comment:# why"));
});

test("JSON: a key is not coloured like a value", () => {
  assert.deepEqual(marked('{"a": "b", "n": 1, "t": true}', "json"), ['attr:"a"', 'string:"b"', 'attr:"n"', "number:1", 'attr:"t"', "literal:true"]);
});

test("SQL keywords in any case, and comments", () => {
  const got = marked("select Name from Orders Where Id = 3 -- why", "sql");
  assert.ok(got.includes("keyword:select") && got.includes("keyword:from") && got.includes("keyword:Where") && got.includes("number:3") && got.includes("comment:-- why"));
});

test("HTML/XML: tags, attributes, strings, comments and Razor", () => {
  const got = marked('<div class="x">@Model.Name<!-- c --></div>', "markup");
  for (const want of ["tag:<div", 'attr:class', 'string:"x"', "meta:@Model.Name", "comment:<!-- c -->", "tag:</div>"]) assert.ok(got.includes(want), `${want} in ${got.join(" ")}`);
});

test("diff lines by their first character", () => {
  assert.deepEqual(marked("--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n same", "diff"), ["meta:--- a\n+++ b\n@@ -1 +1 @@\n", "del:-old\n", "add:+new\n"]);
});

test("shell: options, variables, strings and comments", () => {
  const got = marked('dotnet test --filter "Name=x" $HOME # go', "shell");
  assert.ok(got.includes("attr:--filter") && got.includes('string:"Name=x"') && got.includes("literal:$HOME") && got.includes("comment:# go"));
});

test("YAML keys and CSS properties", () => {
  assert.ok(marked("name: x # c\nok: true", "yaml").includes("attr:name") && marked("a { max-width: 12px; color: #fff }", "css").includes("attr:max-width"));
  assert.ok(marked("a { margin: 12px; color: #fff }", "css").includes("number:12px"));
});

test("fence words name a grammar, whatever the case or prefix; others have none", () => {
  assert.equal(languageOf("C#"), "csharp");
  assert.equal(languageOf("language-ts"), "javascript");
  assert.equal(languageOf("PowerShell"), "shell");
  assert.equal(languageOf("cshtml"), "markup");
  assert.equal(languageOf("brainfuck"), null);
  assert.equal(tokenize("x", "brainfuck"), null);
  assert.equal(tokenize("x", null), null);
});

test("code too long to be a snippet is left plain", () => {
  assert.equal(tokenize("a".repeat(MAX_HIGHLIGHT + 1), "csharp"), null);
  assert.notEqual(tokenize("a".repeat(MAX_HIGHLIGHT), "csharp"), null);
});

test("whatever the code, the spans join back into exactly the code", () => {
  const samples = [
    "", "\n", "   ", "unterminated 'string and \"another", "/* never closed", '"""never closed', "`template ${x} never closed",
    "<!-- never closed", "<div", "{\"a\":", "emoji 😀 and tab\tand \\ backslash and \u0000 nul", "a--b ---- c", "$ ${ $( $(", "@\"verbatim never closed",
    "line1\r\nline2\r\n", "x = 'it''s'", "<?xml version=\"1.0\"?>", "#!/bin/bash\n# comment\nfi",
  ];
  for (const language of ["csharp", "java", "javascript", "c", "go", "rust", "python", "sql", "shell", "yaml", "css", "json", "markup", "diff"]) {
    for (const code of samples) assert.equal(joined(code, language), code, `${language}: ${JSON.stringify(code)}`);
  }
});

test("a whole file is fast", () => {
  const big = "public class A { void M() { var x = \"s\"; /* c */ return 1; } }\n".repeat(300).slice(0, MAX_HIGHLIGHT);
  const t0 = performance.now();
  tokenize(big, "csharp");
  assert.ok(performance.now() - t0 < 250, `took ${Math.round(performance.now() - t0)} ms`);
});
