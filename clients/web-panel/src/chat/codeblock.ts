// Code in a reply: a header with the language and what can be done with the code (Copy, and where the
// IDE can, Insert at cursor), and the code coloured by language. The Markdown has already been
// rendered and cleaned (main.ts `sanitize`) when this runs; the colours are added by building elements
// with textContent from highlight.ts spans, so nothing in the code is ever parsed as markup.

import { copyText } from "./clipboard";
import { labelOf, languageOf, tokenize } from "./highlight";
import { updateThemeMode } from "./theme";

export interface CodeHost {
  /** Put the code into the file being edited, at the cursor (data-features "insertCode"); none: no button. */
  insert?: (text: string) => void;
  /** Open the code against the file being edited, in the IDE's diff editor (data-features "compareCode"). */
  compare?: (text: string) => void;
}

function button(label: string, title: string): HTMLButtonElement {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "secondary codebtn";
  b.textContent = label;
  b.title = title;
  return b;
}

/** Wrap every code block under `root` (once) and colour it. */
export function enhanceCodeBlocks(root: ParentNode, host: CodeHost = {}): void {
  const blocks = Array.from(root.querySelectorAll("pre")).filter((pre) => pre.querySelector(":scope > code") && !pre.closest(".codeblock"));
  if (!blocks.length) return;
  updateThemeMode(); // the colours below are picked by the page's theme
  for (const pre of blocks) {
    const code = pre.querySelector(":scope > code") as HTMLElement;
    const text = (code.textContent ?? "").replace(/\n$/, "");
    const word = /language-(\S+)/.exec(code.className)?.[1] ?? "";
    const language = languageOf(word);
    const spans = tokenize(text, language);
    if (spans) {
      code.replaceChildren(
        ...spans.map((s) => {
          if (!s.kind) return document.createTextNode(s.text);
          const span = document.createElement("span");
          span.className = `tk-${s.kind}`;
          span.textContent = s.text;
          return span;
        }),
      );
    }

    const block = document.createElement("div");
    block.className = "codeblock";
    const bar = document.createElement("div");
    bar.className = "codebar";
    const name = document.createElement("span");
    name.className = "codelang";
    name.textContent = language ? labelOf(language) : word || "code";
    const actions = document.createElement("span");
    actions.className = "codeactions";
    const copy = button("Copy", "Copy this code");
    copy.setAttribute("aria-label", `Copy the ${name.textContent} code`);
    copy.onclick = () => copyText(text, copy);
    actions.append(copy);
    if (host.insert) {
      const insert = button("Insert", "Put this code in the open file, at the cursor");
      insert.setAttribute("aria-label", `Insert the ${name.textContent} code at the cursor`);
      insert.onclick = () => host.insert?.(text);
      actions.append(insert);
    }
    if (host.compare) {
      const compare = button("Compare", "See this code against the open file: nothing is written until you take a change");
      compare.setAttribute("aria-label", `Compare the ${name.textContent} code with the open file`);
      compare.onclick = () => host.compare?.(text);
      actions.append(compare);
    }
    bar.append(name, actions);
    pre.replaceWith(block);
    block.append(bar, pre);
  }
}
