#!/usr/bin/env node
/**
 * shell 产品中立门禁（3.1 建立，3.2 随 shell 上提到框架仓库）：
 * @steerable/agent-shell 的 TS 宿主源码（ts/src/）与渲染层源码
 * （web/src/）不允许出现任何产品硬编码（品牌名、场景包 id、产品域名、
 * 开发者机器路径）。产品身份一律由消费方产品的组装根经 setProductBrand /
 * setProductConfig / vite define / setBrandLogoUrl 注入。
 *
 * 扫描面：ts/src 与 web/src 的 .ts/.tsx，外加 ts/src/local-backend/skills
 * 下的 .md（技能正文是模型可见文案，同样必须中立——身份技能用
 * {agentName} 占位符由产品品牌渲染）。
 *
 * 豁免规则（刻意从简，能 reword 就不要豁免）：
 *  - 行内注释 `shell-neutral:allow`：线协议常量等真正无法中性化的点
 *    （必须在注释里说明对端契约归谁所有）；
 *  - 测试文件（*.test.ts(x)）：测试 fixture 允许用产品名；
 *  - DEEPPATH_ 大写环境变量名与 __DEEPPATH_BS__ 引导键：兼容存量部署，
 *    改名是消费切换时的一次性决策，不在本门禁范围；
 *  - `@deeppath/` npm scope（历史包名前缀）。
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const PKG = path.resolve(ROOT); // 本包根（packages/agent-shell/ts）
const SHELL_TS = PKG;
const SHELL_WEB = path.resolve(PKG, '..', 'web');
const SCAN_DIRS = [path.join(SHELL_TS, 'src'), path.join(SHELL_WEB, 'src')];
const SKILL_MD_DIR = path.join(SHELL_TS, 'src', 'local-backend', 'skills');
const FILE_RE = /\.(ts|tsx)$/;
const SKILL_MD_RE = /\.md$/;
const EXEMPT_FILES = new Set();
const ALLOW_MARK = 'shell-neutral:allow';

/** 命中即违规的字面量（大小写敏感；按需扩列，扩列即加重门禁）。 */
const FORBIDDEN = [
  '时踪',
  '亦庄',
  'CIFLog',
  'ciflog',
  'CIFLOG',
  'cflog',
  'etown',
  'deeppath.cc',
  'deeppath-agent',
  'DeepPath',
  '/Users/',
  'ppt',
];

/** 白名单 token：命中这些的行不算违规（先剥白再扫黑）。 */
const WHITELIST = ['@deeppath/', 'DEEPPATH_', '__DEEPPATH_BS__'];

function* walk(dir) {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) {
      yield* walk(full);
    } else {
      yield full;
    }
  }
}

const violations = [];
function scanFile(file) {
  const rel = path.relative(PKG, file);
  if (EXEMPT_FILES.has(rel)) return;
  if (/\.test\.(ts|tsx)$/.test(file)) return;
  const lines = readFileSync(file, 'utf8').split('\n');
  for (let i = 0; i < lines.length; i++) {
    let line = lines[i];
    if (line.includes(ALLOW_MARK)) continue;
    for (const token of WHITELIST) line = line.split(token).join('');
    for (const token of FORBIDDEN) {
      if (line.includes(token)) {
        violations.push(`${rel}:${i + 1}: 含产品耦合字符串 "${token}" → ${line.trim().slice(0, 100)}`);
      }
    }
  }
}

for (const dir of SCAN_DIRS) {
  for (const file of walk(dir)) {
    if (FILE_RE.test(file)) scanFile(file);
  }
}
// 技能正文（.md）同属扫描面。
for (const file of walk(SKILL_MD_DIR)) {
  if (SKILL_MD_RE.test(file)) scanFile(file);
}

if (violations.length > 0) {
  console.error('[shell-neutral] 违规（shell 不得含产品硬编码；产品身份由产品组装根注入）：');
  for (const v of violations) console.error('  ' + v);
  console.error(
    '\n修复方式：把产品值移到消费方产品的 product.json（或包声明）并注入；' +
      '线协议常量等确需保留的行加 `shell-neutral:allow` 注释并说明对端契约。',
  );
  process.exit(1);
}
console.log('[shell-neutral] OK — shell（ts/src + web/src + 技能正文）无产品硬编码。');
