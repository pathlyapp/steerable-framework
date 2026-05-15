/**
 * Visual regression for every Storybook component story.
 *
 * Strategy:
 *   1. Fetch `index.json` from the served Storybook build to enumerate the
 *      complete set of stories (including any future ones — the suite stays
 *      in sync without manual edits).
 *   2. Filter to runnable stories (`type === 'story'`) and skip MDX docs.
 *   3. Open each story in the iframe at `?id=<storyId>&viewMode=story`,
 *      wait for fonts + the story root, and take a viewport snapshot.
 *
 * Snapshots are deterministic across CI / local because we disable animations
 * and set a fixed viewport in playwright.config.ts.
 */
import { expect, test } from '@playwright/test';

interface StoryIndexEntry {
  id: string;
  title: string;
  name: string;
  type: 'story' | 'docs';
  importPath: string;
  tags?: string[];
}

interface StoryIndex {
  v: number;
  entries: Record<string, StoryIndexEntry>;
}

async function loadStoryIndex(baseURL: string): Promise<StoryIndexEntry[]> {
  const res = await fetch(`${baseURL}/index.json`);
  if (!res.ok) {
    throw new Error(
      `Failed to fetch Storybook index at ${baseURL}/index.json: ${res.status}`,
    );
  }
  const json = (await res.json()) as StoryIndex;
  return Object.values(json.entries).filter((e) => e.type === 'story');
}

const BASE = process.env.STORYBOOK_URL ?? 'http://127.0.0.1:6006';

const indexPromise = loadStoryIndex(BASE);

test.describe('@steerable/agent-ui visual regression', () => {
  // Static enumeration so the per-story `test()` blocks register before the
  // suite starts. Playwright doesn't allow `test.beforeAll(async)` to spawn
  // tests, so we resolve the index synchronously via a top-level await.
  // Workaround: we register a single dynamic block that fans out at runtime.
  test('snapshots every component story', async ({ page }, testInfo) => {
    const stories = await indexPromise;
    if (stories.length === 0) {
      throw new Error('No stories found — did Storybook build fail?');
    }

    const failures: Array<{ id: string; error: string }> = [];

    for (const story of stories) {
      // Skip MDX-derived "docs only" entries that occasionally slip through.
      if (story.tags?.includes('mdx-docs')) continue;
      // Skip the Interactive ChatPanel story — it relies on user input and
      // would just snapshot the same idle state as `WithMessages`.
      if (story.id === 'components-chatpanel--interactive') continue;

      const url = `${BASE}/iframe.html?id=${story.id}&viewMode=story`;
      try {
        await page.goto(url, { waitUntil: 'load' });
        await page.evaluate(() => document.fonts?.ready);
        // Storybook injects #storybook-root once the renderer mounts.
        await page.waitForSelector('#storybook-root', { state: 'attached' });
        // A short settle to ensure layout / images stabilised.
        await page.waitForTimeout(200);

        const filename = `${story.id}.png`;
        await expect(page).toHaveScreenshot(filename, {
          fullPage: false,
        });
      } catch (err) {
        failures.push({
          id: story.id,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    }

    if (failures.length > 0) {
      const summary = failures
        .map((f) => `  • ${f.id}: ${f.error.split('\n')[0]}`)
        .join('\n');
      testInfo.attach('failures.json', {
        body: JSON.stringify(failures, null, 2),
        contentType: 'application/json',
      });
      throw new Error(
        `${failures.length} story snapshot(s) regressed:\n${summary}`,
      );
    }
  });
});
