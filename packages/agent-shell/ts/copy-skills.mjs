#!/usr/bin/env node
/**
 * 把 shell 自带技能（src/local-backend/skills/<id>/SKILL.md 等非 TS 资产）
 * 拷进 dist 镜像位置（dist/local-backend/skills/）。tsc 只编译 TS；技能
 * 是运行时由 skill-loader 按 __dirname 相对路径 fs 读的数据文件。
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, 'src', 'local-backend', 'skills');
const DST = path.join(HERE, 'dist', 'local-backend', 'skills');

async function exists(p) {
  try {
    await fs.access(p);
    return true;
  } catch {
    return false;
  }
}

if (!(await exists(SRC))) {
  console.warn('[agent-shell copy-skills] no skills dir at', SRC, '-- skipping');
  process.exit(0);
}

await fs.rm(DST, { recursive: true, force: true });
await fs.mkdir(DST, { recursive: true });
await fs.cp(SRC, DST, { recursive: true });
const entries = await fs.readdir(DST, { withFileTypes: true });
console.log(`[agent-shell copy-skills] ${entries.filter((e) => e.isDirectory()).length} skills → dist/local-backend/skills`);
