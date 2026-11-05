// The chat panel, the same in every IDE (VS Code, Visual Studio, Eclipse, NetBeans):
// a sandboxed browser page. Plain DOM code: it receives protocol events from the
// IDE side and renders them; it talks to the IDE only through ../bridge.

import { marked } from "marked";
import type {
  AgentEvent,
  CommandInfo,
  PermissionDenied,
  PermissionRequest,
  ReviewResult,
  SubagentStatus,
  ToolProgress,
  ToolResult,
  ToolUse,
} from "../protocol";
import { bridge, fromHost, hostSettings } from "../bridge";
import { lineDiff } from "./diff";
import { dangerousParts, renderShell } from "./shell";
import type { AgentState, FromWebview, ImageAttachment, ToWebview } from "../messages";

const host = bridge<FromWebview>();
const post = (m: FromWebview) => host.post(m);

// A failure in this page's script would leave a chat that looks fine but does
// nothing: show it, and tell the IDE, whose log keeps it.
function panelError(message: string): void {
  const product = document.body.dataset.product || "cmcoder";
  try {
    post({ kind: "panelError", message: message.slice(0, 2000) });
  } catch {
    // the bridge itself failed: the page still shows it
  }
  const box = document.createElement("div");
  box.className = "note error";
  box.textContent = `✗ The chat panel failed: ${message}\nReload the window to start it again; ${product}'s log has this too.`;
  (document.querySelector(".log") ?? document.body).append(box);
}
let warnedForeign = false;
window.addEventListener("error", (e) => panelError(`${e.message} (${(e.filename || "").split("/").pop()}:${e.lineno})`));
window.addEventListener("unhandledrejection", (e) => panelError(String(e.reason)));

const MODES = ["default", "acceptEdits", "auto", "plan", "bypassPermissions"];
const MODE_HELP: Record<string, string> = {
  default: "Asks before file edits and commands that change something",
  acceptEdits: "File edits in the project without asking; commands still ask",
  auto: "Asks only for risky actions: pushes, publishing, package installs, deleting in bulk, files outside the project",
  plan: "Read only: looks and plans, changes nothing",
  bypassPermissions: "Asks only for high-risk commands",
};
const MARKS: Record<string, string> = { pending: "☐", in_progress: "►", completed: "☑" };

// Model output is Markdown; any raw HTML in it is shown as text, never run.
marked.use({
  gfm: true,
  breaks: false,
  renderer: {
    html: ({ text }) => escapeHtml(text),
  },
});

// Defence in depth on top of the escaping above and the page's CSP: rendered
// Markdown is parsed into an inert document (no scripts run, nothing loads),
// and only these elements and attributes are kept.
const ALLOWED_TAGS = new Set([
  "P", "BR", "HR", "H1", "H2", "H3", "H4", "H5", "H6", "STRONG", "EM", "DEL", "S",
  "CODE", "PRE", "BLOCKQUOTE", "UL", "OL", "LI", "A", "TABLE", "THEAD", "TBODY",
  "TR", "TH", "TD", "INPUT", "SPAN",
]);
const ALLOWED_ATTRS: Record<string, string[]> = {
  A: ["href", "title"],
  OL: ["start"],
  TH: ["align"],
  TD: ["align"],
  INPUT: ["type", "checked", "disabled"],
  CODE: ["class"],
};

