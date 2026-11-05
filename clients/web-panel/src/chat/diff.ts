// A line diff for the consent card: what an edit changes, with line numbers, a little
// context around each change, and the long unchanged stretches folded away.
// Plain code, no library: edits are small, and the panel can't load anything.

export type DiffRow =
  | { kind: "ctx" | "add" | "del"; oldNo?: number; newNo?: number; text: string }
  | { kind: "gap"; hidden: number };

export interface DiffView {
  rows: DiffRow[];
  added: number;
  removed: number;
}

/** Beyond this many cells (changed lines before x after) the middle is shown as replaced. */
const MAX_CELLS = 4_000_000;

function lines(text: string): string[] {
  if (text === "") return [];
  const parts = text.replace(/\r\n/g, "\n").split("\n");
  if (parts[parts.length - 1] === "") parts.pop();
  return parts;
}

type Op = { kind: "ctx" | "add" | "del"; text: string };

/** The edit script from `a` to `b`: the longest common subsequence of the lines in the
 * middle (common start and end are trimmed first, which is where most edits leave them). */
function script(a: string[], b: string[]): Op[] {
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--;
    endB--;
  }
  const ops: Op[] = a.slice(0, start).map((text) => ({ kind: "ctx", text }));
  const x = a.slice(start, endA);
  const y = b.slice(start, endB);
  if (x.length * y.length > MAX_CELLS) {
    ops.push(...x.map((text) => ({ kind: "del" as const, text })), ...y.map((text) => ({ kind: "add" as const, text })));
  } else {
    const width = y.length + 1;
    const table = new Uint32Array((x.length + 1) * width);
    for (let i = x.length - 1; i >= 0; i--) {
      for (let j = y.length - 1; j >= 0; j--) {
        table[i * width + j] = x[i] === y[j] ? table[(i + 1) * width + j + 1] + 1 : Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < x.length && j < y.length) {
      if (x[i] === y[j]) {
        ops.push({ kind: "ctx", text: x[i] });
        i++;
        j++;
      } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
        ops.push({ kind: "del", text: x[i++] });
      } else {
        ops.push({ kind: "add", text: y[j++] });
      }
    }
    while (i < x.length) ops.push({ kind: "del", text: x[i++] });
    while (j < y.length) ops.push({ kind: "add", text: y[j++] });
  }
  ops.push(...a.slice(endA).map((text) => ({ kind: "ctx" as const, text })));
  return ops;
}

/**
 * The change from `before` to `after` as rows to draw. `before` null is a new file.
 * `context` unchanged lines are kept around each change; longer stretches become a
 * "gap" row saying how many lines are hidden. At most `maxRows` rows are returned (the
 * rest is one more gap row). `firstLine`: the number of the first line, when the texts
 * are only a part of a file (an Edit's old and new text without the file).
 */
export function lineDiff(
  before: string | null,
  after: string,
  { context = 2, maxRows = 60, firstLine = 1 } = {},
): DiffView {
  const ops = script(lines(before ?? ""), lines(after));
  let oldNo = firstLine;
  let newNo = firstLine;
  const numbered: DiffRow[] = ops.map((op) => {
    if (op.kind === "ctx") return { kind: "ctx", oldNo: oldNo++, newNo: newNo++, text: op.text };
    if (op.kind === "del") return { kind: "del", oldNo: oldNo++, text: op.text };
    return { kind: "add", newNo: newNo++, text: op.text };
  });
  const added = numbered.filter((r) => r.kind === "add").length;
  const removed = numbered.filter((r) => r.kind === "del").length;
  if (added + removed === 0) return { rows: [], added, removed }; // nothing changed: nothing to show
  // Keep a row if a change is within `context` rows of it.
  const keep = new Array<boolean>(numbered.length).fill(false);
  numbered.forEach((row, i) => {
    if (row.kind === "ctx") return;
    for (let k = Math.max(0, i - context); k <= Math.min(numbered.length - 1, i + context); k++) keep[k] = true;
  });
  const rows: DiffRow[] = [];
  let hidden = 0;
  numbered.forEach((row, i) => {
    if (keep[i]) {
      if (hidden) rows.push({ kind: "gap", hidden });
      hidden = 0;
      rows.push(row);
    } else {
      hidden++;
    }
  });
  if (hidden) rows.push({ kind: "gap", hidden });
  if (rows.length > maxRows) {
    const shown = rows.slice(0, maxRows);
    const rest = rows.slice(maxRows).reduce((n, r) => n + (r.kind === "gap" ? r.hidden : 1), 0);
    shown.push({ kind: "gap", hidden: rest });
    return { rows: shown, added, removed };
  }
  return { rows, added, removed };
}
