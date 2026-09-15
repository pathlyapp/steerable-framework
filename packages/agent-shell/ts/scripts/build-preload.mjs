#!/usr/bin/env node
/**
 * shell 默认 preload 打包：esbuild 把 src/preload-default.ts（中性面，
 * 无包贡献）打成单文件 CJS → dist/preload.cjs，与 main.ts 的默认
 * preload 路径（getPreloadPath 回退值）同目录。
 *
 * 为什么 CJS 单文件：Electron preload 的 ESM 支持有边角（sandbox 限制），
 * CJS 最稳；bundle 成单文件避免与 ESM 主产物撞同名 .js（"type":
 * "module" 下 .js 按 ESM 解释）。electron 模块由运行时提供，保持
 * external。带包贡献的产品 preload 由消费方产品构建（应用仓
 * scripts/build-preload.mjs）出在产品 main.js 旁，不经本脚本。
 */
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const PKG = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const outfile = path.join(PKG, 'dist', 'preload.cjs');

await build({
  entryPoints: [path.join(PKG, 'src', 'preload-default.ts')],
  bundle: true,
  format: 'cjs',
  platform: 'node',
  target: 'node20',
  external: ['electron'],
  outfile,
  logLevel: 'silent',
});
console.log(`[agent-shell build-preload] preload-default.ts → ${path.relative(PKG, outfile)}`);
