// An error as a card: what happened in plain words, the engine's own message, and the buttons that make
// sense for it. The engine decides which buttons (`Error.actions`): retrying a gateway that was down
// is offered, retrying a refused key is not. The panel only draws them and says what each one does.

import type { AgentEvent } from "../protocol";

type ErrorEvent = Extract<AgentEvent, { type: "error" }>;
type Action = ErrorEvent["actions"][number];

export interface ErrorCardHost {
  /** The IDE can show its log (data-features "openLog"). */
  canOpenLog: boolean;
  retry(): void;
  switchModel(): void;
  compact(): void;
  newConversation(): void;
  openLog(): void;
}

interface Spec {
  label: string;
  title: string;
  /** Done for good: the other buttons go too, and the card says what is happening. */
  final?: string;
}

const SPECS: Record<Action, Spec> = {
  retry: { label: "Retry", title: "Run the step that failed again; your message is not sent twice", final: "Retrying…" },
  switch_model: { label: "Switch model", title: "Put /model in the message box: type the model to use instead" },
  compact: { label: "Compact", title: "Summarise the conversation to make room (/compact)", final: "Compacting…" },
  new_conversation: { label: "New conversation", title: "Start again with an empty conversation (the old one stays in History)", final: "Started a new conversation." },
  open_log: { label: "Open log", title: "Show the IDE's log for this extension" },
};

const MAX_SHOWN = 280;

function node<K extends keyof HTMLElementTagNameMap>(tag: K, cls: string, text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

/** The card for an error that has a title or actions; the caller puts it in the conversation. */
export function errorCard(ev: ErrorEvent, host: ErrorCardHost): HTMLElement {
  const card = node("div", "alert error");
  card.setAttribute("role", "alert"); // read out at once, which an error should be
  const title = node("div", "alert-title");
  const icon = node("span", "alert-icon", "⚠");
  icon.setAttribute("aria-hidden", "true");
  title.append(icon, document.createTextNode(` ${ev.title || "Something went wrong"}`));
  card.append(title);

  const message = ev.message.trim();
  const first = message.split("\n").find((l) => l.trim()) ?? "";
  card.append(node("div", "alert-message", first.length > MAX_SHOWN ? `${first.slice(0, MAX_SHOWN)}…` : first));
  if (ev.hint) card.append(node("div", "alert-hint", ev.hint));
  if (message.length > first.length || first.length > MAX_SHOWN) {
    const more = node("details", "alert-more");
    more.append(node("summary", "", "Full message"), node("pre", "", message));
    card.append(more);
  }

  const row = node("div", "alert-actions");
  const status = node("div", "alert-status");
  status.hidden = true;
  const handlers: Record<Action, () => void> = {
    retry: () => host.retry(),
    switch_model: () => host.switchModel(),
    compact: () => host.compact(),
    new_conversation: () => host.newConversation(),
    open_log: () => host.openLog(),
  };
  for (const action of ev.actions ?? []) {
    const spec = SPECS[action];
    if (!spec || (action === "open_log" && !host.canOpenLog)) continue;
    const b = node("button", action === "retry" ? "" : "secondary", spec.label);
    b.type = "button";
    b.title = spec.title;
    b.dataset.action = action;
    b.onclick = () => {
      if (spec.final) retire(card, spec.final);
      handlers[action]();
    };
    row.append(b);
  }
  if (row.childElementCount) card.append(row, status);
  return card;
}

/** The card's buttons are over: it says what happened (or nothing), and they are no longer pressable. */
export function retire(card: Element, text = ""): void {
  card.classList.add("resolved");
  card.querySelectorAll<HTMLButtonElement>(".alert-actions button").forEach((b) => (b.disabled = true));
  const status = card.querySelector<HTMLElement>(".alert-status");
  if (status && text) {
    status.textContent = text;
    status.hidden = false;
  }
}

/** A new request starts: the buttons of earlier errors would act on the wrong one, so they stop. */
export function retireErrorCards(root: ParentNode = document): void {
  root.querySelectorAll(".alert.error:not(.resolved)").forEach((card) => retire(card));
}
