#!/usr/bin/env node
import { readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const sourceRoot = path.join(root, 'src');
const allowed = new Set([
  path.join(sourceRoot, 'storage', 'index.ts'),
  path.join(sourceRoot, 'storage', 'sqlite-driver.ts'),
  path.join(sourceRoot, 'storage', 'write-lease.ts'),
  // Reads the separate sidecar sessions database; host StorageDriver does not own it.
  path.join(sourceRoot, 'local-backend', 'task-process.ts'),
]);
const violations = [];

function walk(directory) {
  for (const name of readdirSync(directory)) {
    const file = path.join(directory, name);
    if (statSync(file).isDirectory()) {
      walk(file);
      continue;
    }
    if (!file.endsWith('.ts') || allowed.has(file)) continue;
    const source = readFileSync(file, 'utf8');
    if (/(?:from\s*|import\s*\()\s*['"]better-sqlite3['"]/.test(source)) {
      violations.push(`${path.relative(root, file)} imports better-sqlite3`);
    }
    if (/\.\s*prepare\s*\(/.test(source)) {
      violations.push(`${path.relative(root, file)} calls raw SQLite prepare()`);
    }
  }
}

walk(sourceRoot);
if (violations.length > 0) {
  console.error(`storage boundary violations (${violations.length}):`);
  for (const violation of violations) console.error(`  ${violation}`);
  process.exit(1);
}
console.log('storage boundaries OK');
