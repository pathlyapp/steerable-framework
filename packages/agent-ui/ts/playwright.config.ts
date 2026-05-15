import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright config for visual regression of @steerable/agent-ui.
 *
 * The suite expects a Storybook static build to be served on
 * http://127.0.0.1:6006 — the npm script `storybook:vrt` chains a fresh build
 * + a temporary http-server before running Playwright. CI mirrors that flow.
 *
 * Snapshots live next to the spec file under `tests/visual/__screenshots__/`.
 * Refresh them with `pnpm storybook:vrt --update-snapshots` after intentional
 * UI changes; the CI guard fails on any unintended diff.
 */
export default defineConfig({
  testDir: 'tests/visual',
  timeout: 30_000,
  fullyParallel: true,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: 'http://127.0.0.1:6006',
    headless: true,
    viewport: { width: 1280, height: 800 },
  },
  expect: {
    // Allow a tiny rendering jitter (font hinting, antialiasing) without
    // burying real diffs.
    toHaveScreenshot: {
      maxDiffPixelRatio: 0.005,
      animations: 'disabled',
      caret: 'hide',
    },
  },
  projects: [
    {
      name: 'chromium-desktop',
      use: {
        ...devices['Desktop Chrome'],
        deviceScaleFactor: 1,
      },
    },
  ],
  /**
   * If a Storybook static build already exists at `storybook-static/`, spin
   * up a tiny http server pointed at it for the duration of the test run.
   * In CI we expect the calling step to run `pnpm storybook:build` first;
   * locally `pnpm storybook:vrt` should be preceded by `pnpm storybook:build`
   * (or the npm script chain `storybook:build && storybook:vrt`).
   *
   * The reuseExistingServer flag avoids fighting with a long-lived `pnpm
   * storybook` dev server during interactive use.
   */
  webServer: {
    command:
      'python3 -m http.server 6006 --bind 127.0.0.1 --directory storybook-static',
    url: 'http://127.0.0.1:6006/index.json',
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
