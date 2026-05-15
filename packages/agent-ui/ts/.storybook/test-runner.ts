/**
 * Storybook test-runner config.
 *
 * For every story we mount via Playwright we:
 *   1. Inject `axe-core` into the iframe,
 *   2. Run `checkA11y` with the story's parent body as the root,
 *   3. Surface any "serious" / "critical" violations as test failures.
 *
 * Stories that opt out of a11y checks (e.g. intentionally low-contrast debug
 * surfaces) can set `parameters.a11y = { test: 'off' }` in their CSF file.
 *
 * Usage:
 *   pnpm storybook       # in one terminal — Storybook on :6006
 *   pnpm storybook:test  # in another — runs the test-runner
 *
 * In CI we serve the static build with `http-server` and pass
 * `--url http://127.0.0.1:6006` directly.
 */
import type { TestRunnerConfig } from '@storybook/test-runner';
import { getStoryContext } from '@storybook/test-runner';
import { injectAxe, checkA11y, configureAxe } from 'axe-playwright';

const config: TestRunnerConfig = {
  async preVisit(page) {
    await injectAxe(page);
  },
  async postVisit(page, context) {
    const story = await getStoryContext(page, context);
    const a11yParameters = story.parameters?.a11y as
      | { test?: 'off' | 'todo' | 'error'; config?: unknown; options?: unknown }
      | undefined;
    if (a11yParameters?.test === 'off') return;

    if (a11yParameters?.config) {
      await configureAxe(page, a11yParameters.config as Parameters<typeof configureAxe>[1]);
    }

    await checkA11y(page, '#storybook-root', {
      detailedReport: true,
      detailedReportOptions: { html: true },
      axeOptions: {
        runOnly: {
          type: 'tag',
          values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'],
        },
      },
    });
  },
};

export default config;
