// The settings page: the models, how much the agent may do on its own, the checks after an edit and
// the audit log, as a form in the chat. The engine decides everything that matters (which settings are
// offered, what is valid, where it is saved, what is fixed by the organisation); this draws what a
// `settings_state` event lists and says which control was changed, as the message
// `/settings set <key> <value>` (a list as JSON), so no IDE needs anything new for it. An API key is
// never typed here (it would pass through the IDE and its logs): the page shows whether one was found
// and points to `cmcoder login`.

import type { AgentEvent } from "../protocol";

export type SettingsState = Extract<AgentEvent, { type: "settings_state" }>;
export type ConnectionTested = Extract<AgentEvent, { type: "connection_tested" }>;
type Field = SettingsState["fields"][number];

export interface SettingsHost {
  /** Save one setting (the message `/settings set <key> <value>`). */
  set(key: string, value: string): void;
  /** Ask the gateway for its models (`/settings test`). */
  test(): void;
  /** Back to the conversation. */
  back(): void;
}

export interface SettingsStatus {
  /** The last connection test, if any. */
  tested?: ConnectionTested;
  /** A test is running. */
  testing: boolean;
  /** A request is running: settings change between requests. */
  busy: boolean;
}

function node<K extends keyof HTMLElementTagNameMap>(tag: K, cls = "", text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

const MODEL_KEYS = new Set(["model", "smallFastModel", "visionModel", "fallbackModel"]);

/** What to send for a control's value: the engine reads "null" as "back to the default". */
function textOf(field: Field, control: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement): string {
  if (field.kind === "bool") return String((control as HTMLInputElement).checked);
  const value = control.value.trim();
  if (field.kind === "lines") {
    return JSON.stringify(value.split("\n").map((l) => l.trim()).filter(Boolean));
  }
  return value === "" ? "null" : value;
}

function control(field: Field, id: string, state: SettingsState, host: SettingsHost, disabled: boolean): HTMLElement {
  let c: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;
  if (field.kind === "bool") {
    const box = node("input");
    box.type = "checkbox";
    box.checked = field.value === true;
    c = box;
  } else if (field.kind === "choice") {
    const select = node("select");
    for (const choice of field.choices) {
      const option = node("option", "", choice);
      option.value = choice;
      select.append(option);
    }
    select.value = String(field.value ?? "");
    c = select;
  } else if (field.kind === "lines") {
    const area = node("textarea");
    area.rows = 3;
    area.value = Array.isArray(field.value) ? field.value.join("\n") : "";
    area.spellcheck = false;
    c = area;
  } else if (field.kind === "number") {
    const number = node("input");
    number.type = "number";
    number.value = field.value === null || field.value === undefined ? "" : String(field.value);
    c = number;
  } else {
    const text = node("input");
    text.type = "text";
    text.spellcheck = false;
    text.value = field.value === null || field.value === undefined ? "" : String(field.value);
    if (MODEL_KEYS.has(field.key) && state.models.length) text.setAttribute("list", "settings-models");
    c = text;
  }
  c.id = id;
  c.dataset.key = field.key;
  c.disabled = disabled || field.locked;
  c.onchange = () => host.set(field.key, textOf(field, c));
  return c;
}

/** The whole page. Drawn again after every event, so what it shows is what the engine says. */
export function settingsView(state: SettingsState, host: SettingsHost, status: SettingsStatus): HTMLElement {
  const view = node("div", "settings");
  const head = node("div", "settings-head");
  const back = node("button", "secondary settings-back", "← Chat");
  back.type = "button";
  back.title = "Back to the conversation";
  back.onclick = () => host.back();
  head.append(back, node("strong", "settings-title", "Settings"));
  view.append(head);

  const notice = node("div", "settings-notice");
  notice.setAttribute("role", "status");
  notice.textContent = state.notice || (status.busy ? "Settings change between requests: wait for this one to finish." : "");
  notice.hidden = !notice.textContent;
  view.append(notice);

  // The gateway: where the models come from, whether there is a key, and a way to try it.
  const gateway = node("section", "settings-group");
  gateway.setAttribute("aria-label", "Gateway");
  gateway.append(node("h2", "settings-heading", "Gateway"));
  for (const g of state.gateways) {
    const row = node("div", "settings-gateway");
    row.append(node("span", "gw-name", g.name), node("code", "gw-url", g.base_url));
    const key = node(
      "span",
      `gw-key ${g.key === "missing" ? "missing" : "ok"}`,
      g.key === "set" ? "✓ API key found" : g.key === "not needed" ? "✓ no key needed" : "✗ no API key: run cmcoder login in a terminal",
    );
    row.append(key);
    gateway.append(row);
  }
  const test = node("button", "secondary settings-test", status.testing ? "Testing…" : "Test connection");
  test.type = "button";
  test.disabled = status.testing || state.gateways.length === 0;
  test.onclick = () => host.test();
  const result = node("span", "settings-tested");
  result.setAttribute("role", "status");
  if (status.tested) {
    result.classList.add(status.tested.ok ? "ok" : "fail");
    result.textContent = `${status.tested.ok ? "✓" : "✗"} ${status.tested.message}${status.tested.ok ? ` · ${status.tested.elapsed_ms} ms` : ""}`;
  }
  const testRow = node("div", "settings-test-row");
  testRow.append(test, result);
  gateway.append(testRow);
  view.append(gateway);

  const groups = new Map<string, Field[]>();
  for (const f of state.fields) groups.set(f.group, [...(groups.get(f.group) ?? []), f]);
  let n = 0;
  for (const [name, fields] of groups) {
    const group = node("section", "settings-group");
    group.setAttribute("aria-label", name);
    group.append(node("h2", "settings-heading", name));
    for (const field of fields) {
      const id = `setting-${n++}`;
      const row = node("div", `settings-row ${field.kind}`);
      const label = node("label", "settings-label", field.label);
      label.htmlFor = id;
      const help = node("div", "settings-help", [field.help, field.note].filter(Boolean).join(" · "));
      help.id = `${id}-help`;
      const c = control(field, id, state, host, status.busy);
      c.setAttribute("aria-describedby", help.id);
      if (field.kind === "bool") row.append(c, label);
      else row.append(label, c);
      row.append(help);
      group.append(row);
    }
    view.append(group);
  }

  if (state.models.length) {
    const list = node("datalist");
    list.id = "settings-models";
    for (const m of state.models) {
      const option = node("option");
      option.value = m;
      list.append(option);
    }
    view.append(list);
  }
  view.append(node("p", "settings-foot", `Saved in ${state.file}. Gateways, hooks, the sandbox and other settings are edited in that file. The API key is never typed here.`));
  return view;
}