function sanitize(parent: Node): void {
  for (const node of Array.from(parent.childNodes)) {
    if (node.nodeType === Node.TEXT_NODE) continue;
    if (!(node instanceof Element) || !ALLOWED_TAGS.has(node.tagName)) {
      // Unknown elements become their text; comments and the rest go.
      const text = node instanceof Element ? node.getAttribute("alt") ?? node.textContent ?? "" : "";
      node.replaceWith(document.createTextNode(text));
      continue;
    }
    const keep = ALLOWED_ATTRS[node.tagName] ?? [];
    for (const attr of Array.from(node.attributes)) {
      if (!keep.includes(attr.name)) node.removeAttribute(attr.name);
    }
    if (node.tagName === "A" && !/^(https?:|mailto:|#)/i.test(node.getAttribute("href") ?? "")) {
      node.removeAttribute("href");
    }
    if (node.tagName === "INPUT" && node.getAttribute("type") !== "checkbox") {
      node.replaceWith(document.createTextNode(""));
      continue;
    }
    sanitize(node);
  }
}

function setMarkdown(target: HTMLElement, markdown: string): void {
  const html = marked.parse(markdown, { async: false }) as string;
  const doc = new DOMParser().parseFromString(html, "text/html");
  sanitize(doc.body);
  target.replaceChildren(...Array.from(doc.body.childNodes));
}

// --- layout ------------------------------------------------------------------

// The product name (from the extension's displayName; see brand.ts).
const { product: PRODUCT, features: HOST_FEATURES } = hostSettings();

const app = document.getElementById("app")!;
app.innerHTML = `
  <header>
    <span class="model" title="Model"></span>
    <label class="critique" title="A critic agent reviews each answer before you see it; if it finds problems, the answer is fixed first. Saved in your cmcoder settings: applies to the terminal, the TUI and every VS Code window.">
      <input type="checkbox" disabled> Critique
    </label>
    <button class="history secondary" title="Past conversations in this project">History</button>
    <select class="mode" title="Permission mode (Shift+Tab in the CLI)"></select>
  </header>
  <section class="sessions" hidden></section>
  <section class="todos" hidden></section>
  <main class="log" aria-live="polite"></main>
  <section class="agents" hidden></section>
  <div class="progress" hidden role="status"><span class="spark">✻</span><span class="verb"></span><span class="detail"></span><span class="meta"></span></div>
  <div class="status" hidden></div>
  <footer>
    <div class="queued" hidden></div>
    <label class="context" hidden title="Send the active file, selection and problems with this message">
      <input type="checkbox" checked> <span></span>
    </label>
    <div class="commands" role="listbox" hidden></div>
    <div class="images" hidden></div>
    <textarea rows="3"></textarea>
    <div class="actions">
      <button class="attach secondary" title="Attach a file (@)">@</button>
      <button class="image secondary" title="Attach an image (or paste one with Ctrl+V, or drop it here)">Image</button>
      <input class="image-file" type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden>
      <span class="usage"></span>
      <button class="stop secondary" hidden title="Stop (Esc)">Stop</button>
      <button class="send">Send</button>
    </div>
  </footer>`;

const $ = <T extends HTMLElement>(sel: string) => app.querySelector(sel) as T;
const log = $<HTMLElement>(".log");
log.dataset.product = PRODUCT; // the empty screen shows the logo and this name
const input = $<HTMLTextAreaElement>("textarea");
input.placeholder = `Ask ${PRODUCT}… (Enter to send, Shift+Enter for a new line)`;
const sendButton = $<HTMLButtonElement>(".send");
const stopButton = $<HTMLButtonElement>(".stop");
const modeSelect = $<HTMLSelectElement>(".mode");
const critiqueBox = $<HTMLLabelElement>(".critique").querySelector("input") as HTMLInputElement;
const modelLabel = $<HTMLElement>(".model");
const usageLabel = $<HTMLElement>(".usage");
const statusLine = $<HTMLElement>(".status");
const progressLine = $<HTMLElement>(".progress");
const queuedBox = $<HTMLElement>(".queued");
const todosPanel = $<HTMLElement>(".todos");
const agentsPanel = $<HTMLElement>(".agents");
const sessionsPanel = $<HTMLElement>(".sessions");
const historyButton = $<HTMLButtonElement>(".history");
const attachButton = $<HTMLButtonElement>(".attach");
const imageButton = $<HTMLButtonElement>(".image");
const imageFile = $<HTMLInputElement>(".image-file");
const imagesStrip = $<HTMLElement>(".images");
const contextChip = $<HTMLLabelElement>(".context");
const commandsPopup = $<HTMLElement>(".commands");
const contextBox = contextChip.querySelector("input") as HTMLInputElement;
const contextText = contextChip.querySelector("span") as HTMLElement;

for (const mode of MODES) {
  const option = new Option(mode, mode);
  option.title = MODE_HELP[mode] ?? "";
  modeSelect.append(option);
}

// --- state -------------------------------------------------------------------

let busy = false;
let rewound: string | undefined; // the note to show once the rewound history is drawn
let ready = false;
let agentState: AgentState = "starting";
let reply: { el: HTMLElement; text: string } | undefined; // the streaming reply
let renderQueued = false;
const toolCards = new Map<string, HTMLElement>();
// What this cmcoder can do, from its system_init; unknown until one arrives
// (the page may have reloaded after it: then nothing is refused).
let features: string[] | undefined;
/** A control that needs both sides: this cmcoder (system_init.features) and the IDE (data-features). */
const can = (feature: string) => HOST_FEATURES.includes(feature) && features?.includes(feature) === true;
const permissionCards = new Map<string, HTMLElement>();
// Tool calls the user denied here: their card already says so.
const deniedHere = new Set<string>();

function setBusy(value: boolean): void {
  const was = busy;
  busy = value;
  sendButton.hidden = value;
  stopButton.hidden = !value;
  input.placeholder = value
    ? "Queue another message… (Esc to stop)"
    : `Ask ${PRODUCT}… (Enter to send, Shift+Enter for a new line)`;
  if (!value) showStatus();
  if (value && !was) startProgress();
  if (!value) stopProgress();
}

// --- the progress line: "✻ Considering… (12s · ↓ 1.2k tokens · Esc to interrupt)" ---

const SPARKS = ["·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢"];
const VERBS = ["Considering", "Pondering", "Thinking", "Working", "Reasoning", "Exploring", "Analysing", "Composing"];
// What the agent is doing, from the tool it runs.
const TOOL_VERBS: Record<string, string> = {
  Read: "Reading", Write: "Writing", Edit: "Editing", MultiEdit: "Editing", Bash: "Running",
  Grep: "Searching", Glob: "Searching", CodeSearch: "Searching", WebFetch: "Fetching",
  Task: "Delegating", Skill: "Using a skill",
};
const progress = { started: 0, tokens: 0, verb: "Considering", activity: "", detail: "", waiting: false, frame: 0 };
let progressTimer: number | undefined;

function startProgress(): void {
  Object.assign(progress, {
    started: Date.now(), tokens: 0, activity: "", detail: "", waiting: false, frame: 0,
    verb: VERBS[Math.floor(Math.random() * VERBS.length)],
  });
  progressLine.hidden = false;
  renderProgress();
  if (progressTimer === undefined) progressTimer = window.setInterval(tickProgress, 120);
}

function stopProgress(): void {
  progressLine.hidden = true;
  if (progressTimer !== undefined) window.clearInterval(progressTimer);
  progressTimer = undefined;
}

/** What it's doing now ("" = the turn's word), and a detail such as the tool call. */
function setActivity(activity = "", detail = ""): void {
  progress.activity = activity;
  progress.detail = detail;
  progress.waiting = false;
  if (busy) renderProgress();
}

function tickProgress(): void {
  progress.frame = (progress.frame + 1) % SPARKS.length;
  renderProgress();
}

function renderProgress(): void {
  const spark = progressLine.querySelector<HTMLElement>(".spark")!;
  spark.textContent = progress.waiting ? "✻" : SPARKS[progress.frame];
  progressLine.classList.toggle("waiting", progress.waiting);
  const verb = progress.waiting ? "Waiting for your answer" : progress.activity || progress.verb;
  progressLine.querySelector(".verb")!.textContent = `${verb}…`;
  progressLine.querySelector(".detail")!.textContent = progress.detail && !progress.waiting ? progress.detail : "";
  const parts = [duration(Date.now() - progress.started)];
  if (progress.tokens) parts.push(`↓ ${shortCount(progress.tokens)} tokens`);
  parts.push("Esc to interrupt");
  progressLine.querySelector(".meta")!.textContent = `(${parts.join(" · ")})`;
}

// --- images: pasted, dropped or picked; shown above the input until sent ----------

const MAX_IMAGES = 5; // as cmcoder takes per message
const MAX_IMAGE_BYTES = 20 * 1024 * 1024; // cmcoder makes larger ones smaller, up to this
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
let pendingImages: ImageAttachment[] = [];

function thumbnail(img: ImageAttachment): HTMLImageElement {
  const t = document.createElement("img");
  t.className = "thumb";
  t.alt = img.name || "image";
  t.title = img.name || "Pasted image";
  // Only the image types above, and base64 only: nothing else becomes a URL here.
  if (IMAGE_TYPES.has(img.mediaType) && /^[A-Za-z0-9+/]*={0,2}$/.test(img.data)) {
    t.src = `data:${img.mediaType};base64,${img.data}`;
  }
  return t;
}

function renderImages(): void {
  imagesStrip.hidden = pendingImages.length === 0;
  imagesStrip.replaceChildren(
    ...pendingImages.map((img, i) => {
      const item = el("div", "item");
      item.append(thumbnail(img));
      const remove = el("button", "secondary remove", "×");
      remove.title = "Remove this image";
      remove.onclick = () => {
        pendingImages.splice(i, 1);
        renderImages();
      };
      item.append(remove);
      return item;
    }),
  );
}

function addImage(img: ImageAttachment): void {
  if (!IMAGE_TYPES.has(img.mediaType)) {
    showStatus(`${img.name || "That"} isn't a PNG, JPEG, GIF or WebP image.`);
    return;
  }
  if (pendingImages.length >= MAX_IMAGES) {
    showStatus(`At most ${MAX_IMAGES} images per message.`);
    return;
  }
  pendingImages.push(img);
  renderImages();
}

function addImageFile(file: File): void {
  if (!IMAGE_TYPES.has(file.type)) {
    showStatus(`${file.name || "That"} isn't a PNG, JPEG, GIF or WebP image.`);
    return;
  }
  if (file.size > MAX_IMAGE_BYTES) {
    showStatus(`${file.name || "The image"} is larger than 20 MB.`);
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    const url = String(reader.result ?? "");
    addImage({ data: url.slice(url.indexOf(",") + 1), mediaType: file.type, name: file.name || "" });
  };
  reader.readAsDataURL(file);
}

function imageFiles(list: DataTransferItemList | FileList | null | undefined): File[] {
  const out: File[] = [];
  const items: (File | DataTransferItem)[] = list ? Array.from(list as ArrayLike<File | DataTransferItem>) : [];
  for (const item of items) {
    const file = item instanceof File ? item : item.kind === "file" ? item.getAsFile() : null;
    if (file && file.type.startsWith("image/")) out.push(file);
  }
  return out;
}

input.addEventListener("paste", (e) => {
  const files = imageFiles(e.clipboardData?.items);
  if (files.length) {
    e.preventDefault(); // not the file's name as text
    files.forEach(addImageFile);
  } else if (!e.clipboardData?.getData("text/plain")) {
    // Some browsers (JavaFX's) don't give the page the clipboard's image: ask the IDE.
    post({ kind: "pasteImage" });
  }
});
app.addEventListener("dragover", (e) => {
  if (Array.from(e.dataTransfer?.types ?? []).includes("Files")) e.preventDefault();
});
app.addEventListener("drop", (e) => {
  const files = imageFiles(e.dataTransfer?.files);
  if (!files.length) return;
  e.preventDefault();
  files.forEach(addImageFile);
});
imageButton.onclick = () => imageFile.click();
imageFile.onchange = () => {
  imageFiles(imageFile.files).forEach(addImageFile);
  imageFile.value = "";
};

// --- messages typed while it works: sent together when the turn ends ---------

const queue: { text: string; includeContext: boolean; images: ImageAttachment[] }[] = [];

function renderQueue(): void {
  queuedBox.hidden = queue.length === 0;
  queuedBox.replaceChildren(
    ...queue.map((q, i) => {
      const row = el("div", "item");
      row.append(el("span", "text", `↳ ${q.text}${q.images.length ? ` (+${q.images.length} image${q.images.length > 1 ? "s" : ""})` : ""}`));
      const remove = el("button", "secondary remove", "×");
      remove.title = "Don't send this";
      remove.onclick = () => {
        queue.splice(i, 1);
        renderQueue();
      };
      row.append(remove);
      return row;
    }),
  );
}

/** The turn ended: send what was queued, or (interrupted) give it back to edit. */
function flushQueue(interrupted: boolean): void {
  if (!queue.length) return;
  const items = queue.splice(0);
  renderQueue();
  const text = items.map((q) => q.text).filter(Boolean).join("\n\n");
  const images = items.flatMap((q) => q.images).slice(0, MAX_IMAGES);
  if (interrupted) {
    input.value = text + (input.value ? `\n\n${input.value}` : "");
    pendingImages = [...images, ...pendingImages].slice(0, MAX_IMAGES);
    renderImages();
    input.focus();
    return;
  }
  submit(text, items.some((q) => q.includeContext), images);
}

// A start that takes long says so, with the time, instead of looking stuck.
let startTimer: ReturnType<typeof setInterval> | undefined;
function watchStart(): void {
  const started = Date.now();
  if (startTimer !== undefined) clearInterval(startTimer);
  startTimer = setInterval(() => {
    if (agentState !== "starting") {
      clearInterval(startTimer);
      startTimer = undefined;
      return;
    }
    const s = Math.round((Date.now() - started) / 1000);
    if (s >= 10) showStatus(`Starting ${PRODUCT}… (${s} s; its log says what it's doing)`);
  }, 1000);
}

function showStatus(text?: string): void {
  statusLine.hidden = !text;
  statusLine.textContent = text ?? "";
}

// --- rendering helpers ---------------------------------------------------------

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

function el(tag: string, cls: string, text?: string): HTMLElement {
  const e = document.createElement(tag);
  e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function append(node: HTMLElement): HTMLElement {
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.append(node);
  if (atBottom) node.scrollIntoView({ block: "end" });
  return node;
}

function note(text: string, cls: string): void {
  append(el("div", `note ${cls}`, text));
}

function renderReply(): void {
  renderQueued = false;
  if (reply) {
    setMarkdown(reply.el, reply.text);
    log.scrollTop = log.scrollHeight;
  }
}

function queueRender(): void {
  if (!renderQueued) {
    renderQueued = true;
    requestAnimationFrame(renderReply);
  }
}

function clip(text: string, maxLines: number): string {
  const lines = text.replace(/\s+$/, "").split("\n");
  return lines.length > maxLines
    ? lines.slice(0, maxLines).join("\n") + `\n… ${lines.length - maxLines} more lines`
    : lines.join("\n");
}

// --- events from cmcoder ---------------------------------------------------------

// A subagent's steps, listed inside its Task call's card.
const subagentSteps = new Map<string, HTMLElement>();

function subagentStep(ev: ToolUse | ToolResult | PermissionDenied, parentId: string): void {
  const card = toolCards.get(parentId);
  if (ev.type === "tool_use") {
    if (!card) return;
    let steps = card.querySelector<HTMLDetailsElement>("details.steps");
    if (!steps) {
      steps = card.appendChild(document.createElement("details"));
      steps.className = "steps";
      steps.open = true; // folded when the subagent finishes
      steps.append(el("summary", "", "Subagent steps"));
    }
    subagentSteps.set(ev.id, steps.appendChild(el("div", "step", `● ${ev.label}`)));
    setActivity("Subagents working");
    return;
  }
  const row = subagentSteps.get(ev.id);
  subagentSteps.delete(ev.id);
  if (ev.type === "permission_denied") {
    deniedHere.delete(ev.id);
    row?.append(el("span", "error", " — denied"));
  } else if (ev.is_error) {
    row?.append(el("span", "error", ` — ${ev.content.split("\n")[0].slice(0, 200)}`));
  }
}

// --- the agent map: this turn's subagents, above the input ----------------------

const STATE_MARKS: Record<string, string> = {
  queued: "○", running: "◐", waiting: "⏸", stopping: "◑",
  done: "✓", limit: "✓", stopped: "■", failed: "✗",
};
const STATE_WORDS: Record<string, string> = {
  queued: "queued", running: "running", waiting: "waiting for permission",
  stopping: "stopping (writing its report)", done: "done", limit: "done (step limit reached)",
  stopped: "stopped", failed: "failed",
};
const ACTIVE = new Set(["queued", "running", "waiting", "stopping"]);
const TICKING = new Set(["running", "waiting", "stopping"]);
const agentRuns = new Map<string, { status: SubagentStatus; seen: number }>();
let agentTimer: number | undefined;

function shortCount(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k` : `${n}`;
}

function duration(ms: number): string {
  const s = Math.floor(ms / 1000);
  return s >= 60 ? `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s` : `${s}s`;
}

function agentSummary(s: SubagentStatus, live: boolean): string {
  const entry = agentRuns.get(s.id);
  const ms = live && entry && TICKING.has(s.state) ? s.elapsed_ms + (Date.now() - entry.seen) : s.elapsed_ms;
  const tools = `${s.tool_uses} tool${s.tool_uses === 1 ? "" : "s"}`;
  return [s.agent_type, `${s.steps}/${s.max_steps} steps`, tools, `${shortCount(s.tokens)} tokens`, duration(ms)].join(" · ");
}

function agentDetail(s: SubagentStatus): string {
  if (s.state === "queued") return `${s.agent_type} · queued`;
  const words = s.state === "running" || s.state === "done" ? "" : `${STATE_WORDS[s.state]} · `;
  return words + agentSummary(s, true);
}

function subagentStatus(s: SubagentStatus): void {
  agentRuns.set(s.id, { status: s, seen: Date.now() });
  const card = toolCards.get(s.id) ?? document.querySelector<HTMLElement>(`.tool[data-task="${CSS.escape(s.id)}"]`);
  if (card) {
    card.dataset.task = s.id;
    let line = card.querySelector<HTMLElement>(".substatus");
    if (!line) line = card.insertBefore(el("div", "substatus"), card.children[1] ?? null);
    line.className = `substatus ${s.state}`;
    line.textContent = `${STATE_MARKS[s.state]} ${STATE_WORDS[s.state]} · ${agentSummary(s, false)}`;
  }
  renderAgents();
  if (agentTimer === undefined) agentTimer = window.setInterval(renderAgents, 1000);
}

function renderAgents(): void {
  const runs = Array.from(agentRuns.values()).map((r) => r.status);
  const active = runs.filter((s) => ACTIVE.has(s.state));
  agentsPanel.hidden = runs.length === 0;
  if (!active.length && agentTimer !== undefined) {
    window.clearInterval(agentTimer);
    agentTimer = undefined;
  }
  if (!runs.length) return;
  const done = runs.length - active.length;
  const title = el("div", "title", `Subagents · ${active.length} active · ${done} finished `);
  const map = el("button", "secondary open-map", "Map");
  map.title = "Open the Agent Navigator: this turn's agents as a mind map";
  map.onclick = () => post({ kind: "openNavigator" });
  title.append(map);
  const rows = runs.map((s, i) => {
    const row = el("div", `agent ${s.state}`);
    row.title = "Show its card";
    row.onclick = () => document.querySelector(`.tool[data-task="${CSS.escape(s.id)}"]`)?.scrollIntoView({ block: "center" });
    const branch = i === runs.length - 1 ? "└─" : "├─";
    row.append(el("span", "name", `${branch} ${STATE_MARKS[s.state]} ${s.number}. ${s.description}`));
    row.append(el("span", "detail", agentDetail(s)));
    if (ACTIVE.has(s.state) && s.state !== "stopping") {
      const stop = el("button", "secondary stop-one", "Stop");
      stop.title = "Stop this subagent: it reports what it has found so far; the others go on";
      stop.onclick = (e) => {
        e.stopPropagation();
        stop.setAttribute("disabled", "");
        post({ kind: "stopSubagent", id: s.id });
      };
      row.append(stop);
    }
    const parts = [row];
    if (s.activity && TICKING.has(s.state)) parts.push(el("div", "activity", `└ ${s.activity}`));
    return parts;
  });
  agentsPanel.replaceChildren(title, ...rows.flat());
}

function endAgents(): void {
  // The turn ended: anything still running was stopped with it.
  for (const entry of agentRuns.values()) {
    if (ACTIVE.has(entry.status.state)) subagentStatus({ ...entry.status, state: "stopped", activity: "" });
  }
  agentRuns.clear();
  renderAgents();
}

function onEvent(ev: AgentEvent): void {
  if ((ev.type === "tool_use" || ev.type === "tool_result" || ev.type === "permission_denied") && ev.parent_tool_use_id) {
    subagentStep(ev, ev.parent_tool_use_id);
    return;
  }
  switch (ev.type) {
    case "system_init":
      // An older cmcoder sends neither: it can't take images.
      features = ev.features ?? [];
      modelLabel.textContent = ev.model;
      modelLabel.title = `${ev.model} (${ev.provider}) · ${ev.cwd} · ${PRODUCT} ${ev.version || "(an older version)"}`;
      modeSelect.value = ev.permission_mode;
      critiqueBox.checked = ev.critique ?? false;
      critiqueBox.disabled = false;
      break;
    case "critique_changed":
      critiqueBox.checked = ev.enabled;
      if (ev.source === "saved") note(`Critique ${ev.enabled ? "on" : "off"} (switched in another window).`, "info");
      break;
    case "assistant_delta":
      if (progress.activity || progress.detail) setActivity();
      if (!reply) reply = { el: append(el("div", "msg assistant")), text: "" };
      reply.text += ev.text;
      queueRender();
      break;
    case "reasoning_delta":
      if (progress.activity !== "Thinking") setActivity("Thinking");
      break;
    case "assistant_message":
      if (ev.text && !reply) reply = { el: append(el("div", "msg assistant")), text: "" };
      if (reply) {
        reply.text = ev.text || reply.text;
        renderReply();
      }
      reply = undefined;
      if (ev.tool_calls.length) setActivity();
      break;
    case "tool_use":
      if (ev.name !== "TodoWrite") toolCard(ev);
      if (ev.input.subagent_type === "critic") setActivity("Reviewing the answer");
      else setActivity(TOOL_VERBS[ev.name] ?? "Working", ev.name === "TodoWrite" ? "" : ev.label);
      break;
    case "tool_result":
      toolResult(ev);
      setActivity();
      break;
    case "tool_progress":
      bashProgress(ev);
      break;
    case "subagent_status":
      subagentStatus(ev);
      break;
    case "permission_request":
      permissionCard(ev);
      break;
    case "permission_denied":
      if (!deniedHere.delete(ev.id)) note(`Denied: ${ev.reason}`, "warn");
      break;
    case "todo_update":
      renderTodos(ev.todos);
      break;
    case "usage": {
      const k = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : `${n}`);
      usageLabel.textContent = ev.context_window
        ? `${k(ev.prompt_tokens)} / ${k(ev.context_window)} tokens`
        : `${k(ev.prompt_tokens)} tokens`;
      progress.tokens += ev.completion_tokens;
      if (busy) renderProgress();
      break;
    }
    case "warning":
      note(`⚠ ${ev.message}`, "warn");
      break;
    case "error":
      note(`✗ ${ev.message}${ev.hint ? `\n${ev.hint}` : ""}`, "error");
      break;
    case "review_result":
      reviewNote(ev);
      if (!ev.final) setActivity("Fixing what the reviewer found");
      break;
    case "images_describing":
      setActivity(`Reading the image${ev.count > 1 ? "s" : ""}`, `with ${ev.model}`);
      break;
    case "images_described":
      setActivity();
      note(`◦ ${ev.model} described ${ev.count === 1 ? "the image" : `${ev.count} images`} for the model, which can't see images`, "info");
      break;
    case "code_context": {
      const where = ev.items.slice(0, 4).map((i) => `${i.path}:${i.start_line}-${i.end_line}`).join(", ");
      const more = ev.items.length > 4 ? ` and ${ev.items.length - 4} more` : "";
      note(`◦ Added code from the index: ${where}${more} (≈${ev.tokens} tokens)`, "info");
      break;
    }
    case "compacted":
      note(
        `✻ ${ev.trigger === "auto" ? "Context nearly full: compacted" : "Compacted"} the conversation ` +
          `(summarised ${ev.summarized_messages} earlier messages with ${ev.model}).`,
        "info",
      );
      break;
    case "mode_changed":
      modeSelect.value = ev.mode;
      break;
    case "history":
      for (const item of ev.messages) {
        if (item.role === "user") append(el("div", "msg user", item.text));
        else if (item.role === "tool") append(el("div", "tool")).append(el("div", "label", `● ${item.text}`));
        else setMarkdown(append(el("div", "msg assistant")), item.text);
      }
      if (rewound) note(rewound, "info");
      else if (ev.messages.length) note("Resumed this conversation.", "info");
      rewound = undefined;
      break;
    case "rewind_points":
      note("Choose the message to go back to in the list at the top of the window.", "info");
      break;
    case "rewound": {
      const restored = ev.actions.filter((a) => a.action === "restored").length;
      const deleted = ev.actions.filter((a) => a.action.startsWith("deleted")).length;
      const files = ev.code ? ` Files: ${restored} restored, ${deleted} deleted (changes made by Bash commands are not undone).` : "";
      if (ev.conversation) {
        log.replaceChildren(); // the history that follows redraws it
        toolCards.clear();
        bashRuns.clear();
        permissionCards.clear();
        rewound = `Rewound the conversation.${files}`;
        if (ev.prompt) {
          input.value = ev.prompt;
          input.focus();
        }
      } else {
        note(`Rewound the code.${files}`, "info");
      }
      break;
    }
    case "session_list":
      renderSessions(ev.sessions);
      break;
    case "command_list":
      commands = ev.commands;
      updateCommands();
      break;
    case "model_changed":
      modelLabel.textContent = ev.model;
      break;
    case "result":
      if (reply) renderReply();
      reply = undefined;
      for (const card of permissionCards.values()) answered(card, "Cancelled");
      permissionCards.clear();
      updateAskQueue();
      endBashRuns();
      endAgents();
      if (ev.subtype === "interrupted") note(`Interrupted. What should ${PRODUCT} do instead?`, "warn");
      else if (ev.subtype === "max_turns") note(`Stopped: ${ev.result}`, "warn");
      setBusy(false);
      input.focus();
      flushQueue(ev.subtype === "interrupted");
      break;
  }
}

/** Critique: what the critic decided about the answer. */
function reviewNote(ev: ReviewResult): void {
  const n = ev.issues.length;
  const problems = `${n} problem${n === 1 ? "" : "s"}`;
  let head: string;
  let cls = "warn";
  if (!ev.final) head = `↺ The reviewer found ${problems}; fixing ${n === 1 ? "it" : "them"} (review ${ev.round}/${ev.max_rounds}):`;
  else if (ev.verdict === "pass") {
    head = `✓ Reviewed: ${ev.summary}`;
    cls = "info review-pass";
  } else if (ev.verdict === "fail") head = `⚠ Not validated: after ${ev.round} review${ev.round === 1 ? "" : "s"} the reviewer still found ${problems}:`;
  else head = `⚠ Not reviewed: ${ev.summary}`;
  const box = append(el("div", `note review ${cls}`));
  box.append(el("div", "head", head));
  if (ev.verdict !== "pass" && ev.verdict !== "none") {
    const list = el("ul", "issues");
    for (const i of ev.issues) {
      const item = el("li", `issue ${i.severity}`, `[${i.severity}] ${i.problem}${i.where ? ` (${i.where})` : ""}`);
      if (i.fix) item.append(el("div", "fix", `fix: ${i.fix}`));
      list.append(item);
    }
    if (!n && ev.summary) list.append(el("li", "issue", ev.summary));
    box.append(list);
  }
}

// --- tool cards ------------------------------------------------------------------

function toolCard(ev: ToolUse): void {
  if (ev.name === "Bash") {
    bashRun(ev.id, commandOf(ev));
    return;
  }
  const card = el("div", "tool");
  card.append(el("div", "label", `● ${ev.label}`));
  toolCards.set(ev.id, append(card));
}

function toolResult(ev: ToolResult): void {
  if (ev.name === "TodoWrite") return;
  if (ev.name === "Bash") {
    finishBash(bashRuns.get(ev.id) ?? bashRun(ev.id, ""), ev);
    return;
  }
  const card = toolCards.get(ev.id) ?? append(el("div", "tool"));
  toolCards.delete(ev.id);
  const steps = card.querySelector<HTMLDetailsElement>("details.steps");
  if (steps) {
    // A finished subagent: fold its steps away (a click shows them again).
    const n = steps.querySelectorAll(".step").length;
    steps.open = false;
    steps.querySelector("summary")!.textContent = `${n} subagent step${n === 1 ? "" : "s"}`;
  }
  if (ev.is_error) {
    card.append(el("pre", "result error", clip(ev.content, 4)));
    return;
  }
  card.append(el("div", "result", `└ ${ev.summary || "done"}`));
  // A browser screenshot (the Browser tool): shown small, a click enlarges it.
  for (const url of ev.images ?? []) {
    if (!url.startsWith("data:image/")) continue;
    const img = document.createElement("img");
    img.className = "shot";
    img.src = url;
    img.alt = "Screenshot of the page";
    img.title = "Click to enlarge";
    img.onclick = () => img.classList.toggle("big");
    card.append(img);
  }
}

// --- the Bash card: the command, its output as it comes, how it ended -----------------

/** How many lines of output stay visible; "Show all" opens the rest. */
const BASH_TAIL = 8;
/** What the card keeps of a live output (the engine keeps more; the final result replaces it). */
const BASH_LIVE_CHARS = 400_000;

interface BashRun {
  id: string;
  command: string;
  card: HTMLElement;
  badge: HTMLElement;
  out: HTMLElement;
  foot: HTMLElement;
  stop: HTMLButtonElement;
  more: HTMLButtonElement;
  copy: HTMLButtonElement;
  state: "running" | "waiting" | "stopping" | "done";
  text: string; // the output so far (live), or all of it (done)
  started: number; // Date.now() when it began, as far as we know
  expanded: boolean;
  total?: number; // lines in the whole output, from the engine
  result?: string; // "ok" | "fail" | "stopped" | "interrupted" once it ended
  ended?: string; // the badge text of an ended command
}

const bashRuns = new Map<string, BashRun>();
let bashTimer: number | undefined;
let bashRenderQueued = false;

/** The command of a Bash call: its input, or (history, an older cmcoder) its label. */
function commandOf(ev: ToolUse): string {
  const c = (ev.input as Record<string, unknown>).command;
  if (typeof c === "string") return c;
  const m = /^Bash\(([\s\S]*)\)$/.exec(ev.label);
  return m ? m[1] : ev.label;
}

function bashRun(id: string, command: string): BashRun {
  const card = el("div", "tool bash");
  const head = el("div", "bash-head");
  head.append(el("span", "prompt", "$"));
  const code = el("code", "bash-cmd");
  code.append(renderShell(command.split("\n")[0] + (command.includes("\n") ? " …" : "")));
  code.title = command;
  const badge = el("span", "bash-badge");
  head.append(code, badge);
  const out = el("pre", "bash-out");
  out.hidden = true;
  const foot = el("div", "bash-foot");
  const stop = el("button", "secondary stop-cmd", "Stop this command") as HTMLButtonElement;
  stop.title = "Ends this command only; the turn goes on with what it printed";
  const more = el("button", "link more") as HTMLButtonElement;
  const copy = el("button", "link copy", "Copy output") as HTMLButtonElement;
  foot.append(stop, more, copy);
  card.append(head, out, foot);
  const run: BashRun = {
    id, command, card, badge, out, foot, stop, more, copy,
    state: "running", text: "", started: Date.now(), expanded: false,
  };
  stop.onclick = () => {
    if (run.state !== "running") return;
    run.state = "stopping";
    post({ kind: "stopTool", toolUseId: id });
    renderBash(run);
  };
  more.onclick = () => {
    run.expanded = !run.expanded;
    renderBash(run);
  };
  copy.onclick = () => copyText(cleanOutput(run), copy);
  append(card);
  bashRuns.set(id, run);
  renderBash(run);
  if (bashTimer === undefined) bashTimer = window.setInterval(tickBash, 1000);
  return run;
}

/** Output as a person reads it: no colour codes, a carriage-return line shows its last state. */
function plainOutput(text: string): string {
  return text
    .replace(/\u001b\[[0-9;?]*[ -/]*[@-~]/g, "")
    .replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, "")
    .split("\n")
    .map((line) => (line.includes("\r") ? (line.split("\r").filter(Boolean).pop() ?? "") : line))
    .join("\n");
}

/** What the card shows of a finished command's text: the engine's trailer lines are the badge's job. */
function cleanOutput(run: BashRun): string {
  let text = plainOutput(run.text);
  if (run.state === "done" && run.result !== "fail-other") {
    text = text
      .replace(/^\(The user stopped this command[^\n]*\n?/, "")
      .replace(/(?:^|\n\n?)Exit code -?\d+\s*$/, "");
  }
  text = text.replace(/\s+$/, "");
  return text === "(no output)" ? "" : text;
}

function renderBash(run: BashRun): void {
  const { state } = run;
  run.card.dataset.state = run.state === "done" ? (run.result ?? "ok") : state;
  run.card.classList.toggle("running", state === "running" || state === "stopping");
  // The badge.
  if (state === "done" || run.ended) {
    run.badge.textContent = run.ended ?? "";
    run.badge.className = `bash-badge ${run.result ?? ""}`;
  } else {
    const word = state === "waiting" ? "waiting for your answer" : state === "stopping" ? "stopping…" : "running";
    const time = state === "waiting" ? "" : ` · ${duration(Date.now() - run.started)}`;
    run.badge.textContent = `${word}${time}`;
    run.badge.className = `bash-badge ${state}`;
  }
  // The output: the last lines, or all of them.
  const text = cleanOutput(run);
  const lines = text ? text.split("\n") : [];
  const total = Math.max(run.total ?? 0, lines.length);
  const folded = !run.expanded && lines.length > BASH_TAIL;
  run.out.hidden = lines.length === 0 && state !== "done";
  run.out.classList.toggle("empty", lines.length === 0);
  if (lines.length === 0) run.out.textContent = state === "done" ? "(no output)" : "";
  else {
    const shown = folded ? lines.slice(-BASH_TAIL) : lines;
    run.out.textContent = (folded ? `… ${lines.length - BASH_TAIL} earlier lines\n` : "") + shown.join("\n");
  }
  run.out.classList.toggle("expanded", run.expanded);
  if (run.expanded && state !== "done") run.out.scrollTop = run.out.scrollHeight;
  // The buttons.
  run.stop.hidden = !(state === "running" && can("stopTool"));
  run.more.hidden = lines.length <= BASH_TAIL;
  run.more.textContent = run.expanded ? "Show less" : `Show all (${total} lines)`;
  run.copy.hidden = lines.length === 0;
  run.foot.hidden = run.stop.hidden && run.more.hidden && run.copy.hidden;
}

function queueBashRender(): void {
  if (bashRenderQueued) return;
  bashRenderQueued = true;
  requestAnimationFrame(() => {
    bashRenderQueued = false;
    for (const r of bashRuns.values()) if (r.state !== "done") renderBash(r);
  });
}

/** Once a second: the running time of every command still going. */
function tickBash(): void {
  let live = false;
  for (const run of bashRuns.values()) {
    if (run.state === "done") continue;
    live = true;
    renderBash(run);
  }
  if (!live && bashTimer !== undefined) {
    window.clearInterval(bashTimer);
    bashTimer = undefined;
  }
}

function bashProgress(ev: ToolProgress): void {
  const run = bashRuns.get(ev.id);
  if (!run || run.state === "done" || ev.parent_tool_use_id) return;
  run.text = (run.text + ev.text).slice(-BASH_LIVE_CHARS);
  run.started = Date.now() - ev.elapsed_ms;
  if (run.state === "waiting") run.state = "running"; // output means it is running
  queueBashRender();
}

/** The permission question for a Bash call opened or closed: it waits for the answer, or runs. */
function bashWaiting(toolUseId: string, waiting: boolean): void {
  const run = bashRuns.get(toolUseId);
  if (!run || run.state === "done" || run.state === "stopping") return;
  run.state = waiting ? "waiting" : "running";
  if (!waiting) run.started = Date.now(); // the time it ran, not the time it waited
  renderBash(run);
}

function elapsed(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
  return duration(ms);
}

function finishBash(run: BashRun, ev: ToolResult): void {
  const code = ev.exit_code ?? null;
  run.state = "done";
  run.text = ev.content;
  run.total = ev.total_lines ?? undefined;
  let text: string;
  if (ev.stopped) {
    run.result = "stopped";
    text = "■ stopped";
  } else if (!ev.is_error) {
    run.result = "ok";
    text = code === null ? "✓ done" : `✓ exit ${code}`;
  } else if (code !== null) {
    run.result = "fail";
    text = `✗ exit ${code}`;
  } else {
    // Timed out, the shell ended, or it was denied: the content says which, as it is.
    run.result = "fail-other";
    text = `✗ ${ev.summary || "failed"}`;
  }
  if (ev.duration_ms !== null && ev.duration_ms !== undefined) text += ` · ${elapsed(ev.duration_ms)}`;
  run.ended = text;
  if (run.result === "fail-other") run.card.dataset.state = "fail";
  renderBash(run);
}

/** The turn ended with commands still shown as running: they did not finish, whatever the reason. */
function endBashRuns(): void {
  for (const run of bashRuns.values()) {
    if (run.state === "done") continue;
    run.state = "done";
    run.result = "interrupted";
    run.ended = "■ interrupted";
    renderBash(run);
  }
}

/** Copy to the clipboard: the browser's API where the page may use it, else the old way. */
function copyText(text: string, button: HTMLElement): void {
  const said = (ok: boolean) => {
    const was = button.dataset.label ?? button.textContent ?? "";
    button.dataset.label = was;
    button.textContent = ok ? "Copied" : "Could not copy";
    window.setTimeout(() => (button.textContent = was), 1500);
  };
  const fallback = () => {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {
      ok = false;
    }
    area.remove();
    said(ok);
  };
  if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(() => said(true), fallback);
  else fallback();
}

// --- the consent card: what is asked, why, where, and the ways to answer ---------------

/** The last part of a path, for a headline. */
function base(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() ?? path;
}

function askHeadline(ev: PermissionRequest): string {
  const i = ev.input as Record<string, unknown>;
  const path = typeof i.file_path === "string" ? i.file_path : (ev.change?.path ?? "");
  switch (ev.name) {
    case "Bash":
      return "Run a command";
    case "Write":
      return ev.change && ev.change.before === null ? `Create ${base(path)}` : `Write ${base(path)}`;
    case "Edit":
    case "MultiEdit":
      return `Edit ${base(path)}`;
    default:
      return `Allow ${ev.label}?`;
  }
}

/** The diff of a file change: line numbers, the changed lines and a little around them. */
function diffBlock(path: string, before: string | null, after: string, numbers: boolean): HTMLElement {
  const view = lineDiff(before, after);
  const box = el("div", `ask-diff${numbers ? "" : " plain"}`);
  const head = el("div", "diff-head");
  const name = el("span", "diff-path", path);
  name.title = path;
  head.append(name);
  if (before === null) head.append(el("span", "diff-new", "new file"));
  if (view.added || !view.removed) head.append(el("span", "diff-add", `+${view.added}`));
  if (view.removed || !view.added) head.append(el("span", "diff-del", `−${view.removed}`));
  box.append(head);
  const rows = el("div", "diff-rows");
  if (!view.rows.length) rows.append(el("div", "drow gap", "No change"));
  for (const row of view.rows) {
    if (row.kind === "gap") {
      rows.append(el("div", "drow gap", `⋯ ${row.hidden} line${row.hidden === 1 ? "" : "s"} not shown`));
      continue;
    }
    const line = el("div", `drow ${row.kind}`);
    line.append(
      el("span", "no", row.oldNo === undefined ? "" : String(row.oldNo)),
      el("span", "no", row.newNo === undefined ? "" : String(row.newNo)),
      el("span", "sign", row.kind === "add" ? "+" : row.kind === "del" ? "−" : " "),
      el("span", "txt", row.text),
    );
    rows.append(line);
  }
  box.append(rows);
  return box;
}

/** What the request is about: the command, the change, or the input as it is. */
function askBody(ev: PermissionRequest): HTMLElement | null {
  const i = ev.input as Record<string, unknown>;
  const s = (v: unknown) => (typeof v === "string" ? v : "");
  if (ev.name === "Bash") {
    const box = el("div", "ask-cmd");
    const line = el("div", "cmd-line");
    const code = el("code", "bash-cmd");
    code.append(renderShell(s(i.command)));
    line.append(el("span", "prompt", "$"), code);
    box.append(line);
    if (ev.cwd) {
      const where = el("div", "cmd-cwd", `in ${ev.cwd}`);
      where.title = ev.cwd;
      box.append(where);
    }
    return box;
  }
  if (ev.change) return diffBlock(ev.change.path, ev.change.before, ev.change.after, true);
  if (ev.name === "Edit" && s(i.file_path)) return diffBlock(s(i.file_path), s(i.old_string), s(i.new_string), false);
  if (ev.name === "Write" && s(i.file_path)) return diffBlock(s(i.file_path), null, s(i.content), false);
  return el("pre", "preview", clip(JSON.stringify(i, null, 2), 20));
}

type Scope = "once" | "project" | "session";

function permissionCard(ev: PermissionRequest): void {
  progress.waiting = true;
  if (busy) renderProgress();
  const risky = ev.high_risk === true;
  const card = el("div", `permission${risky ? " risk" : ""}`);
  card.dataset.toolUseId = ev.tool_use_id;

  // The headline: what it wants to do, who asks, and (with several waiting) which one this is.
  const head = el("div", "ask-head");
  head.append(el("span", "ask-icon", risky ? "⚠" : "?"));
  head.append(el("span", "ask-title", askHeadline(ev)));
  if (ev.agent) head.append(el("span", "ask-agent", `from ${ev.agent}`));
  head.append(el("span", "ask-count"));
  card.append(head);

  // Why it is asked.
  const why = ev.reasons?.length ? ev.reasons : ev.reason ? [ev.reason] : [];
  if (why.length) {
    const chips = el("div", "ask-chips");
    for (const reason of why) chips.append(el("span", "chip", reason));
    card.append(chips);
  }

  const body = askBody(ev);
  if (body) card.append(body);

  if (risky) {
    const parts = ev.name === "Bash" ? dangerousParts(String((ev.input as Record<string, unknown>).command ?? "")) : [];
    const note = el("div", "ask-note", "Asked every time: this can't be allowed again for the session or saved as a rule.");
    if (parts.length) note.append(el("div", "danger-parts", `Look at: ${parts.join("  ·  ")}`));
    card.append(note);
  }

  const answer = (allow: boolean, scope: Scope, feedback = "") => {
    post({
      kind: "permission",
      requestId: ev.request_id,
      allow,
      remember: allow && scope === "project",
      ...(allow && scope === "session" ? { rememberSession: true } : {}),
      ...(allow ? {} : { feedback: feedback.trim() }),
    });
    permissionCards.delete(ev.request_id);
    if (!allow) deniedHere.add(ev.tool_use_id);
    else bashWaiting(ev.tool_use_id, false);
    const said = !allow
      ? feedback.trim()
        ? `Denied · “${feedback.trim().slice(0, 80)}”`
        : "Denied"
      : scope === "session"
        ? "Allowed for this session"
        : scope === "project"
          ? "Allowed · rule saved for this project"
          : "Allowed";
    answered(card, said);
  };

  // "Always allow" shows the rule first, then asks where it should hold.
  const always = el("div", "ask-always");
  always.hidden = true;
  const rule = el("div", "ask-rule");
  rule.append(document.createTextNode("Allow from now on: "), el("code", "", ev.suggested_rule));
  always.append(rule);
  const where = el("div", "buttons");
  const sessionRules = can("sessionRules");
  if (sessionRules) {
    const session = el("button", "", "For this session");
    session.title = "Until you close this conversation; nothing is saved";
    session.onclick = () => answer(true, "session");
    where.append(session);
  }
  const project = el("button", sessionRules ? "secondary" : "", "In this project");
  project.title = `Saves ${ev.suggested_rule} in this project's settings`;
  project.onclick = () => answer(true, "project");
  const never = el("button", "secondary", "Cancel");
  never.onclick = () => {
    always.hidden = true;
    actions.hidden = false;
  };
  where.append(project, never);
  always.append(where);

  // A note for the model when denying.
  const deny = el("div", "ask-deny");
  deny.hidden = true;
  const feedback = document.createElement("input");
  feedback.type = "text";
  feedback.placeholder = `Tell ${PRODUCT} what to do instead`;
  const send = el("button", "secondary", "Deny and send");
  send.onclick = () => answer(false, "once", feedback.value);
  feedback.onkeydown = (e) => {
    if (e.key === "Enter") answer(false, "once", feedback.value);
  };
  deny.append(feedback, send);

  const actions = el("div", "ask-actions");
  const allow = el("button", risky ? "danger" : "", risky ? "Allow once" : "Allow");
  allow.onclick = () => answer(true, "once");
  actions.append(allow);
  if (ev.can_remember && !risky) {
    const ever = el("button", "secondary", "Always allow…");
    ever.onclick = () => {
      always.hidden = false;
      actions.hidden = true;
    };
    actions.append(ever);
  }
  const no = el("button", "secondary", "Deny");
  no.onclick = () => answer(false, "once");
  actions.append(no);
  const note = el("button", "link", "Deny with a note…");
  note.onclick = () => {
    deny.hidden = false;
    note.hidden = true;
    feedback.focus();
  };
  actions.append(note);
  if (ev.change) {
    const show = el("button", "link", "Open in diff editor");
    show.title = "Open the proposed change in the IDE's diff editor";
    show.onclick = () => post({ kind: "showDiff", requestId: ev.request_id });
    actions.append(show);
  }
  card.append(actions, always, deny);
  permissionCards.set(ev.request_id, append(card));
  updateAskQueue();
  bashWaiting(ev.tool_use_id, true);
}

/** With several requests waiting, each says which it is. */
function updateAskQueue(): void {
  const cards = Array.from(permissionCards.values());
  cards.forEach((card, i) => {
    const count = card.querySelector<HTMLElement>(".ask-count");
    if (count) count.textContent = cards.length > 1 ? `${i + 1} of ${cards.length} waiting` : "";
  });
}

function answered(card: HTMLElement, text: string): void {
  progress.waiting = false;
  // Only what was asked stays; the ways to answer go.
  card.querySelectorAll(".ask-actions, .ask-always, .ask-deny, .ask-note, button, input").forEach((e) => e.remove());
  card.querySelector(".ask-count")?.remove();
  card.classList.add("answered");
  card.append(el("div", "answer", `└ ${text}`));
  updateAskQueue();
}

function renderTodos(todos: Record<string, unknown>[]): void {
  todosPanel.replaceChildren();
  todosPanel.hidden = todos.length === 0;
  if (!todos.length) return;
  const done = todos.filter((t) => t.status === "completed").length;
  todosPanel.append(el("div", "title", `Todo list · ${done}/${todos.length} done`));
  for (const t of todos) {
    const status = String(t.status);
    todosPanel.append(el("div", `todo ${status}`, `${MARKS[status] ?? "☐"} ${String(t.content ?? "")}`));
  }
}

// --- input ---------------------------------------------------------------------

function send(): void {
  const text = input.value.trim();
  if (!text && !pendingImages.length) return;
  // Never ignore Send silently: say why it didn't go (the text and images stay).
  if (!ready) {
    showStatus(
      agentState === "exited"
        ? `Not sent: ${PRODUCT} isn't running. Click Restart in the chat.`
        : `Not sent yet: ${PRODUCT} is still starting. Press Enter again in a moment.`,
    );
    return;
  }
  if (pendingImages.length && features && !features.includes("images")) {
    const why = `Not sent: this ${PRODUCT} program is too old for images. Update it, or remove the images to send the text.`;
    showStatus(why);
    note(`✗ ${why}`, "error");
    return;
  }
  showStatus();
  const images = pendingImages;
  pendingImages = [];
  renderImages();
  const includeContext = !contextChip.hidden && contextBox.checked;
  input.value = "";
  commandsPopup.hidden = true;
  contextBox.checked = true; // turning it off counts for one message
  if (busy) {
    queue.push({ text, includeContext, images }); // sent when this turn ends
    renderQueue();
    return;
  }
  submit(text, includeContext, images);
}

function submit(text: string, includeContext: boolean, images: ImageAttachment[] = []): void {
  const bubble = append(el("div", "msg user", text));
  if (images.length) {
    const thumbs = el("div", "thumbs");
    thumbs.append(...images.map(thumbnail));
    bubble.append(thumbs);
  }
  if (includeContext) bubble.append(el("div", "attached", `📎 ${contextText.textContent}`));
  setBusy(true);
  post(images.length ? { kind: "send", text, includeContext, images } : { kind: "send", text, includeContext });
}

function renderSessions(sessions: { id: string; title: string; updated: number; messages: number }[]): void {
  sessionsPanel.replaceChildren(el("div", "title", "Past conversations"));
  if (!sessions.length) sessionsPanel.append(el("div", "empty", "None yet in this project."));
  for (const s of sessions.slice(0, 50)) {
    const row = el("button", "session secondary");
    row.append(el("span", "name", s.title || "(untitled)"));
    row.append(el("span", "when", `${new Date(s.updated * 1000).toLocaleString()} · ${s.messages} messages`));
    row.onclick = () => {
      sessionsPanel.hidden = true;
      post({ kind: "resume", id: s.id });
    };
    sessionsPanel.append(row);
  }
  sessionsPanel.hidden = false;
}

// --- slash-command completion ---------------------------------------------------

let commands: CommandInfo[] = [];
let matches: CommandInfo[] = [];
let selected = 0;

/** The `/name` being typed (nothing after it yet), or undefined. */
function typedCommand(): string | undefined {
  const m = /^\/([^\s]*)$/.exec(input.value);
  return m ? m[1] : undefined;
}

function updateCommands(): void {
  const typed = typedCommand();
  matches = typed === undefined ? [] : commands.filter((c) => c.name.startsWith(typed)).slice(0, 12);
  selected = Math.min(selected, Math.max(matches.length - 1, 0));
  commandsPopup.replaceChildren(
    ...matches.map((c, i) => {
      const row = el("div", i === selected ? "command selected" : "command");
      row.setAttribute("role", "option");
      row.append(el("span", "name", `/${c.name}`));
      if (c.argument_hint) row.append(el("span", "hint", ` ${c.argument_hint}`));
      const where = c.origin === "project" ? " (project)" : c.origin === "mcp" ? " (MCP)" : "";
      row.append(el("span", "description", `${c.description}${where}`));
      row.onmousedown = (e) => {
        e.preventDefault();
        acceptCommand(i);
      };
      return row;
    }),
  );
  commandsPopup.hidden = matches.length === 0;
}

function acceptCommand(i: number): void {
  const c = matches[i];
  if (!c) return;
  input.value = `/${c.name} `;
  matches = [];
  commandsPopup.hidden = true;
  input.focus();
}

input.addEventListener("input", () => {
  if (input.value === "/") post({ kind: "listCommands" }); // fresh: files may have changed
  updateCommands();
});

input.addEventListener("keydown", (e) => {
  if (!commandsPopup.hidden && matches.length) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      selected = (selected + (e.key === "ArrowDown" ? 1 : matches.length - 1)) % matches.length;
      updateCommands();
      return;
    }
    if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && !e.isComposing && matches[selected]?.name !== typedCommand())) {
      e.preventDefault();
      acceptCommand(selected);
      return;
    }
    if (e.key === "Escape") {
      e.stopPropagation();
      commandsPopup.hidden = true;
      return;
    }
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  }
});
sendButton.onclick = send;
stopButton.onclick = () => post({ kind: "interrupt" });
attachButton.onclick = () => post({ kind: "attachFile" });
historyButton.onclick = () => {
  if (!sessionsPanel.hidden) sessionsPanel.hidden = true;
  else if (!busy) post({ kind: "listSessions" });
};
modeSelect.onchange = () => post({ kind: "setMode", mode: modeSelect.value });
critiqueBox.onchange = () => post({ kind: "setCritique", enabled: critiqueBox.checked });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && busy) post({ kind: "interrupt" });
});
// Links in replies open in the browser (only http/https), never inside the panel.
document.addEventListener("click", (e) => {
  const a = (e.target as HTMLElement).closest("a");
  if (a) {
    e.preventDefault();
    post({ kind: "openLink", href: a.getAttribute("href") ?? "" });
  }
});

