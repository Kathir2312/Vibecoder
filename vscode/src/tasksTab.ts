// The Tasks tab: an editor tab with the agent's whole todo list, so the chat window stays free.
// The chat panel shows one compact bar (done/total, the task in progress); clicking it opens this.
// It keeps the latest list, so opening it mid-job shows everything so far, and follows the agent live.

import { randomBytes } from "node:crypto";
import * as vscode from "vscode";
import { productName } from "./brand";
import type { FromTasks, ToTasks } from "./webviewMessages";

export class TasksTab {
  static readonly viewType = "cmcoder.tasks";

  private panel?: vscode.WebviewPanel;
  private todos: Record<string, unknown>[] = [];
  private readonly readyEvents = new vscode.EventEmitter<void>();
  /** The tab's page loaded and its script runs (used by the integration tests). */
  readonly onReady = this.readyEvents.event;

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly openChat: () => void,
  ) {}

  /** The todo list changed (the `todo_update` event). */
  update(todos: Record<string, unknown>[]): void {
    this.todos = todos;
    this.post({ kind: "todos", todos });
  }

  /** A new conversation: no tasks. */
  reset(): void {
    this.todos = [];
    this.post({ kind: "reset" });
  }

  open(): void {
    if (this.panel) {
      this.panel.reveal(undefined, true);
      return;
    }
    const panel = vscode.window.createWebviewPanel(
      TasksTab.viewType,
      `${productName()}: Tasks`,
      { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, "dist")],
      },
    );
    panel.webview.html = this.html(panel.webview);
    panel.webview.onDidReceiveMessage((m: FromTasks) => {
      if (m.kind === "ready") {
        this.post({ kind: "todos", todos: this.todos });
        this.readyEvents.fire();
      } else if (m.kind === "openChat") this.openChat();
    });
    panel.onDidDispose(() => (this.panel = undefined));
    this.panel = panel;
  }

  dispose(): void {
    this.panel?.dispose();
  }

  private post(message: ToTasks): void {
    void this.panel?.webview.postMessage(message);
  }

  private html(webview: vscode.Webview): string {
    const nonce = randomBytes(16).toString("hex");
    const script = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "dist", "tasks.js"));
    const style = webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, "dist", "tasks.css"));
    const csp = ["default-src 'none'", `style-src ${webview.cspSource}`, `script-src 'nonce-${nonce}'`].join("; ");
    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="${csp}">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link href="${style}" rel="stylesheet">
  <title>Tasks</title>
</head>
<body>
  <div id="app"></div>
  <script nonce="${nonce}" src="${script}"></script>
</body>
</html>`;
  }
}
