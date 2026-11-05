// VS Code's built-in browser tools, passed on to cmcoder.
//
// VS Code has an Integrated Browser (a browser tab inside the editor) and registers language-model
// tools to drive it (open a page, read it, click, type, screenshot, run Playwright code): what
// GitHub Copilot's agent uses. They are listed in `vscode.lm.tools` and can be run with
// `vscode.lm.invokeTool`. This file finds them at run time, hands their names and JSON schemas to
// cmcoder (`ide_capabilities.tool_defs`) and runs them when cmcoder asks (`ide_tool_request`), so
// a page the agent opens appears in VS Code, where the user can watch it, and not in a separate
// Chrome window.
//
// Nothing here imports "vscode": it takes the `lm` object as an argument, so it is testable and a
// VS Code without these APIs just finds nothing. The tools' exact names and schemas are VS Code's
// own and have changed between versions, so nothing is hard-coded about their inputs: the
// schemas travel as they are, and the command "cmcoder: Show the IDE's Browser Tools" lists
// what this VS Code offers.

export interface LmTool {
  readonly name: string;
  readonly description?: string;
  readonly inputSchema?: object;
  readonly tags?: readonly string[];
}

export interface LmLike {
  readonly tools?: readonly LmTool[];
  invokeTool(
    name: string,
    options: { input: object; toolInvocationToken: undefined },
    token?: unknown,
  ): PromiseLike<{ content: readonly unknown[] }>;
}

/** What cmcoder takes in `ide_capabilities.tool_defs`. */
export interface IdeToolDef {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
}

export interface IdeToolResult {
  content: string;
  is_error: boolean;
  images: string[];
}

// The names VS Code's documentation gives its browser tools, and the "integratedBrowser" prefix
// other versions use; compared without case or separators (open_browser_page = openBrowserPage).
const BROWSER_TOOLS = [
  "openbrowserpage",
  "navigatepage",
  "readpage",
  "screenshotpage",
  "clickelement",
  "hoverelement",
  "dragelement",
  "typeinpage",
  "handledialog",
  "runplaywrightcode",
  "integratedbrowser",
];

const flat = (name: string): string => name.toLowerCase().replace(/[^a-z0-9]/g, "");

/** Is this one of VS Code's browser tools? Never one of cmcoder's own. */
export function isBrowserTool(name: string): boolean {
  const n = flat(name);
  return !n.includes("cmcoder") && BROWSER_TOOLS.some((t) => n.includes(t));
}

/**
 * The browser tools this VS Code offers, as cmcoder wants them. `explicit`: the names the user
 * listed in the setting `cmcoder.integratedBrowserTools` (empty: every tool that looks like one).
 */
export function browserTools(lm: LmLike | undefined, explicit: readonly string[] = []): IdeToolDef[] {
  let tools: readonly LmTool[] = [];
  try {
    tools = lm?.tools ?? [];
  } catch {
    return []; // an older VS Code, or the API refused
  }
  const wanted = new Set(explicit.map((n) => n.trim()).filter(Boolean));
  return tools
    .filter((t) => (wanted.size ? wanted.has(t.name) : isBrowserTool(t.name)))
    .map((t) => ({
      name: t.name,
      description: t.description ?? "",
      input_schema: (t.inputSchema ?? {}) as Record<string, unknown>,
    }));
}

const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);

function dataUrl(part: { data?: unknown; mimeType?: unknown }): string | undefined {
  const type = typeof part.mimeType === "string" ? part.mimeType : "";
  if (!IMAGE_TYPES.has(type) || !(part.data instanceof Uint8Array) || !part.data.length) return undefined;
  return `data:${type};base64,${Buffer.from(part.data).toString("base64")}`;
}

/** The text and pictures of a tool result: LanguageModelTextPart has `value`, LanguageModelDataPart `data`. */
export function fromParts(parts: readonly unknown[]): { content: string; images: string[] } {
  const text: string[] = [];
  const images: string[] = [];
  for (const part of parts) {
    if (typeof part !== "object" || part === null) continue;
    const p = part as { value?: unknown; data?: unknown; mimeType?: unknown };
    if (typeof p.value === "string") text.push(p.value);
    else if (p.data !== undefined) {
      const url = dataUrl(p);
      if (url) images.push(url);
      else if (typeof p.mimeType === "string" && p.data instanceof Uint8Array && /^(text|application\/json)/.test(p.mimeType)) {
        text.push(Buffer.from(p.data).toString("utf8")); // a text or JSON part sent as bytes
      }
    } else if (p.value !== undefined) {
      text.push(JSON.stringify(p.value)); // a structured part: the model can read JSON
    }
  }
  return { content: text.join("\n"), images };
}

/** Runs one of VS Code's tools for cmcoder; a failure becomes an error result, never an exception. */
export async function runBrowserTool(
  lm: LmLike | undefined,
  name: string,
  input: Record<string, unknown>,
  token?: unknown,
): Promise<IdeToolResult> {
  if (!lm) return { content: "This VS Code has no language-model tools.", is_error: true, images: [] };
  try {
    const result = await lm.invokeTool(name, { input, toolInvocationToken: undefined }, token);
    const { content, images } = fromParts(result.content);
    return { content: content || (images.length ? "Done (see the picture)." : "Done."), is_error: false, images };
  } catch (e) {
    return { content: `${name} failed: ${e instanceof Error ? e.message : String(e)}`, is_error: true, images: [] };
  }
}

/** The text for the "Show the IDE's Browser Tools" command (and the log). */
export function describeTools(lm: LmLike | undefined, explicit: readonly string[] = []): string {
  let all: readonly LmTool[] = [];
  try {
    all = lm?.tools ?? [];
  } catch {
    all = [];
  }
  if (!lm || !("tools" in lm)) {
    return "This VS Code has no language-model tool API (vscode.lm.tools). Browser tools need VS Code 1.110 or newer.";
  }
  const used = new Set(browserTools(lm, explicit).map((t) => t.name));
  const lines = [`VS Code offers ${all.length} language-model tools; cmcoder passes on ${used.size} as browser tools.`];
  for (const t of all) {
    if (!used.has(t.name)) continue;
    const props = Object.keys(((t.inputSchema ?? {}) as { properties?: object }).properties ?? {});
    lines.push(`  ✓ ${t.name}(${props.join(", ")}): ${(t.description ?? "").split("\n")[0].slice(0, 100)}`);
  }
  if (!used.size) {
    lines.push(
      "None looks like a browser tool. VS Code 1.110 or newer has them (setting workbench.browser.enableChatTools).",
      "If your VS Code names them differently, list the names in the setting cmcoder.integratedBrowserTools. Every tool name:",
      ...all.map((t) => `  · ${t.name}`),
    );
  }
  return lines.join("\n");
}
