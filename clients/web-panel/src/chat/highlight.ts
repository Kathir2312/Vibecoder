// Syntax highlighting for the code in replies. Small, regular-expression based and dependency free:
// a few dozen kilobytes of a general-purpose highlighter would double the panel, and a reply's code
// block only needs the usual colours for the languages this panel's users write (C#, TypeScript,
// Python, SQL, markup, JSON, shell, diffs) -- nothing is parsed, so a snippet that is not valid still
// reads well. What this returns is a list of spans; the caller builds DOM from them with textContent
// (never innerHTML), so code in a reply can't become markup.

export type Kind =
  | "keyword"
  | "type"
  | "function"
  | "literal"
  | "string"
  | "number"
  | "comment"
  | "attr"
  | "tag"
  | "meta"
  | "add"
  | "del";

export interface Span {
  text: string;
  kind?: Kind; // none: plain text
}

/** Longer than this is shown plain: a pasted file, not a snippet. */
export const MAX_HIGHLIGHT = 20_000;

const words = (s: string): Set<string> => new Set(s.split(/\s+/).filter(Boolean));

interface Grammar {
  line?: string[]; // line comment starters
  block?: boolean; // /* ... */
  strings: string[]; // regular-expression sources, tried in order
  keywords: Set<string>;
  literals: Set<string>;
  types?: Set<string>; // built-in type names
  pascal?: boolean; // a PascalCase name that is not a call is a type
  caseless?: boolean; // keywords in any case (SQL)
  keyColon?: boolean; // a word followed by ":" is a key (YAML, CSS)
  flags?: boolean; // -x and --long are options (shell)
  vars?: boolean; // $name is a variable (shell)
  units?: boolean; // 12px, #fff (CSS)
}

const DQ = String.raw`"(?:[^"\\\n]|\\.)*"`;
const SQ = String.raw`'(?:[^'\\\n]|\\.)*'`;
const TEMPLATE = "`(?:[^`\\\\]|\\\\[\\s\\S])*`"; // a JS template string, which may span lines

const GRAMMARS: Record<string, Grammar> = {
  csharp: {
    line: ["//"],
    block: true,
    strings: [String.raw`@"(?:[^"]|"")*"`, String.raw`\$@?"(?:[^"\\\n]|\\.)*"`, DQ, SQ],
    keywords: words(`abstract as async await base break case catch checked class const continue default delegate do else enum event explicit extern finally fixed for foreach goto if implicit in interface internal is lock namespace new operator out override params private protected public readonly ref return sealed sizeof stackalloc static struct switch this throw try typeof unchecked unsafe using virtual void volatile while var record init get set yield when where select from orderby let join into group by on equals with required file scoped`),
    literals: words("true false null"),
    types: words("int string bool long short byte char decimal double float object uint ulong ushort sbyte dynamic nint nuint"),
    pascal: true,
  },
  java: {
    line: ["//"],
    block: true,
    strings: [DQ, SQ],
    keywords: words(`abstract assert break case catch class const continue default do else enum extends final finally for goto if implements import instanceof interface native new package private protected public return static strictfp super switch synchronized this throw throws transient try void volatile while var record sealed permits yield`),
    literals: words("true false null"),
    types: words("int long short byte char boolean double float"),
    pascal: true,
  },
  javascript: {
    line: ["//"],
    block: true,
    strings: [DQ, SQ, TEMPLATE],
    keywords: words(`async await break case catch class const continue debugger default delete do else enum export extends finally for from function if implements import in instanceof interface let new of package private protected public return static super switch this throw try typeof var void while with yield as type namespace declare readonly abstract keyof infer is satisfies`),
    literals: words("true false null undefined NaN Infinity"),
    types: words("string number boolean any unknown never object symbol bigint"),
    pascal: true,
  },
  c: {
    line: ["//"],
    block: true,
    strings: [DQ, SQ],
    keywords: words(`auto break case catch class const constexpr continue default delete do else enum explicit extern for friend goto if inline namespace new noexcept operator private protected public register return signed sizeof static struct switch template this throw try typedef typename union unsigned using virtual void volatile while`),
    literals: words("true false nullptr NULL"),
    types: words("int long short char float double bool size_t"),
  },
  go: {
    line: ["//"],
    block: true,
    strings: [DQ, "`[^`]*`", SQ],
    keywords: words("break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var"),
    literals: words("true false nil iota"),
    types: words("int int8 int16 int32 int64 uint uint8 uint16 uint32 uint64 float32 float64 string bool error byte rune any"),
  },
  rust: {
    line: ["//"],
    block: true,
    strings: [DQ, String.raw`'(?:[^'\\\n]|\\.)'`],
    keywords: words("as async await break const continue crate dyn else enum extern fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait type unsafe use where while"),
    literals: words("true false"),
    types: words("i8 i16 i32 i64 u8 u16 u32 u64 usize isize f32 f64 bool str char String Vec Option Result"),
  },
  python: {
    line: ["#"],
    strings: [String.raw`[rRbBfFuU]{0,2}"""[\s\S]*?(?:"""|$)`, String.raw`[rRbBfFuU]{0,2}'''[\s\S]*?(?:'''|$)`, `[rRbBfFuU]{0,2}${DQ}`, `[rRbBfFuU]{0,2}${SQ}`],
    keywords: words("and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case"),
    literals: words("True False None self cls"),
    types: words("int str bool float list dict set tuple bytes object"),
  },
  sql: {
    line: ["--"],
    block: true,
    strings: [String.raw`'(?:[^']|'')*'`],
    caseless: true,
    keywords: words("select from where join left right inner outer full cross on group by order having limit offset top insert into values update set delete create table alter drop add column index view primary key foreign references constraint default unique check union all distinct as and or not in is exists between like case when then else end with begin commit rollback transaction procedure function returns declare exec execute asc desc over partition"),
    literals: words("null true false"),
    types: words("int bigint smallint tinyint bit decimal numeric float real money varchar nvarchar char nchar text datetime datetime2 date time uniqueidentifier"),
  },
  shell: {
    line: ["#"],
    strings: [DQ, SQ],
    keywords: words("if then else elif fi for do done while until case esac in function return exit break continue foreach param begin process end try catch finally switch"),
    literals: words("true false null"),
    flags: true,
    vars: true,
  },
  yaml: {
    line: ["#"],
    strings: [DQ, SQ],
    keywords: new Set(),
    literals: words("true false null yes no on off"),
    keyColon: true,
  },
  css: {
    block: true,
    strings: [DQ, SQ],
    keywords: new Set(),
    literals: new Set(),
    keyColon: true,
    units: true,
  },
};

