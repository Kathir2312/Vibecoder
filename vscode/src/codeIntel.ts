// Code intelligence for cmcoder: what VS Code's language servers know about the code, asked by
// name for the model (`goToDefinition`, `findReferences`, `workspaceSymbols`). cmcoder sends the
// question as an `ide_tool_request`; this runs the editor's own providers and answers with JSON
// (`{"results": [...]}`) that the engine turns into the text the model reads (src/cmcoder/core/ide.py:
// it leaves out protected files and shows files outside the project as a path only).
//
// The editor is reached through `CodeApi`, a small structural type that `vscode` satisfies
// (editorContext.ts builds it), so that what is decided here (which column a name is at, how the
// answers are read) is tested without VS Code (test/codeIntel.test.ts).

export interface UriLike {
  fsPath: string;
}
export interface PosLike {
  line: number;
  character: number;
}
export interface RangeLike {
  start: PosLike;
}
export interface LocationLike {
  uri: UriLike;
  range: RangeLike;
}
export interface LocationLinkLike {
  targetUri: UriLike;
  targetRange: RangeLike;
  targetSelectionRange?: RangeLike;
}
export interface SymbolLike {
  name: string;
  kind: number;
  containerName?: string;
  location: LocationLike;
}
export interface DocumentLike {
  lineCount: number;
  lineAt(line: number): { text: string };
}

export interface CodeApi {
  /** vscode.commands.executeCommand: the language servers' providers are commands. */
  command(name: string, ...args: unknown[]): Promise<unknown>;
  file(path: string): UriLike;
  position(line: number, character: number): PosLike;
  /** The file as a document, or undefined when it cannot be opened (binary, gone). */
  document(uri: UriLike): Promise<DocumentLike | undefined>;
  /** The name of a symbol kind ("Class", "Method"). */
  kindName(kind: number): string;
}

export const CODE_TOOLS = ["goToDefinition", "findReferences", "workspaceSymbols"] as const;
export type CodeTool = (typeof CODE_TOOLS)[number];

export interface ToolAnswer {
  content: string;
  is_error: boolean;
}

/** Most places sent back: the engine shows fewer, but drops some (protected files) after this. */
const MAX_PLACES = 80;
const MAX_SYMBOLS = 100;

export function isCodeTool(name: string): name is CodeTool {
  return (CODE_TOOLS as readonly string[]).includes(name);
}

/** Where `symbol` starts in `text` as a whole word (0-based), or -1. `total` is not found in `subtotal`. */
export function columnOf(text: string, symbol: string): number {
  const escaped = symbol.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const found = new RegExp(`(?<![\\w$])${escaped}(?![\\w$])`).exec(text);
  return found ? found.index : -1;
}

const fail = (content: string): ToolAnswer => ({ content, is_error: true });
const ok = (results: unknown[]): ToolAnswer => ({ content: JSON.stringify({ results }), is_error: false });

function isLink(x: LocationLike | LocationLinkLike): x is LocationLinkLike {
  return "targetUri" in x;
}

/** Locations and links, both of which a provider may answer with, as one kind of place. */
function placeOf(x: LocationLike | LocationLinkLike): { uri: UriLike; at: PosLike } {
  if (isLink(x)) return { uri: x.targetUri, at: (x.targetSelectionRange ?? x.targetRange).start };
  return { uri: x.uri, at: x.range.start };
}

async function ask(api: CodeApi, command: string, input: Record<string, unknown>): Promise<ToolAnswer> {
  const file = typeof input.file_path === "string" ? input.file_path : "";
  const line = typeof input.line === "number" ? Math.floor(input.line) : 0;
  const symbol = typeof input.symbol === "string" ? input.symbol : "";
  if (!file || line < 1 || !symbol) return fail("file_path, line and symbol are required.");
  const uri = api.file(file);
  const doc = await api.document(uri);
  if (!doc) return fail(`Could not open ${file}.`);
  if (line > doc.lineCount) return fail(`${file} has only ${doc.lineCount} lines.`);
  const text = doc.lineAt(line - 1).text;
  const column = columnOf(text, symbol);
  if (column < 0) return fail(`Line ${line} of ${file} does not contain '${symbol}': ${text.trim().slice(0, 200)}`);
  const found = ((await api.command(command, uri, api.position(line - 1, column))) ?? []) as Array<LocationLike | LocationLinkLike>;
  const results: unknown[] = [];
  const opened = new Map<string, DocumentLike | undefined>();
  for (const item of found.slice(0, MAX_PLACES)) {
    const { uri: where, at } = placeOf(item);
    if (!opened.has(where.fsPath)) opened.set(where.fsPath, await api.document(where));
    const target = opened.get(where.fsPath);
    results.push({
      path: where.fsPath,
      line: at.line + 1,
      column: at.character + 1,
      text: target && at.line < target.lineCount ? target.lineAt(at.line).text : "",
    });
  }
  return ok(results);
}

async function symbols(api: CodeApi, input: Record<string, unknown>): Promise<ToolAnswer> {
  const query = typeof input.query === "string" ? input.query : "";
  if (!query) return fail("query is required.");
  const found = ((await api.command("vscode.executeWorkspaceSymbolProvider", query)) ?? []) as SymbolLike[];
  return ok(
    found.slice(0, MAX_SYMBOLS).map((s) => ({
      name: s.name,
      kind: api.kindName(s.kind),
      container: s.containerName ?? "",
      path: s.location.uri.fsPath,
      line: s.location.range.start.line + 1,
    })),
  );
}

/** Run one of the code tools; a language server that fails is an error answer, never a crash. */
export async function runCodeTool(api: CodeApi, name: CodeTool, input: Record<string, unknown>): Promise<ToolAnswer> {
  try {
    switch (name) {
      case "goToDefinition":
        return await ask(api, "vscode.executeDefinitionProvider", input);
      case "findReferences":
        return await ask(api, "vscode.executeReferenceProvider", input);
      case "workspaceSymbols":
        return await symbols(api, input);
    }
  } catch (e) {
    return fail(`The language server failed: ${(e as Error).message}`);
  }
}
