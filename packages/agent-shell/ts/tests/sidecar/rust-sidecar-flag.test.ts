import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  resolveRustSidecarBin,
  rustSidecarEnabled,
  RUST_SIDECAR_BIN_ENV,
  RUST_SIDECAR_ENV,
} from '../../src/sidecar/supervisor.js';

describe('Rust sidecar flag fallback', () => {
  let scratch = '';
  let prevFlag: string | undefined;
  let prevBin: string | undefined;

  beforeEach(() => {
    scratch = mkdtempSync(join(tmpdir(), 'rust-sidecar-'));
    prevFlag = process.env[RUST_SIDECAR_ENV];
    prevBin = process.env[RUST_SIDECAR_BIN_ENV];
    delete process.env[RUST_SIDECAR_ENV];
    delete process.env[RUST_SIDECAR_BIN_ENV];
  });

  afterEach(() => {
    if (prevFlag === undefined) delete process.env[RUST_SIDECAR_ENV];
    else process.env[RUST_SIDECAR_ENV] = prevFlag;
    if (prevBin === undefined) delete process.env[RUST_SIDECAR_BIN_ENV];
    else process.env[RUST_SIDECAR_BIN_ENV] = prevBin;
    rmSync(scratch, { recursive: true, force: true });
  });

  it('is off unless STEERABLE_RUST_SIDECAR is set', () => {
    expect(rustSidecarEnabled()).toBe(false);
    process.env[RUST_SIDECAR_ENV] = '1';
    expect(rustSidecarEnabled()).toBe(true);
  });

  it('resolves an explicit binary and ignores a missing env path', () => {
    const bin = join(scratch, 'steerable-sidecar');
    writeFileSync(bin, '');
    expect(resolveRustSidecarBin(bin)).toBe(bin);
    process.env[RUST_SIDECAR_BIN_ENV] = join(scratch, 'missing');
    expect(resolveRustSidecarBin()).toBeUndefined();
    process.env[RUST_SIDECAR_BIN_ENV] = bin;
    expect(resolveRustSidecarBin()).toBe(bin);
  });
});