const ALIASES: Record<string, string> = {
  cs: "csharp", csharp: "csharp", "c#": "csharp", cake: "csharp",
  java: "java", kotlin: "java", kt: "java", scala: "java",
  js: "javascript", javascript: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  ts: "javascript", typescript: "javascript", tsx: "javascript",
  c: "c", h: "c", cpp: "c", "c++": "c", cc: "c", hpp: "c",
  go: "go", golang: "go",
  rust: "rust", rs: "rust",
  py: "python", python: "python", python3: "python",
  sql: "sql", tsql: "sql", psql: "sql", mysql: "sql",
  sh: "shell", bash: "shell", shell: "shell", zsh: "shell", console: "shell", shellsession: "shell",
  powershell: "shell", ps1: "shell", pwsh: "shell",
  yaml: "yaml", yml: "yaml", toml: "yaml", ini: "yaml",
  css: "css", scss: "css", less: "css",
  json: "json", jsonc: "json", json5: "json",
  html: "markup", htm: "markup", xml: "markup", xaml: "markup", svg: "markup", csproj: "markup",
  vbproj: "markup", props: "markup", config: "markup", cshtml: "markup", razor: "markup", vue: "markup",
  diff: "diff", patch: "diff",
};

/** The grammar a fence's language word names ("C#", "language-csharp", "ts"), or null. */
export function languageOf(word: string): string | null {
  const w = word.toLowerCase().replace(/^language-/, "").trim();
  return ALIASES[w] ?? null;
}

/** A language's name as the block's header shows it. */
const LABELS: Record<string, string> = {
  csharp: "C#", java: "Java", javascript: "JS/TS", c: "C/C++", go: "Go", rust: "Rust", python: "Python",
  sql: "SQL", shell: "Shell", yaml: "YAML", css: "CSS", json: "JSON", markup: "HTML/XML", diff: "Diff",
};
export const labelOf = (language: string): string => LABELS[language] ?? language;

const cache = new Map<string, RegExp>();

function master(language: string, g: Grammar): RegExp {
  let re = cache.get(language);
  if (re) return re;
  const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&");
  const parts: string[] = [];
  const comment: string[] = [];
  for (const l of g.line ?? []) comment.push(`${esc(l)}[^\\n]*`);
  if (g.block) comment.push("/\\*[\\s\\S]*?(?:\\*/|$)");
  if (comment.length) parts.push(`(?<c>${comment.join("|")})`);
  parts.push(`(?<s>${g.strings.join("|")})`);
  if (g.vars) parts.push("(?<v>\\$(?:\\{[^}\\n]*\\}|\\([^)\\n]*\\)|[A-Za-z_]\\w*))");
  if (g.flags) parts.push("(?<f>(?<![\\w-])--?[A-Za-z][\\w-]*)");
  if (g.units) parts.push("(?<n>#[0-9a-fA-F]{3,8}\\b|\\d+(?:\\.\\d+)?(?:px|em|rem|%|vh|vw|ms|s|deg|fr)?)");
  else parts.push("(?<n>(?:0[xX][\\da-fA-F_]+|\\d[\\d_]*(?:\\.\\d[\\d_]*)?(?:[eE][+-]?\\d+)?)[uUlLfFdDmM]*)");
  parts.push(g.keyColon ? "(?<w>[A-Za-z_$][\\w$-]*)" : "(?<w>[A-Za-z_$][\\w$]*)"); // YAML and CSS names have hyphens
  re = new RegExp(parts.join("|"), "gy");
  cache.set(language, re);
  return re;
}

