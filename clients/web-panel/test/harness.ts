// What every panel test shares: a Chromium, and a panel loaded into it the way each IDE loads it
// (src/bridge.ts), with helpers to send it events and read what it posted to the IDE.
// Needs `npm run build` first and a Chromium for Playwright
// (`npx playwright install chromium`, or PLAYWRIGHT_BROWSERS_PATH).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { chromium, type Browser, type Page } from "playwright";
import type { AgentEvent, PermissionRequest } from "../src/protocol";
import type { FromWebview, ToWebview } from "../src/messages";

export const root = join(__dirname, "..");
let browser: Browser;

export async function startBrowser(): Promise<void> {
  browser = await chromium.launch();
}

export async function stopBrowser(): Promise<void> {
  await browser?.close();
}

// How each IDE connects (src/bridge.ts). Eclipse and NetBeans add their function
// only after the page has loaded, so messages wait in the panel's queue first.
export const FLAVOURS = ["vscode", "webview2", "function"] as const;
export type Flavour = (typeof FLAVOURS)[number];
const HOSTS: Record<Flavour, string> = {
  vscode: "window.sent=[];function acquireVsCodeApi(){return{postMessage:(m)=>window.sent.push(m)}}",
  webview2: "window.sent=[];window.chrome={webview:{postMessage:(m)=>window.sent.push(m)}}",
  function: "window.sent=[];setTimeout(()=>{window.cmcoderHostPost=(j)=>window.sent.push(JSON.parse(j))},150)",
};

/** Waits until the host can receive (the late function in Eclipse/NetBeans) and the queue is sent. */
async function connected(page: Page, flavour: Flavour): Promise<void> {
  if (flavour !== "function") return;
  // Every page says "ready" first: once it arrived, the queue has been sent.
  await page.waitForFunction(() => (window as unknown as { sent: unknown[] }).sent.length > 0);
}

export async function panel(flavour: Flavour, product?: string, started = true, hostFeatures = "stopTool sessionRules"): Promise<{ page: Page; send: (m: ToWebview) => Promise<void>; ev: (e: AgentEvent) => Promise<void>; sent: () => Promise<FromWebview[]> }> {
  const page = await browser.newPage();
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.setContent(`<!DOCTYPE html><html lang="en"><head><title>cmcoder</title><style>${readFileSync(join(root, "dist", "chat.css"), "utf8")}</style>
    <script>${HOSTS[flavour]}</script>
    </head><body data-icon="https://panel.test/icon.png" data-features="${hostFeatures}"${product ? ` data-product="${product}"` : ""}><div id="app"></div></body></html>`);
  await page.addScriptTag({ content: readFileSync(join(root, "dist", "chat.js"), "utf8") });
  await connected(page, flavour);
  // postMessage is delivered later: resolve only once the page has handled it
  // (a marker posted right after it arrives after it), so assertions never
  // race the rendering.
  const send = (m: ToWebview) =>
    page.evaluate(
      (m) =>
        new Promise<void>((resolve) => {
          const done = (e: MessageEvent) => {
            if ((e.data as { testFlush?: boolean })?.testFlush) {
              window.removeEventListener("message", done);
              resolve();
            }
          };
          window.addEventListener("message", done);
          window.postMessage(m, "*");
          window.postMessage({ testFlush: true }, "*");
        }),
      m,
    );
  const ev = (event: AgentEvent) => send({ kind: "event", event });
  const sent = () => page.evaluate(() => (window as unknown as { sent: FromWebview[] }).sent);
  page.on("close", () => assert.deepEqual(errors, []));
  if (!started) return { page, send, ev, sent };
  await send({ kind: "state", state: "ready" });
  await ev({
    type: "system_init",
    protocol_version: 1,
    session_id: "s",
    cwd: "/p",
    model: "qwen3-27b",
    provider: "corp",
    tools: [],
    permission_mode: "default",
    critique: false,
    version: "0.1.0 (abc1234 2026-10-08)",
    features: ["images", "stopTool", "sessionRules", "agents"],
  });
  return { page, send, ev, sent };
}

export const result: Extract<AgentEvent, { type: "result" }> = {
  type: "result",
  subtype: "success",
  is_error: false,
  result: "",
  num_turns: 1,
  duration_ms: 1,
  usage: {},
  session_id: "s",
  review: null,
};

/** A permission request with the usual fields; each test gives what it is about. */
export function ask(over: Partial<PermissionRequest> & Pick<PermissionRequest, "request_id" | "tool_use_id" | "name" | "label" | "input" | "suggested_rule">): AgentEvent {
  return {
    type: "permission_request", reason: "", can_remember: true, change: null, cwd: null, reasons: [], high_risk: false, agent: null,
    ...over,
  };
}

const theme = JSON.parse(readFileSync(join(root, "theme.json"), "utf8")).variables as Record<string, { dark: string; light: string }>;

/** The IDE's colours: VS Code sets these itself; here they are the values theme.json lists. */
export async function themed(page: Page, mode: "dark" | "light"): Promise<void> {
  const vars = Object.entries(theme).map(([name, v]) => `${name}:${v[mode]}`).join(";");
  await page.addStyleTag({ content: `:root{${vars}}body{background:var(--vscode-sideBar-background);color:var(--vscode-foreground)}` });
}
