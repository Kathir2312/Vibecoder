// Copying to the clipboard, from a button in the panel.

/** Copy to the clipboard: the browser's API where the page may use it, else the old way. */
export function copyText(text: string, button: HTMLElement): void {
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
