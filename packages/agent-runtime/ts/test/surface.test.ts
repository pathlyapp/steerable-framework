/**
 * 3.2.4 conformance gate: the TS runtime's API surface must cover exactly
 * the method surface the Python sidecar registers — no missing wrapper
 * (a sidecar method TS callers cannot reach) and no phantom wrapper (a TS
 * method calling an RPC the sidecar no longer has).
 *
 * Source of truth on the Python side: the `register("<method>", …)` lines
 * in `steerable_sidecar/sidecar.py`. Source of truth on the TS side:
 * `SIDECAR_METHODS` in `src/methods.ts`, plus a scan of `src/runtime.ts`
 * proving each method is actually wired to a `this.process.request` call.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { SIDECAR_METHODS } from '../src/methods.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../../../..');
const SIDECAR_PY = path.join(
  REPO,
  'packages/sidecar/py/src/steerable_sidecar/sidecar.py',
);
const RUNTIME_TS = path.join(HERE, '../src/runtime.ts');

function pythonRegisteredMethods(): string[] {
  const src = readFileSync(SIDECAR_PY, 'utf8');
  const methods = [...src.matchAll(/register\("([a-z._]+)"/g)].map((m) => m[1]);
  return [...new Set(methods)].sort();
}

describe('TS API surface ↔ sidecar method surface', () => {
  it('SIDECAR_METHODS matches every register() in the Python sidecar', () => {
    const py = pythonRegisteredMethods();
    const ts = [...SIDECAR_METHODS].sort();
    expect(ts).toEqual(py);
  });

  it('every declared method is requested somewhere in runtime.ts', () => {
    const src = readFileSync(RUNTIME_TS, 'utf8');
    const wired = new Set(
      [...src.matchAll(/request(?:<[^>]*>)?\(\s*'([a-z._]+)'/g)].map((m) => m[1]),
    );
    for (const method of SIDECAR_METHODS) {
      // system.* is served by SidecarProcess itself (ping/shutdown), not the
      // runtime facade — everything else must have a typed wrapper.
      if (method.startsWith('system.')) continue;
      expect(wired.has(method), `missing runtime wrapper for ${method}`).toBe(true);
    }
  });
});
