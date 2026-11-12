// Is the panel light or dark right now? The colours of code in replies differ for each, and the panel
// is shown by IDEs that say so in different ways: VS Code puts a class on <body>, the others only set
// the colour variables (theme.json). So this looks at the colour the page really has, and marks the
// page (`<html data-theme="light|dark">`) for the CSS to use.

/** Relative luminance (0 black .. 1 white) of a computed CSS colour, or undefined when it can't be read. */
export function luminanceOf(color: string): number | undefined {
  let r: number, g: number, b: number, a = 1;
  const rgb = /^rgba?\(\s*([\d.]+)[ ,]+([\d.]+)[ ,]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/.exec(color.trim());
  const srgb = /^color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+%?))?\s*\)$/.exec(color.trim());
  if (rgb) {
    [r, g, b] = [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])];
    if (rgb[4] !== undefined) a = rgb[4].endsWith("%") ? parseFloat(rgb[4]) / 100 : Number(rgb[4]);
  } else if (srgb) {
    [r, g, b] = [Number(srgb[1]) * 255, Number(srgb[2]) * 255, Number(srgb[3]) * 255];
    if (srgb[4] !== undefined) a = srgb[4].endsWith("%") ? parseFloat(srgb[4]) / 100 : Number(srgb[4]);
  } else return undefined;
  if (a === 0) return undefined; // transparent: the page has no colour of its own yet
  const lin = (c: number) => {
    const v = c / 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

export type ThemeMode = "dark" | "light";

/** Mark the page light or dark, from the colour it has (else from the system's setting). */
export function updateThemeMode(): ThemeMode {
  const lum = luminanceOf(getComputedStyle(document.body).backgroundColor);
  const mode: ThemeMode =
    lum !== undefined ? (lum > 0.4 ? "light" : "dark") : window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
  if (document.documentElement.dataset.theme !== mode) document.documentElement.dataset.theme = mode;
  return mode;
}

/** Follow the IDE when its theme changes: it sets the variables again (style) or changes a class. */
export function watchTheme(): void {
  const watch = new MutationObserver(() => updateThemeMode());
  watch.observe(document.documentElement, { attributes: true, attributeFilter: ["style", "class"] });
  watch.observe(document.body, { attributes: true, attributeFilter: ["style", "class"] });
  window.matchMedia?.("(prefers-color-scheme: light)").addEventListener?.("change", () => updateThemeMode());
  updateThemeMode();
}
