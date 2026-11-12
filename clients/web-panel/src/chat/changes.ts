// What a request changed, for review: a bar in the conversation (files, lines, Review changes, Undo all)
// and a view with every file and hunk and a button to take each back. The engine does the work: it lists
// the files and hunks (a `turn_changes` event) and takes them back when asked (`/revert <request> <file>
// <hunk>`, numbers only, never a path), then sends the list again with what is back marked.
// Nothing here knows how a file is put back; it draws the list and says which button was pressed.

import type { AgentEvent } from "../protocol";

export type TurnChanges = Extract<AgentEvent, { type: "turn_changes" }>;
type ChangedFile = TurnChanges["files"][number];

export interface ChangesHost {
  /** Open the review of this request. */
  review(turn: number): void;
  /** Take back: a hunk (`file` and `hunk`), a file (`hunk` null) or the whole request (both null). */
  revert(turn: number, file: number | null, hunk: number | null): void;
  /** Back to the conversation. */
  back(): void;
}

function node<K extends keyof HTMLElementTagNameMap>(tag: K, cls = "", text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

/** A button. Its accessible name, when it needs more than its text (which file), always starts with that text. */
function button(label: string, title: string, aria?: string, cls = "secondary"): HTMLButtonElement {
  const b = node("button", cls, label);
  b.type = "button";
  b.title = title;
  if (aria) b.setAttribute("aria-label", aria);
  return b;
}

const plural = (n: number, word: string): string => `${n} ${word}${n === 1 ? "" : "s"}`;

function counts(ev: TurnChanges): HTMLElement {
  const span = node("span", "changes-counts");
  span.append(node("span", "diff-add", `+${ev.added}`), document.createTextNode(" "), node("span", "diff-del", `−${ev.removed}`));
  return span;
}

/** How many files are completely back. */
function back(ev: TurnChanges): number {
  return ev.files.filter((f) => f.reverted).length;
}

/** The bar that closes a request which changed files. */
export function changesBar(ev: TurnChanges, host: ChangesHost): HTMLElement {
  const bar = node("div", "changes-bar");
  bar.dataset.turn = String(ev.turn);
  bar.setAttribute("role", "group");
  bar.setAttribute("aria-label", "Files this request changed");
  const text = node("div", "changes-text");
  const actions = node("div", "changes-actions");
  const review = button("Review changes", "See each file and hunk, and take back what you don't want", "Review changes of this request", "");
  review.onclick = () => host.review(ev.turn);
  const undo = button("Undo all", "Put every file back as it was before this request", "Undo all the changes of this request");
  undo.className = "secondary changes-undo";
  undo.onclick = () => host.revert(ev.turn, null, null);
  actions.append(review, undo);
  bar.append(text, actions);
  updateBar(bar, ev);
  return bar;
}

/** The bar after the engine sent the list again. */
export function updateBar(bar: HTMLElement, ev: TurnChanges): void {
  const text = bar.querySelector(".changes-text") as HTMLElement;
  const done = back(ev);
  const all = done === ev.files.length;
  text.replaceChildren(
    node("strong", "", all ? `All ${plural(ev.files.length, "file")} taken back` : plural(ev.files.length, "file") + " changed"),
    document.createTextNode(" "),
    counts(ev),
  );
  if (done && !all) text.append(node("div", "changes-sub", `${done} of ${ev.files.length} taken back`));
  const undo = bar.querySelector(".changes-undo") as HTMLButtonElement;
  undo.hidden = all;
  bar.classList.toggle("all-back", all);
}

function hunkRows(file: ChangedFile): HTMLElement {
  const rows = node("div", "diff-hunks");
  for (const h of file.hunks) {
    const box = node("div", `hunk${h.reverted ? " back" : ""}`);
    box.append(node("div", "hunk-head", `Line ${h.new_start}${h.reverted ? " · taken back" : ""}`));
    const lines = node("div", "hunk-lines");
    for (const line of h.lines) {
      const row = node("div", `dl ${line.kind}`);
      row.append(node("span", "dl-sign", line.kind === "add" ? "+" : line.kind === "del" ? "−" : ""), node("span", "dl-text", line.text));
      lines.append(row);
    }
    box.append(lines);
    rows.append(box);
  }
  return rows;
}

/** The review: every file of the request, with a button for each file and each hunk. */
export function reviewView(ev: TurnChanges, host: ChangesHost, busy: boolean): HTMLElement {
  const view = node("div", "review");
  const head = node("div", "review-head");
  const backButton = button("← Chat", "Back to the conversation", undefined, "secondary review-back");
  backButton.onclick = () => host.back();
  const title = node("span", "review-title");
  title.append(node("strong", "", `Changes · ${plural(ev.files.length, "file")} `), counts(ev));
  const all = button("Revert all", "Put every file back as it was before this request", "Revert all the changes of this request");
  all.disabled = busy || back(ev) === ev.files.length;
  all.onclick = () => host.revert(ev.turn, null, null);
  head.append(backButton, title, all);
  view.append(head);
  view.append(node("p", "review-hint", "These are already in your files. Taking a part back puts the file as it was before this request; a file you edited yourself since is left alone."));

  for (const f of ev.files) {
    const box = node("section", `review-file${f.reverted ? " back" : ""}`);
    box.dataset.file = String(f.index);
    box.setAttribute("aria-label", f.path);
    const fh = node("div", "file-head");
    fh.append(node("span", `file-status ${f.status}`, f.status === "added" ? "A" : "M"));
    fh.append(node("span", "file-path", f.path));
    const stat = node("span", "file-stat");
    stat.append(node("span", "diff-add", `+${f.added}`), document.createTextNode(" "), node("span", "diff-del", `−${f.removed}`));
    fh.append(stat);
    if (f.reverted) fh.append(node("span", "file-back", f.status === "added" ? "deleted" : "taken back"));
    else {
      const b = button(f.status === "added" ? "Delete file" : "Revert file", f.status === "added" ? "Delete this file: cmcoder created it" : "Put this file back as it was before this request", `${f.status === "added" ? "Delete file" : "Revert file"} ${f.path}`);
      b.disabled = busy;
      b.onclick = () => host.revert(ev.turn, f.index, null);
      fh.append(b);
    }
    box.append(fh);
    if (f.note) box.append(node("div", "file-note", f.note));
    if (f.binary) box.append(node("div", "file-sub", "Not a text file: it can only be taken back whole."));
    else if (f.truncated) box.append(node("div", "file-sub", "A rewrite too big to show: it can only be taken back whole."));
    else if (f.hunks.length) {
      const rows = hunkRows(f);
      f.hunks.forEach((h, i) => {
        if (h.reverted || f.status === "added" || f.hunks.length === 1) return; // one hunk: the file's button is the same thing
        const b = button("Revert hunk", "Take back just this part", `Revert hunk ${i + 1} of ${f.path}`, "secondary hunk-revert");
        b.disabled = busy;
        b.onclick = () => host.revert(ev.turn, f.index, h.id);
        (rows.children[i].querySelector(".hunk-head") as HTMLElement).append(b);
      });
      box.append(rows);
    }
    view.append(box);
  }
  return view;
}
