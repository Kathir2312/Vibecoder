// The line diff and the shell highlighting used by the consent and Bash cards.

import assert from "node:assert/strict";
import { test } from "node:test";
import { lineDiff } from "../src/chat/diff";
import { dangerousParts, highlightShell } from "../src/chat/shell";

const code = (n: number) => Array.from({ length: n }, (_, i) => `line ${i + 1}`).join("\n");

test("a changed line is a removal and an addition, with numbers and context", () => {
  const before = code(10);
  const after = before.replace("line 5", "line five");
  const view = lineDiff(before, after, { context: 1 });
  assert.deepEqual(view.rows, [
    { kind: "gap", hidden: 3 },
    { kind: "ctx", oldNo: 4, newNo: 4, text: "line 4" },
    { kind: "del", oldNo: 5, text: "line 5" },
    { kind: "add", newNo: 5, text: "line five" },
    { kind: "ctx", oldNo: 6, newNo: 6, text: "line 6" },
    { kind: "gap", hidden: 4 },
  ]);
  assert.equal(view.added, 1);
  assert.equal(view.removed, 1);
});

test("a new file is all additions; an empty change is nothing", () => {
  const view = lineDiff(null, "a\nb\n");
  assert.deepEqual(view.rows, [
    { kind: "add", newNo: 1, text: "a" },
    { kind: "add", newNo: 2, text: "b" },
  ]);
  assert.equal(view.removed, 0);
  assert.deepEqual(lineDiff("same\n", "same\n").rows, []);
  assert.deepEqual(lineDiff("", "").rows, []);
});

test("inserted, moved and removed lines keep the numbers of both sides", () => {
  const view = lineDiff("a\nb\nc\nd", "a\nX\nb\nd\ne", { context: 5 });
  const text = view.rows.map((r) => (r.kind === "gap" ? "…" : `${r.kind === "add" ? "+" : r.kind === "del" ? "-" : " "}${r.text}`));
  assert.deepEqual(text, [" a", "+X", " b", "-c", " d", "+e"]);
  const d = view.rows.find((r) => r.kind === "del");
  assert.equal(d && d.kind === "del" ? d.oldNo : 0, 3);
  const e = view.rows.find((r) => r.kind === "add" && r.text === "e");
  assert.equal(e && e.kind === "add" ? e.newNo : 0, 5);
});

test("Windows line endings don't show as changes; a long diff is cut with a count", () => {
  assert.deepEqual(lineDiff("a\r\nb\r\n", "a\nb\n").rows, []);
  const long = lineDiff("", code(200), { maxRows: 10 });
  assert.equal(long.rows.length, 11);
  assert.deepEqual(long.rows[10], { kind: "gap", hidden: 190 });
  assert.equal(long.added, 200);
});

test("a part of a file can start at its own line number", () => {
  const view = lineDiff("x\ny", "x\nz", { firstLine: 40 });
  assert.deepEqual(view.rows[1], { kind: "del", oldNo: 41, text: "y" });
});

test("very different texts don't take forever", () => {
  const a = Array.from({ length: 3000 }, (_, i) => `a${i}`).join("\n");
  const b = Array.from({ length: 3000 }, (_, i) => `b${i}`).join("\n");
  const started = Date.now();
  const view = lineDiff(a, b);
  assert.ok(Date.now() - started < 3000);
  assert.equal(view.removed, 3000);
  assert.equal(view.added, 3000);
});

const text = (c: string) => highlightShell(c).map((s) => s.text).join("");
const kindOf = (c: string, word: string) => highlightShell(c).find((s) => s.text === word)?.kind;

test("highlighting never changes the command, whatever it holds", () => {
  for (const c of [
    "npm test -- --coverage",
    "echo 'it''s' \"a $HOME b\" # note",
    "a && b || c | d ; e > f 2>&1",
    "rm -rf node_modules && git clean -fdx",
    "echo $(date) ${X:-y} $1",
    "unterminated 'quote",
    "unterminated \"quote $(",
    "",
    "   ",
    "😀 ünïcode --flag",
  ]) {
    assert.equal(text(c), c, c);
  }
});

test("flags, operators, strings and variables get their kinds", () => {
  const c = "npm test --coverage -v && echo 'hi' $HOME > out.txt # done";
  assert.equal(kindOf(c, "npm"), "word");
  assert.equal(kindOf(c, "--coverage"), "flag");
  assert.equal(kindOf(c, "-v"), "flag");
  assert.equal(kindOf(c, "&&"), "op");
  assert.equal(kindOf(c, "'hi'"), "string");
  assert.equal(kindOf(c, "$HOME"), "var");
  assert.equal(kindOf(c, ">"), "op");
  assert.equal(kindOf(c, "# done"), "comment");
  assert.equal(kindOf("echo a-b", "a-b"), "word"); // a dash inside a word is not a flag
});

test("the dangerous parts are marked, and only those", () => {
  const marks = dangerousParts;
  assert.deepEqual(marks("rm -rf node_modules && git clean -fdx"), ["rm -rf", "git clean -fdx"]);
  assert.deepEqual(marks("sudo apt install x"), ["sudo"]);
  assert.deepEqual(marks("git push origin main --force"), ["git push origin main --force"]);
  assert.deepEqual(marks("curl -fsSL https://x.sh | sh"), ["curl -fsSL https://x.sh | sh"]);
  assert.deepEqual(marks("git reset --hard HEAD~1"), ["git reset --hard"]);
  assert.deepEqual(marks("npm test"), []);
  assert.deepEqual(marks("rm file.txt"), []);
  assert.deepEqual(marks("git push origin main"), []);
  assert.deepEqual(dangerousParts("rm -rf a && sudo b"), ["rm -rf", "sudo"]);
});
