"""The files `/playwright setup` has the model create, as exact text.

The model writes them with its Write tool, so the user sees each one in the usual consent and
diff card first. They are the same for every web app: the address, the browser and (only when
the user gave one) the command that starts the app are the only things that differ.
"""

from __future__ import annotations

import json

PLAYWRIGHT_VERSION = "^1.56.1"  # the first with the test agents; npm picks the newest 1.x


def package_json(directory: str) -> str:
    return (
        json.dumps(
            {
                "name": directory,
                "private": True,
                "description": "Playwright tests for the web app, made with cmcoder (/playwright).",
                "scripts": {
                    "test": "playwright test",
                    "list": "playwright test --list",
                    "report": "playwright show-report",
                },
                "devDependencies": {"@playwright/test": PLAYWRIGHT_VERSION},
            },
            indent=2,
        )
        + "\n"
    )


def config_ts(url: str, *, channel: str | None, executable: str | None, start: str | None) -> str:
    """playwright.config.ts. `start`: how to start the app, only when the user said so (without
    it the app has to be running already)."""
    if channel:
        browser = f"    channel: {json.dumps(channel)}, // the browser already on this PC: nothing to download"
    elif executable:
        browser = (
            f"    launchOptions: {{ executablePath: {json.dumps(executable)} }}, "
            "// the browser cmcoder found on this PC"
        )
    else:
        browser = "    // No Chrome or Edge was found: run `npx playwright install chromium` in this folder."
    server = (
        "  // Playwright starts the app for the tests (and uses it if it already runs).\n"
        "  webServer: {\n"
        f"    command: {json.dumps(start)},\n"
        "    cwd: '..', // the project folder: write the command as you would type it there\n"
        f"    url: {json.dumps(url)},\n"
        "    reuseExistingServer: true,\n"
        "    timeout: 120_000,\n"
        "  },\n"
        if start
        else "  // The app must already be running at the address above (add a `webServer` to start it).\n"
    )
    return (
        "import { defineConfig } from '@playwright/test';\n"
        "\n"
        "// Made by cmcoder (/playwright setup). Edit freely.\n"
        "export default defineConfig({\n"
        "  testDir: './tests',\n"
        "  fullyParallel: true,\n"
        "  retries: 0,\n"
        "  reporter: [['list'], ['html', { open: 'never' }]],\n"
        "  use: {\n"
        f"    baseURL: {json.dumps(url)},\n"
        "    ignoreHTTPSErrors: true, // development certificates\n"
        f"{browser}\n"
        "  },\n"
        f"{server}"
        "});\n"
    )


SEED_SPEC = """\
import { test, expect } from '@playwright/test';

// The starting point of every plan and test: the app's front page opens.
// If the app needs a login, do it here, and have the other tests start the same way.
test('seed', async ({ page }) => {
  await page.goto('/');
  await expect(page).toHaveURL(/.*/);
});
"""

SPECS_README = """\
# Test plans

`/playwright plan` writes one plan per area of the app here (`<area>.md`); `/playwright generate`
turns each into tests in `../tests/<area>/`. `site-map.md` lists the pages (`/playwright map`).
"""

GITIGNORE = "node_modules/\ntest-results/\nplaywright-report/\n"


def files(
    directory: str, url: str, *, channel: str | None, executable: str | None, start: str | None
) -> dict[str, str]:
    """Path (relative to the project) -> content, for every file of a new Playwright project."""
    return {
        f"{directory}/package.json": package_json(directory),
        f"{directory}/playwright.config.ts": config_ts(
            url, channel=channel, executable=executable, start=start
        ),
        f"{directory}/tests/seed.spec.ts": SEED_SPEC,
        f"{directory}/specs/README.md": SPECS_README,
        f"{directory}/.gitignore": GITIGNORE,
    }
