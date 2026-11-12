// The Jobs panel of the dock: background jobs (a dev server, a test watcher) that the agent started
// with Bash `run_in_background`. Its line says how many are running and which; open, it lists every
// job with how long it has run, the last lines it printed, and a Stop button. The engine sends the
// whole list (a `jobs_changed` event) whenever one starts, ends or prints; Stop is the message
// `/jobs stop <n>`, which the engine runs at once even while a request is running, so no IDE needs
// anything new for it. Rows are made once and updated in place, so a click is never lost to a redraw.

import type { AgentEvent } from "../protocol";
import type { DockPanel } from "./dock";

export type JobsChanged = Extract<AgentEvent, { type: "jobs_changed" }>;
type Job = JobsChanged["jobs"][number];

export interface JobsHost {
  /** End one job (the message `/jobs stop <id>`). */
  stop(id: number): void;
  /** End every running job (`/jobs stop all`). */
  stopAll(): void;
}

const MARKS: Record<Job["state"], string> = { running: "◐", exited: "✓", stopped: "■", failed: "✗" };

function node<K extends keyof HTMLElementTagNameMap>(tag: K, cls = "", text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function duration(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}

/** How a job stands, in words: "running · 1m05s", "exit 3 · 2s", "stopped · 40s". */
function detail(job: Job, ms: number): string {
  const how =
    job.state === "running" ? "running"
    : job.state === "exited" ? (job.exit_code === 0 ? "done" : `exit ${job.exit_code}`)
    : job.state === "stopped" ? "stopped"
    : "failed";
  const parts = [how, duration(ms)];
  if (job.note) parts.push(job.note);
  if (job.outside_sandbox) parts.push("outside the sandbox");
  return parts.join(" · ");
}

interface Row {
  row: HTMLElement;
  mark: HTMLElement;
  command: HTMLElement;
  detail: HTMLElement;
  stop: HTMLButtonElement;
  tail: HTMLElement;
}

export class JobsPanel {
  private readonly jobs = new Map<number, { job: Job; seen: number }>();
  private readonly rows = new Map<number, Row>();
  private readonly count = node("span", "dock-count");
  private readonly now = node("span", "dock-now");
  private readonly stopAll = node("button", "secondary dock-action stop-all", "Stop all");
  private timer: number | undefined;

  constructor(
    private readonly panel: DockPanel,
    private readonly host: JobsHost,
  ) {
    panel.summary.append(this.count, this.now);
    this.stopAll.type = "button";
    this.stopAll.title = "End every running background job";
    this.stopAll.onclick = () => host.stopAll();
    panel.actions.append(this.stopAll);
  }

  update(ev: JobsChanged): void {
    const seen = Date.now();
    const known = new Set(this.jobs.keys());
    const fresh = ev.jobs.some((j) => j.state === "running" && !known.has(j.id));
    this.jobs.clear();
    for (const job of ev.jobs) this.jobs.set(job.id, { job, seen });
    if (fresh) this.panel.autoOpen(); // a job just started
    this.render();
    if (this.timer === undefined && this.running().length) this.timer = window.setInterval(() => this.render(), 1000);
  }

  private running(): Job[] {
    return Array.from(this.jobs.values()).map((e) => e.job).filter((j) => j.state === "running");
  }

  private row(id: number): Row {
    let r = this.rows.get(id);
    if (r) return r;
    const row = node("div", "job");
    const line = node("div", "job-line");
    const mark = node("span", "job-mark");
    mark.setAttribute("aria-hidden", "true");
    const command = node("code", "job-cmd");
    const info = node("span", "job-detail");
    const stop = node("button", "secondary stop-job", "Stop");
    stop.type = "button";
    stop.onclick = () => {
      stop.disabled = true;
      this.host.stop(id);
    };
    line.append(mark, command, info, stop);
    const tail = node("pre", "job-tail");
    row.append(line, tail);
    r = { row, mark, command, detail: info, stop, tail };
    this.rows.set(id, r);
    return r;
  }

  private render(): void {
    const entries = Array.from(this.jobs.values());
    this.panel.show(entries.length > 0);
    for (const [id, r] of this.rows) {
      if (!this.jobs.has(id)) {
        r.row.remove();
        this.rows.delete(id);
      }
    }
    const running = this.running();
    if (!running.length && this.timer !== undefined) {
      window.clearInterval(this.timer);
      this.timer = undefined;
    }
    if (!entries.length) return;

    const finished = entries.length - running.length;
    this.count.textContent = `${running.length} running${finished ? ` · ${finished} finished` : ""}`;
    const latest = running[running.length - 1];
    this.now.textContent = latest ? `${MARKS.running} ${latest.command}` : "";
    this.stopAll.hidden = running.length < 2;

    for (const { job, seen } of entries) {
      const r = this.row(job.id);
      const ms = job.state === "running" ? job.elapsed_ms + (Date.now() - seen) : job.elapsed_ms;
      // A job that ended with a failing exit code is not a success, whatever "exited" says.
      const bad = job.state === "exited" && job.exit_code !== 0;
      r.row.className = `job ${job.state}${bad ? " bad" : ""}`;
      r.row.dataset.job = String(job.id);
      r.mark.textContent = bad ? "✗" : MARKS[job.state];
      r.command.textContent = job.command;
      r.command.title = job.command;
      r.detail.textContent = detail(job, ms);
      r.stop.hidden = job.state !== "running";
      if (job.state === "running") {
        r.stop.setAttribute("aria-label", `Stop job ${job.id}: ${job.command}`);
        r.stop.title = "End this job and everything it started";
      }
      r.tail.textContent = job.tail;
      r.tail.hidden = !job.tail;
      if (r.row.parentElement !== this.panel.body) this.panel.body.append(r.row);
    }
  }
}
