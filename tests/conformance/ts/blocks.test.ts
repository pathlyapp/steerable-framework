/**
 * Validates every fixture in `spec/blocks/fixtures/*.json` against its
 * matching schema in `spec/blocks/*.schema.json` using Ajv. This is the
 * schema-fixture drift guard the wave-3 plan calls for: schemas evolve as
 * backend payloads do, and these fixtures are the canonical examples that
 * `examples/web-shell` consumes -- so we must catch breaks at PR time
 * rather than at runtime in the shell.
 */
import { readdirSync, readFileSync, existsSync } from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import Ajv2020 from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, '..', '..', '..');
const blocksDir = path.join(repoRoot, 'spec', 'blocks');
const fixturesDir = path.join(blocksDir, 'fixtures');

const schemaFiles = readdirSync(blocksDir).filter((f) => f.endsWith('.schema.json'));

describe('block fixtures conform to their schemas', () => {
  for (const schemaFile of schemaFiles) {
    const schema = JSON.parse(readFileSync(path.join(blocksDir, schemaFile), 'utf8'));
    const title = schema.title as string;
    const fixturePath = path.join(fixturesDir, `${title}.json`);
    if (!existsSync(fixturePath)) continue;

    it(`${title} fixture matches schema`, () => {
      const ajv = new Ajv2020({ strict: false, allErrors: true });
      addFormats(ajv);
      const validate = ajv.compile(schema);
      const fixture = JSON.parse(readFileSync(fixturePath, 'utf8'));
      const ok = validate(fixture);
      if (!ok) {
        const message = (validate.errors ?? [])
          .map((e) => `${e.instancePath || '/'} ${e.message}`)
          .join('\n');
        throw new Error(`Fixture for ${title} failed validation:\n${message}`);
      }
      expect(ok).toBe(true);
    });
  }

  it('every block schema has a fixture', () => {
    const missing: string[] = [];
    for (const schemaFile of schemaFiles) {
      const schema = JSON.parse(readFileSync(path.join(blocksDir, schemaFile), 'utf8'));
      const title = schema.title as string;
      const fixturePath = path.join(fixturesDir, `${title}.json`);
      if (!existsSync(fixturePath)) missing.push(title);
    }
    expect(missing, `Schemas missing fixtures: ${missing.join(', ')}`).toEqual([]);
  });
});
