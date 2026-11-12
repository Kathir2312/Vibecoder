// The agent's plan as a card: the steps, the files each one touches, and what the user can do about it:
// approve it (and carry on in the usual mode, or accepting edits), edit the steps, or send it back with a
// note. It is a permission request for the tool ExitPlanMode (nothing changes until it is answered), so
// the answer is an ordinary `permission` message: approve = allow, edited steps = its feedback (one step
// per line), send back = deny with the note. The engine does the rest (src/cmcoder/middleware/permission.py).

export interface PlanStepInfo {
  text: string;
  files: string[];
}

export interface PlanInfo {
  title: string;
  steps: PlanStepInfo[];
  outOfScope: string;
}

/** What the answer to the plan is. */
export interface PlanAnswer {
  allow: boolean;
  /** Approve and accept edits for the session (needs the IDE's "sessionRules"). */
  acceptEdits?: boolean;
  /** The steps the user changed, one per line (only when they differ from the plan). */
  edited?: string;
  /** Why the plan is sent back. */
  note?: string;
}

export interface PlanHost {
  /** The IDE can take "accept edits for the session" with an approval. */
  canAcceptEdits: boolean;
  /** Put the answer to the engine and close the card with what was said. */
  answer(answer: PlanAnswer, said: string): void;
  /** The name of the product, for the note's placeholder. */
  product: string;
}

/** The plan out of a request's `input` (the model's arguments: nothing is trusted to be there). */
export function planOf(input: unknown): PlanInfo {
  const i = (input && typeof input === "object" ? input : {}) as Record<string, unknown>;
  const steps = Array.isArray(i.steps) ? i.steps : [];
  return {
    title: typeof i.title === "string" && i.title.trim() ? i.title.trim() : "Plan",
    steps: steps
      .filter((s): s is Record<string, unknown> => !!s && typeof s === "object" && typeof (s as Record<string, unknown>).text === "string")
      .map((s) => ({
        text: String(s.text),
        files: Array.isArray(s.files) ? s.files.filter((f): f is string => typeof f === "string") : [],
      })),
    outOfScope: typeof i.out_of_scope === "string" ? i.out_of_scope.trim() : "",
  };
}

function node<K extends keyof HTMLElementTagNameMap>(tag: K, cls = "", text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

const NUMBERING = /^\s*(?:\d+[.)]|[-*•])\s*/;

/** The steps typed into the edit box: one per line, numbering and bullets dropped. */
export function linesOf(text: string): string[] {
  return text
    .split("\n")
    .map((l) => l.replace(NUMBERING, "").trim())
    .filter(Boolean);
}

function fileCount(steps: PlanStepInfo[]): number {
  return new Set(steps.flatMap((s) => s.files)).size;
}

