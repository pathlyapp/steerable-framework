/**
 * @steerable/agent-ui
 *
 * Entry point. Re-exports the public hooks + components surface.
 *
 * The Tailwind preset is exposed via the `./tailwind-preset` subpath, not from
 * the root, so consumers don't pull a build-time dependency into their app
 * runtime bundle. See `tsconfig.json` `exports` map.
 */

export * from './hooks/index.js';
export * from './components/index.js';
