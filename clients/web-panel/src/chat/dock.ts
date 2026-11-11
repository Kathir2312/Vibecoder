// The info dock: what the agent keeps track of beside the conversation (its tasks, its
// subagents, and whatever comes later), pinned above the conversation.
//
// Each panel is one summary line that opens to a list. Open lists share a limited part of the
// window (chat.css: each takes what its content needs, and they shrink together when that is too
// much), so the conversation always keeps its room. Reading on in the conversation folds every
// panel back to its line (main.ts calls minimize()); a click opens it again. A panel the user
// folded doesn't open by itself again in that conversation.
//
// Plain DOM code: the line's elements are made once and only their text changes, so a click is
// never lost to a redraw.

export interface DockPanel {
  /** The whole panel; hidden until show(true). */
  readonly el: HTMLElement;
  /** The line's own part (counts, the item in progress): the owner fills it. */
  readonly summary: HTMLElement;
  /** Buttons at the right of the line (open a tab, ...): the owner fills it. */
  readonly actions: HTMLElement;
  /** The list under the line, shown while open: the owner fills it. */
  readonly body: HTMLElement;
  readonly open: boolean;
  show(visible: boolean): void;
  /** New content: opens by itself, unless the user folded this panel. */
  autoOpen(): void;
  setOpen(open: boolean): void;
  /** A new conversation: what the user chose is forgotten. */
  reset(): void;
}

export interface DockPanelOptions {
  /** Called after the panel opened or closed (draw the list, scroll to the current row). */
  onChange?: (open: boolean) => void;
  /** What the line's button says to a screen reader and as a tooltip, by state. */
  hint?: (open: boolean) => string;
}

class Panel implements DockPanel {
  readonly el = document.createElement("div");
  readonly summary = document.createElement("span");
  readonly actions = document.createElement("span");
  readonly body = document.createElement("div");
  private readonly toggle = document.createElement("button");
  private isOpen = false;
  private folded = false; // the user closed it: it stays closed until they open it

  constructor(
    id: string,
    title: string,
    private readonly options: DockPanelOptions,
    private readonly changed: () => void,
  ) {
    this.el.className = "dock-panel";
    this.el.dataset.panel = id;
    this.el.hidden = true;
    const head = document.createElement("div");
    head.className = "dock-head";
    this.toggle.className = "dock-toggle";
    this.toggle.type = "button";
    this.toggle.setAttribute("aria-controls", `dock-${id}`);
    const chevron = document.createElement("span");
    chevron.className = "dock-chevron";
    const name = document.createElement("span");
    name.className = "dock-title";
    name.textContent = title;
    this.summary.className = "dock-summary";
    this.toggle.append(chevron, name, this.summary);
    this.toggle.onclick = () => {
      this.folded = this.isOpen;
      this.setOpen(!this.isOpen);
    };
    this.actions.className = "dock-actions";
    head.append(this.toggle, this.actions);
    this.body.className = "dock-body";
    this.body.id = `dock-${id}`;
    this.el.append(head, this.body);
    this.apply();
  }

  get open(): boolean {
    return this.isOpen;
  }

  show(visible: boolean): void {
    if (this.el.hidden === !visible) return;
    this.el.hidden = !visible;
    this.changed();
  }

  autoOpen(): void {
    if (!this.folded) this.setOpen(true);
  }

  setOpen(open: boolean): void {
    if (open === this.isOpen) return;
    this.isOpen = open;
    this.apply();
    this.changed();
    this.options.onChange?.(open);
  }

  reset(): void {
    this.folded = false;
    this.setOpen(false);
  }

  private apply(): void {
    this.el.classList.toggle("open", this.isOpen);
    this.body.hidden = !this.isOpen;
    this.toggle.setAttribute("aria-expanded", String(this.isOpen));
    const hint = this.options.hint?.(this.isOpen);
    if (hint) this.toggle.title = hint;
  }

  /** The line's tooltip depends on what the owner knows (counts): it asks for a refresh. */
  refreshHint(): void {
    const hint = this.options.hint?.(this.isOpen);
    if (hint) this.toggle.title = hint;
  }
}

export class Dock {
  private readonly panels: Panel[] = [];

  constructor(private readonly root: HTMLElement) {
    root.hidden = true;
  }

  /** A new panel, under the ones added before. */
  add(id: string, title: string, options: DockPanelOptions = {}): DockPanel & { refreshHint(): void } {
    const panel = new Panel(id, title, options, () => this.changed());
    this.panels.push(panel);
    this.root.append(panel.el);
    return panel;
  }

  /** Every open panel back to its line (the user reads on in the conversation). True if one closed. */
  minimize(): boolean {
    let any = false;
    for (const p of this.panels) {
      if (p.open && !p.el.hidden) any = true;
      p.setOpen(false);
    }
    return any;
  }

  /** A new conversation. */
  reset(): void {
    for (const p of this.panels) p.reset();
  }

  private changed(): void {
    const shown = this.panels.filter((p) => !p.el.hidden);
    this.root.hidden = shown.length === 0;
    // For chat.css: the dock is never lower than its lines, and open lists share its height.
    this.root.style.setProperty("--dock-lines", String(shown.length));
    this.root.style.setProperty("--dock-open", String(Math.max(1, shown.filter((p) => p.open).length)));
  }
}