/** The card for a plan. The caller adds it to the conversation and takes it off when it is answered. */
export function planCard(plan: PlanInfo, host: PlanHost): HTMLElement {
  const card = node("div", "permission plan");
  let steps = plan.steps;
  let edited: string | undefined;

  const head = node("div", "ask-head");
  const icon = node("span", "ask-icon", "☰");
  icon.setAttribute("aria-hidden", "true");
  head.append(icon, node("span", "ask-title", `Plan · ${plan.title}`), node("span", "ask-count"));
  card.append(head);

  const meta = node("div", "plan-meta");
  const list = node("ol", "plan-steps");
  const draw = () => {
    const files = fileCount(steps);
    meta.textContent = `${steps.length} step${steps.length === 1 ? "" : "s"}${files ? ` · ${files} file${files === 1 ? "" : "s"}` : ""}${edited !== undefined ? " · edited by you" : ""}`;
    list.replaceChildren(
      ...steps.map((s) => {
        const li = node("li", "plan-step");
        li.append(node("span", "plan-text", s.text));
        if (s.files.length) {
          const files = node("span", "plan-files");
          for (const f of s.files) files.append(node("code", "plan-file", f));
          li.append(files);
        }
        return li;
      }),
    );
  };
  draw();
  card.append(meta, list);
  if (plan.outOfScope) card.append(node("div", "plan-scope", `Out of scope: ${plan.outOfScope}`));

  // Editing: the steps as lines of text.
  const edit = node("div", "plan-edit");
  edit.hidden = true;
  const box = node("textarea", "plan-edit-box");
  box.rows = Math.min(12, Math.max(4, steps.length + 1));
  box.setAttribute("aria-label", "The steps, one per line");
  const editButtons = node("div", "buttons");
  const useThese = node("button", "", "Use these steps");
  useThese.type = "button";
  const cancelEdit = node("button", "secondary", "Cancel");
  cancelEdit.type = "button";
  editButtons.append(useThese, cancelEdit);
  edit.append(node("div", "plan-edit-hint", "One step per line. Numbers and dashes are fine."), box, editButtons);

  // Sending it back: a note for the model.
  const back = node("div", "ask-deny");
  back.hidden = true;
  const note = node("input");
  note.type = "text";
  note.placeholder = `Tell ${host.product} what to change in the plan`;
  note.setAttribute("aria-label", "What to change in the plan");
  const sendBack = node("button", "secondary", "Send back");
  sendBack.type = "button";
  back.append(note, sendBack);
  const sendNote = () => host.answer({ allow: false, note: note.value.trim() }, note.value.trim() ? `Sent back · “${note.value.trim().slice(0, 80)}”` : "Sent back");
  sendBack.onclick = sendNote;
  note.onkeydown = (e) => {
    if (e.key === "Enter") sendNote();
  };

  const actions = node("div", "ask-actions");
  const approveWith = (acceptEdits: boolean) => () =>
    host.answer(
      { allow: true, ...(acceptEdits ? { acceptEdits: true } : {}), ...(edited !== undefined ? { edited } : {}) },
      `Approved${acceptEdits ? " · edits are accepted" : ""}${edited !== undefined ? " · with your changes" : ""}`,
    );
  const approve = node("button", "", "Approve and run");
  approve.type = "button";
  approve.title = "Carry the plan out; changes still ask first";
  approve.onclick = approveWith(false);
  actions.append(approve);
  if (host.canAcceptEdits) {
    const accept = node("button", "secondary", "Approve, accept edits");
    accept.type = "button";
    accept.title = "Carry the plan out and let it edit files without asking, until you change the mode";
    accept.onclick = approveWith(true);
    actions.append(accept);
  }
  const editButton = node("button", "secondary", "Edit steps");
  editButton.type = "button";
  editButton.onclick = () => {
    box.value = steps.map((s, i) => `${i + 1}. ${s.text}`).join("\n");
    edit.hidden = false;
    actions.hidden = true;
    box.focus();
  };
  const revise = node("button", "secondary", "Revise…");
  revise.type = "button";
  revise.title = "Send the plan back with a note on what to change";
  revise.onclick = () => {
    back.hidden = false;
    actions.hidden = true;
    note.focus();
  };
  const reject = node("button", "link", "Reject");
  reject.type = "button";
  reject.title = "Stop: the plan is not wanted";
  reject.onclick = () => host.answer({ allow: false }, "Rejected");
  actions.append(editButton, revise, reject);

  useThese.onclick = () => {
    const lines = linesOf(box.value);
    if (lines.length) {
      const same = lines.length === steps.length && lines.every((l, i) => l === steps[i].text);
      if (!same) {
        const files = new Map(steps.map((s) => [s.text, s.files]));
        steps = lines.map((text) => ({ text, files: files.get(text) ?? [] }));
        edited = lines.join("\n");
        draw();
      }
    }
    edit.hidden = true;
    actions.hidden = false;
    approve.focus();
  };
  cancelEdit.onclick = () => {
    edit.hidden = true;
    actions.hidden = false;
    editButton.focus();
  };
  box.onkeydown = (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      cancelEdit.click();
    }
  };

  card.append(edit, actions, back);
  return card;
}