function push(out: Span[], text: string, kind?: Kind): void {
  if (!text) return;
  const last = out[out.length - 1];
  if (last && last.kind === kind) last.text += text;
  else out.push({ text, kind });
}

function generic(code: string, language: string, g: Grammar): Span[] {
  const re = master(language, g);
  const out: Span[] = [];
  let pos = 0;
  let plain = "";
  const flush = () => {
    push(out, plain);
    plain = "";
  };
  while (pos < code.length) {
    re.lastIndex = pos;
    const m = re.exec(code);
    if (!m) {
      plain += code.slice(pos, pos + 1);
      pos += 1;
      continue;
    }
    const text = m[0];
    pos += text.length || 1;
    const named = m.groups ?? {};
    let kind: Kind | undefined;
    if (named.c !== undefined) kind = "comment";
    else if (named.s !== undefined) kind = "string";
    else if (named.v !== undefined) kind = "literal";
    else if (named.f !== undefined) kind = "attr";
    else if (named.n !== undefined) kind = "number";
    else if (named.w !== undefined) {
      const key = g.caseless ? text.toLowerCase() : text;
      const after = code.slice(pos).match(/^\s*(.)/)?.[1] ?? "";
      if (g.keyColon && after === ":" ) kind = "attr";
      else if (g.keywords.has(key)) kind = "keyword";
      else if (g.literals.has(key)) kind = "literal";
      else if (g.types?.has(key)) kind = "type";
      else if (after === "(") kind = "function";
      else if (g.pascal && /^[A-Z][A-Za-z0-9]*$/.test(text) && text.length > 1) kind = "type";
    }
    if (kind === undefined) plain += text;
    else {
      flush();
      push(out, text, kind);
    }
  }
  flush();
  return out;
}

const JSON_TOKENS = /(?<s>"(?:[^"\\\n]|\\.)*")|(?<n>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|(?<l>\b(?:true|false|null)\b)|(?<c>\/\/[^\n]*|\/\*[\s\S]*?(?:\*\/|$))/gy;

function json(code: string): Span[] {
  const out: Span[] = [];
  let pos = 0;
  let plain = "";
  while (pos < code.length) {
    JSON_TOKENS.lastIndex = pos;
    const m = JSON_TOKENS.exec(code);
    if (!m) {
      plain += code[pos++];
      continue;
    }
    pos += m[0].length;
    const g = m.groups ?? {};
    let kind: Kind;
    if (g.s !== undefined) kind = /^\s*:/.test(code.slice(pos)) ? "attr" : "string";
    else if (g.n !== undefined) kind = "number";
    else if (g.l !== undefined) kind = "literal";
    else kind = "comment";
    push(out, plain);
    plain = "";
    push(out, m[0], kind);
  }
  push(out, plain);
  return out;
}

const MARKUP_TOKENS = /(?<c><!--[\s\S]*?(?:-->|$))|(?<t><\/?[A-Za-z][\w:.-]*|\/?>|<\?[\w-]+|\?>)|(?<s>"[^"]*"|'[^']*')|(?<a>[A-Za-z_:@][\w:.@-]*(?=\s*=))|(?<m>@[A-Za-z_][\w.]*|@\{|@\(|&[#\w]+;)/gy;

function markup(code: string): Span[] {
  const out: Span[] = [];
  let pos = 0;
  let plain = "";
  while (pos < code.length) {
    MARKUP_TOKENS.lastIndex = pos;
    const m = MARKUP_TOKENS.exec(code);
    if (!m) {
      plain += code[pos++];
      continue;
    }
    pos += m[0].length;
    const g = m.groups ?? {};
    const kind: Kind = g.c !== undefined ? "comment" : g.t !== undefined ? "tag" : g.s !== undefined ? "string" : g.a !== undefined ? "attr" : "meta";
    push(out, plain);
    plain = "";
    push(out, m[0], kind);
  }
  push(out, plain);
  return out;
}

function diff(code: string): Span[] {
  const out: Span[] = [];
  const lines = code.split("\n");
  lines.forEach((line, i) => {
    const text = i < lines.length - 1 ? `${line}\n` : line;
    let kind: Kind | undefined;
    if (/^(\+\+\+|---|diff |index )/.test(line)) kind = "meta";
    else if (line.startsWith("@@")) kind = "meta";
    else if (line.startsWith("+")) kind = "add";
    else if (line.startsWith("-")) kind = "del";
    push(out, text, kind);
  });
  return out;
}

/** The code as spans of one kind each, or null when this language is not one this knows (or it is too long). */
export function tokenize(code: string, language: string | null): Span[] | null {
  if (!language || code.length > MAX_HIGHLIGHT) return null;
  if (language === "json") return json(code);
  if (language === "markup") return markup(code);
  if (language === "diff") return diff(code);
  const g = GRAMMARS[language];
  return g ? generic(code, language, g) : null;
}
