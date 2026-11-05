// The todo list's counts and rows (src/todos.ts), shared by the chat panel's bar and the Tasks tab.

import assert from "node:assert/strict";
import { test } from "node:test";
import { summarizeTodos } from "../src/todos";

test("counts, the task in progress and the percentage", () => {
  const s = summarizeTodos([
    { content: "Plan", status: "completed" },
    { content: "Build", status: "in_progress" },
    { content: "Test", status: "pending" },
    { content: "Ship", status: "pending" },
  ]);
  assert.deepEqual([s.total, s.done, s.active, s.pending, s.percent], [4, 1, 1, 2, 25]);
  assert.deepEqual(s.current, { no: 2, status: "in_progress", text: "Build" });
  assert.deepEqual(s.rows.map((r) => r.no), [1, 2, 3, 4]);
});

test("with nothing in progress the next task is the current one; all done has none", () => {
  const next = summarizeTodos([{ content: "A", status: "completed" }, { content: "B", status: "pending" }]);
  assert.equal(next.current?.text, "B");
  const finished = summarizeTodos([{ content: "A", status: "completed" }]);
  assert.equal(finished.current, null);
  assert.equal(finished.percent, 100);
  assert.deepEqual(summarizeTodos([]), { rows: [], total: 0, done: 0, active: 0, pending: 0, current: null, percent: 0 });
});

test("odd entries do not break it", () => {
  const s = summarizeTodos([{ status: "weird" }, { content: "  Spaced  ", status: "completed" }, { activeForm: "Doing X" }]);
  assert.deepEqual(s.rows.map((r) => [r.status, r.text]), [
    ["pending", "(untitled task)"],
    ["completed", "Spaced"],
    ["pending", "Doing X"],
  ]);
});