// --- messages from the extension ----------------------------------------------

window.addEventListener("message", (e: MessageEvent<ToWebview>) => {
  if (!fromHost(e)) {
    // Say so, once, if it looked like the IDE's: a chat that hears nothing looks stuck.
    if (!warnedForeign && typeof e.data?.kind === "string") {
      warnedForeign = true;
      panelError(`ignored a message from a window that isn't the IDE (origin ${e.origin || "unknown"})`);
    }
    return;
  }
  const m = e.data;
  switch (m.kind) {
    case "event":
      onEvent(m.event);
      break;
    case "state":
      ready = m.state === "ready";
      agentState = m.state;
      post({ kind: "panelState", state: m.state }); // the IDE's log: messages reach this page
      if (m.state !== "ready") critiqueBox.disabled = true; // until the next system_init
      if (m.state === "starting") {
        modelLabel.textContent = `Starting ${PRODUCT}…`;
        showStatus(`Starting ${PRODUCT}…`);
        watchStart();
      } else if (m.state === "ready") showStatus();
      else {
        modelLabel.textContent = `${PRODUCT} stopped`;
        endBashRuns(); // whatever was running went with it
        setBusy(false);
        showStatus();
        const box = el("div", "note error", m.message ?? `${PRODUCT} stopped.`);
        const restart = el("button", "", "Restart");
        restart.onclick = () => {
          restart.remove();
          post({ kind: "restart" });
        };
        box.append(document.createElement("br"), restart);
        append(box);
      }
      break;
    case "reset":
      sessionsPanel.hidden = true;
      log.replaceChildren();
      renderTodos([]);
      toolCards.clear();
      bashRuns.clear();
      permissionCards.clear();
      agentRuns.clear();
      renderAgents();
      queue.splice(0);
      renderQueue();
      reply = undefined;
      usageLabel.textContent = "";
      setBusy(false);
      break;
    case "image":
      addImage({ data: m.data, mediaType: m.mediaType, name: m.name });
      input.focus();
      break;
    case "prefill":
      input.value = m.text + input.value;
      input.focus();
      break;
    case "context":
      contextChip.hidden = !m.label;
      contextText.textContent = m.label ?? "";
      break;
    case "permissionAnswered": {
      const card = permissionCards.get(m.requestId);
      if (card) {
        permissionCards.delete(m.requestId);
        if (m.text === "Denied" && card.dataset.toolUseId) deniedHere.add(card.dataset.toolUseId);
        else if (card.dataset.toolUseId) bashWaiting(card.dataset.toolUseId, false);
        answered(card, m.text);
      }
      break;
    }
  }
});

post({ kind: "ready" });
input.focus();
