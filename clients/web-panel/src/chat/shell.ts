// Light highlighting of a shell command for the consent card and the Bash card: flags,
// operators, strings and variables get a colour, and the parts that can do real harm
// (`rm -rf`, `sudo`, `git push --force`, a download piped into a shell, ...) are marked.
// This is for reading, never for deciding: whether a command needs a person's approval
// is the engine's call (core/risk.py); the marks only point at what to look at.

export type TokenKind = "word" | "flag" | "op" | "string" | "var" | "comment" | "space";

export interface Span {
  text: string;
  kind: TokenKind;
  danger: boolean;
}

// Each pattern marks the part of a command that matches.
const DANGER: RegExp[] = [
  /\brm\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*(?:\s+-[a-zA-Z]+)*/g, // rm -rf, rm -fr, rm -r -f
  /\bsudo\b/g,
  /\bgit\s+push\b[^;&|\n]*?(?:--force(?:-with-lease)?|\s-f\b)/g,
  /\bgit\s+clean\s+-[a-zA-Z]*[fdx][a-zA-Z]*/g,
  /\bgit\s+reset\s+--hard\b/g,
  /\bgit\s+checkout\s+(?:--\s+)?\.\s*$/gm,
  /\bchmod\s+-R\b/g,
  /\bchown\s+-R\b/g,
  /\bmkfs(?:\.\w+)?\b/g,
  /\bdd\s+if=\S+/g,
  />\s*\/dev\/(?:sd|nvme|disk)\w*/g,
  /\b(?:curl|wget)\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b/g, // curl ... | sh
  /\b(?:DROP|TRUNCATE)\s+(?:TABLE|DATABASE)\b/gi,
  /:\(\)\s*\{[^}]*\};\s*:/g, // a fork bomb
];

/** Which characters of `command` belong to a dangerous part. */
function dangerMask(command: string): boolean[] {
  const mask = new Array<boolean>(command.length).fill(false);
  for (const pattern of DANGER) {
    pattern.lastIndex = 0;
    for (let m = pattern.exec(command); m; m = pattern.exec(command)) {
      for (let i = m.index; i < m.index + m[0].length; i++) mask[i] = true;
      if (m[0].length === 0) pattern.lastIndex++;
    }
  }
  return mask;
}

/** The kind of each character: quotes and comments first, then operators, flags, variables. */
function kinds(command: string): TokenKind[] {
  const out = new Array<TokenKind>(command.length).fill("word");
  let i = 0;
  const fill = (from: number, to: number, kind: TokenKind) => {
    for (let k = from; k < to; k++) out[k] = kind;
  };
  while (i < command.length) {
    const c = command[i];
    if (c === "'" || c === '"') {
      let j = i + 1;
      while (j < command.length && command[j] !== c) j += command[j] === "\\" && c === '"' ? 2 : 1;
      j = Math.min(command.length, j + 1);
      fill(i, j, "string");
      i = j;
    } else if (c === "#" && (i === 0 || /\s/.test(command[i - 1]))) {
      const j = command.indexOf("\n", i);
      const end = j < 0 ? command.length : j;
      fill(i, end, "comment");
      i = end;
    } else if (/\s/.test(c)) {
      fill(i, i + 1, "space");
      i++;
    } else if (/[&|;<>(){}]/.test(c)) {
      let j = i + 1;
      while (j < command.length && /[&|<>]/.test(command[j]) && /[&|<>]/.test(c)) j++;
      fill(i, j, "op");
      i = j;
    } else if (c === "$") {
      const m = /^\$(?:\{[^}]*\}|\([^)]*\)?|[A-Za-z_][A-Za-z0-9_]*|[0-9@#?$!*-])/.exec(command.slice(i));
      const len = m ? m[0].length : 1;
      fill(i, i + len, "var");
      i += len;
    } else {
      let j = i;
      while (j < command.length && !/[\s&|;<>(){}'"$#]/.test(command[j])) j++;
      if (j === i) j = i + 1;
      const word = command.slice(i, j);
      const atStart = i === 0 || /[\s;&|(]/.test(command[i - 1]);
      fill(i, j, atStart && /^--?[A-Za-z0-9]/.test(word) ? "flag" : "word");
      i = j;
    }
  }
  return out;
}

/** The command cut into spans of one kind and one danger state each (they join back to it). */
export function highlightShell(command: string): Span[] {
  if (!command) return [];
  const k = kinds(command);
  const danger = dangerMask(command);
  const spans: Span[] = [];
  for (let i = 0; i < command.length; i++) {
    const last = spans[spans.length - 1];
    if (last && last.kind === k[i] && last.danger === danger[i]) last.text += command[i];
    else spans.push({ text: command[i], kind: k[i], danger: danger[i] });
  }
  return spans;
}

/** The dangerous parts, whole, for a caption ("rm -rf", "sudo"). */
export function dangerousParts(command: string): string[] {
  const mask = dangerMask(command);
  const parts: string[] = [];
  let current = "";
  for (let i = 0; i < command.length; i++) {
    if (mask[i]) current += command[i];
    else if (current) {
      parts.push(current.trim());
      current = "";
    }
  }
  if (current) parts.push(current.trim());
  return parts.filter(Boolean);
}

/** A `<span>` tree for the spans: text only, nothing from the command is ever parsed as HTML. */
export function renderShell(command: string): DocumentFragment {
  const fragment = document.createDocumentFragment();
  for (const span of highlightShell(command)) {
    if (span.kind === "space" && !span.danger) {
      fragment.append(document.createTextNode(span.text));
      continue;
    }
    const node = document.createElement("span");
    node.className = `sh-${span.kind}${span.danger ? " sh-danger" : ""}`;
    node.textContent = span.text;
    fragment.append(node);
  }
  return fragment;
}
