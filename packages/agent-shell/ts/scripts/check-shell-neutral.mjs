#!/usr/bin/env node
/**
 * shell 产品中立门禁（3.1 建立，3.2 随 shell 上提到框架仓库）：
 * @steerable/agent-shell 的 TS 宿主源码（ts/src/）与渲染层源码
 * （web/src/）不允许出现任何产品硬编码（品牌名、场景包 id、产品域名、
 * 开发者机器路径）。产品身份一律由消费方产品的组装根经 setProductBrand /
 * setProductConfig / vite define / setBrandLogoUrl 注入。
 *
 * 扫描面：ts/src 与 web/src 的 .ts/.tsx/.json（含浏览器预览夹具
 * browser-dev-data.json——它会被打进官网 demo，同样面向公众），外加
 * ts/src/local-backend/skills 下的 .md（技能正文是模型可见文案，
 * 同样必须中立——身份技能用 {agentName} 占位符，有绑定智能体时渲染
 * 智能体显示名，否则回落产品品牌）。
 *
 * 豁免规则（刻意从简，能 reword 就不要豁免）：
 *  - 行内注释 `shell-neutral:allow`：线协议常量等真正无法中性化的点
 *    （必须在注释里说明对端契约归谁所有）；
 *  - 测试文件（*.test.ts(x)）：测试 fixture 允许用产品名；
 *  - DEEPPATH_ 大写环境变量名与 __DEEPPATH_BS__ 引导键：兼容存量部署，
 *    改名是消费切换时的一次性决策，不在本门禁范围；
 *  - `@deeppath/` npm scope（历史包名前缀）。
 */
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const PKG = path.resolve(ROOT); // 本包根（packages/agent-shell/ts）
const SHELL_TS = PKG;
const SHELL_WEB = path.resolve(PKG, '..', 'web');
const SCAN_DIRS = [path.join(SHELL_TS, 'src'), path.join(SHELL_WEB, 'src')];
// dist 是 src 的构建产物，src 干净则 dist 干净——但只对「新鲜」的 dist 成立。
// 一次先清洗 src 再隔数小时才重建 dist 的提交（def4cdd）证明：stale dist 能把
// 已清掉的产品词重新带进发布物。既然 agent-shell 要上 npm、发布物就是 dist，
// 门禁必须把 dist 纳入扫描面（存在才扫，未构建时跳过）。
const DIST_DIR = path.join(SHELL_TS, 'dist');
const SKILL_MD_DIR = path.join(SHELL_TS, 'src', 'local-backend', 'skills');
const FILE_RE = /\.(ts|tsx|json)$/;
const DIST_FILE_RE = /\.(js|d\.ts|cjs|md|json|html)$/;
const SKILL_MD_RE = /\.md$/;
const EXEMPT_FILES = new Set();
const ALLOW_MARK = 'shell-neutral:allow';

/** 命中即违规的字面量（大小写敏感；按需扩列，扩列即加重门禁）。 */
const FORBIDDEN = [
  '时踪',
  '亦庄',
  '测井',
  'CIFLog',
  'ciflog',
  'CIFLOG',
  'cflog',
  'moduflow',
  'ModuFlow',
  'MODUFLOW',
  'etown',
  'deeppath.cc',
  'deeppath.cloud',
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
// src 里带 `shell-neutral:allow` 的行所豁免的「字符串字面量」集合。tsc 编译成
// .d.ts 时会剥掉行内注释，导致 dist 里同一常量（如 INSIGHTS_EXPORT_SCHEMA 的
// 线协议值）失去豁免标记而被误报。扫 dist 前先从 src 收集这些已豁免的字面量，
// dist 行命中已豁免字面量即放行——豁免的判定仍在 src 一处做出，dist 只是继承。
const srcAllowedLiterals = new Set();
function collectSrcAllowed() {
  for (const dir of SCAN_DIRS) {
    for (const file of walk(dir)) {
      if (!FILE_RE.test(file)) continue;
      const lines = readFileSync(file, 'utf8').split('\n');
      for (const line of lines) {
        if (!line.includes(ALLOW_MARK)) continue;
        for (const m of line.matchAll(/['"`]([^'"`]+)['"`]/g)) {
          if (FORBIDDEN.some((tok) => m[1].includes(tok))) srcAllowedLiterals.add(m[1]);
        }
      }
    }
  }
}

function scanFile(file, { isDist = false } = {}) {
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
        // dist 行若只是继承了 src 已豁免的字面量，放行。
        if (isDist && [...srcAllowedLiterals].some((lit) => line.includes(lit))) continue;
        violations.push(`${rel}:${i + 1}: 含产品耦合字符串 "${token}" → ${line.trim().slice(0, 100)}`);
      }
    }
  }
}

collectSrcAllowed();
for (const dir of SCAN_DIRS) {
  for (const file of walk(dir)) {
    if (FILE_RE.test(file)) scanFile(file);
  }
}
// 发布物（dist）同属扫描面：存在才扫，未构建时跳过。技能 .md 与编译 .js 都查。
if (existsSync(DIST_DIR)) {
  for (const file of walk(DIST_DIR)) {
    if (DIST_FILE_RE.test(file)) scanFile(file, { isDist: true });
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
