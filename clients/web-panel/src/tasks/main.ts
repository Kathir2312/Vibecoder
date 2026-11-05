// The Tasks tab (an editor tab, its own webview): the agent's whole todo list, so the chat window
// stays free. The chat panel shows only a compact bar (done/total, a meter, the task in progress);
// this is what that bar opens. It lists every task with its number, grouped as In progress, To do
// and Done, with a filter for a long list, and follows the agent live.

import { bridge, fromHost, hostSettings } from "../bridge";
import type { FromTasks, ToTasks } from "../messages";
import { summarizeTodos, TODO_MARKS, type TodoRow } from "../todos";

const host = bridge<FromTasks>();
const post = (m: FromTasks) => host.post(m);
hostSettings();

const app = document.getElementById("app")!;
app.innerHTML = `
  <header>
    <div class="head">
      <span class="name">Tasks</span>
      <span class="count"></span>
      <button class="secondary back" title="Back to the chat">Chat</button>
    </div>
    <div class="meter"><div class="fill"></div></div>
    <input class="filter" type="search" placeholder="Filter tasks…" aria-label="Filter tasks" hidden>
  </header>
  <main></main>`;

const countLabel = app.querySelector<HTMLElement>(".count")!;
const fill = app.querySelector<HTMLElement>(".fill")!;
const filter = app.querySelector<HTMLInputElement>(".filter")!;
const main = app.querySelector<HTMLElement>("main")!;
app.querySelector<HTMLButtonElement>(".back")!.onclick = () => post({ kind: "openChat" });

let todos: Record<string, unknown>[] = [];
let doneOpen: boolean | undefined; // the user's choice for the Done group, once made

function el(tag: string, cls: string, text?: string): HTMLElement {
  const e = document.createElement(tag);
  e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function row(r: TodoRow): HTMLElement {
  const item = el("div", `task ${r.status}`);
  item.append(el("span", "no", `${r.no}.`), el("span", "mark", TODO_MARKS[r.status]), el("span", "text", r.text));
  return item;
}

function group(title: string, rows: TodoRow[], cls: string): HTMLElement | undefined {
  if (!rows.length) return undefined;
  const box = el("section", `group ${cls}`);
  box.append(el("h2", "", `${title} · ${rows.length}`));
  box.append(...rows.map(row));
  return box;
}

function render(): void {
  const s = summarizeTodos(todos);
  countLabel.textContent = s.total ? `${s.done} of ${s.total} done` : "";
  fill.style.width = `${s.percent}%`;
  filter.hidden = s.total < 8; // a short list needs no filter
  main.replaceChildren();
  if (!s.total) {
    main.append(el("p", "empty", "No tasks yet. The agent writes a task list when a job has several steps."));
    return;
  }
  const q = filter.value.trim().toLowerCase();
  const rows = q ? s.rows.filter((r) => r.text.toLowerCase().includes(q) || String(r.no) === q) : s.rows;
  if (!rows.length) {
    main.append(el("p", "empty", `No task matches “${filter.value.trim()}”.`));
    return;
  }
  const active = group("In progress", rows.filter((r) => r.status === "in_progress"), "active");
  const pending = group("To do", rows.filter((r) => r.status === "pending"), "pending");
  const finished = rows.filter((r) => r.status === "completed");
  if (active) main.append(active);
  if (pending) main.append(pending);
  if (finished.length) {
    const details = document.createElement("details");
    details.className = "group done";
    // Done tasks fold away while there is more to do, unless the user opened them.
    details.open = doneOpen ?? (!active && !pending);
    details.ontoggle = () => (doneOpen = details.open);
    details.append(el("summary", "", `Done · ${finished.length}`), ...finished.map(row));
    main.append(details);
  }
  // Keep the task in progress in view in a long list.
  main.querySelector(".task.in_progress")?.scrollIntoView({ block: "nearest" });
}

filter.oninput = render;

window.addEventListener("message", (e: MessageEvent<ToTasks>) => {
  if (!fromHost(e)) return;
  const m = e.data;
  if (m.kind === "todos") todos = m.todos;
  else if (m.kind === "reset") {
    todos = [];
    filter.value = "";
    doneOpen = undefined;
  } else return;
  render();
});

render();
post({ kind: "ready" });
