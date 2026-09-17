/**
 * 回合产物文件收集（「本轮写了哪些文件」列表）。
 *
 * 四个来源取并集：
 *
 *  1. 工作区扫描（递归）：回合的可写根（项目根 + 场景包工作区，与 exec
 *     沙箱同源）里 mtime/birthtime ≥ 回合开始时间的文件。覆盖脚本/命令
 *     间接写出的产物（如运行脚本生成的文档、报表）——这类路径无法从
 *     工具参数解析。
 *  2. 显式写工具参数：local_write_file / local_edit_file 的 path。覆盖
 *    「完整权限」模式下写到可写根之外的路径（如 ~/Downloads）。
 *  3. exec cwd 浅扫描（仅顶层一层、跳过点文件）：local_exec_shell /
 *     local_run_snippet 的实际工作目录（显式 cwd；缺省按 executor 规则
 *     回落到项目根或 home）。覆盖「脚本在 cwd 落盘」——无项目对话的
 *     exec 默认 cwd 是 home，产物（如 ~/季度报告.pdf）不在任何递归根里。
 *  4. 命令/代码文本里的绝对路径字面量：exec 命令、snippet 代码中出现的
 *     绝对路径（含 ~/ 前缀与引号包裹形式），stat + 时间水位线验证后并入。
 *     覆盖脚本写到与 cwd 无关的任意位置（如 doc.save('/tmp/x/a.pdf')）。
 *
 * 来源 3/4 都是「猜候选、靠 stat + mtime/birthtime 水位线证伪」：命令里
 * 提到的既有文件（ls /etc/hosts）时间戳旧、不会被误收；不存在的路径
 * stat 即失败。因此解析不需要可靠——与沙箱围栏「命令内嵌绝对路径无法
 * 可靠解析所以不拦」不同，展示面可以承受启发式，漏报才伤体验。
 *
 * 单趟末次扫描、无基线快照：kind 标签靠 birthtime（macOS/Windows/新版
 * Linux 文件系统可用）；birthtime 不可用时降级为 'modified'，列表本身
 * 的准确性不依赖 birthtime。
 *
 * 扫描是有界 best-effort：忽略依赖/构建缓存目录，深度与遍历条目数有硬顶，
 * 超限直接截断返回已收集部分——产物列表是展示面，永远不该拖垮回合收尾。
 */

import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

export interface TurnFile {
  /** 绝对路径（点击打开直接用）。 */
  path: string;
  /** created = 本轮新建；modified = 已有文件被改动。 */
  kind: 'created' | 'modified';
  /** 字节数（展示用）。 */
  size: number;
}

/** 扫描入参里只需要工具行动的这几个字段（与 router 的 executedActions 行结构对齐）。 */
export interface TurnFileAction {
  tool?: unknown;
  arguments?: unknown;
  success?: unknown;
}

/** 这些目录要么体量巨大（node_modules/target），要么是框架内部状态（.steerable），扫了只有噪音。 */
const IGNORED_DIR_NAMES = new Set([
  'node_modules',
  '.git',
  '.hg',
  '.svn',
  '__pycache__',
  '.venv',
  'venv',
  '.tox',
  '.next',
  '.nuxt',
  '.cache',
  'target',
  '.steerable',
]);

const IGNORED_FILE_NAMES = new Set(['.DS_Store']);

/** 单次扫描的遍历上限：深度 10、条目 10 万、结果 100 条。 */
const MAX_DEPTH = 10;
const MAX_VISITED = 100_000;
const MAX_FILES = 100;

/** 显式写工具：参数里的 path 并入产物列表（覆盖可写根之外的写入）。 */
const WRITE_TOOL_NAMES = new Set(['local_write_file', 'local_edit_file']);

/** exec 类工具：cwd 浅扫描与命令文本路径字面量的来源。 */
const EXEC_TOOL_NAMES = new Set(['local_exec_shell', 'local_run_snippet']);

/** 每条命令/代码文本最多提取的路径字面量个数（防超长命令刷 stat）。 */
const MAX_PATH_LITERALS_PER_ACTION = 50;

/**
 * 引号包裹的绝对路径字面量：python 代码里的 doc.save('/x/a.pdf')、shell
 * 里的 "/x/a.pdf"。要求以 /、~/ 或 Windows 盘符开头、带扩展名结尾。
 * 贪婪匹配到闭引号再回溯到最后一个点——目录名里带点（.venv/python3.11）
 * 不会把路径截断在中间。
 */
const QUOTED_PATH_PATTERN = /['"]((?:~\/|\/|[A-Za-z]:[\\/])[^'"]{1,300}\.[A-Za-z0-9]{1,10})['"]/g;

/**
 * 裸绝对路径字面量（无引号、无空格）：shell 重定向/参数里常见的写法。
 * 排除引号/空白/shell 元字符；URL 的 // 开头被 (?!\/) 挡掉；同样贪婪到
 * token 尾再回溯到最后一个点，(?![A-Za-z0-9]) 保证扩展名不被截断。
 */
const BARE_PATH_PATTERN =
  /(?:~\/|\/(?!\/))[^\s"'`<>|;,()[\]{}\\*?]{1,300}\.[A-Za-z0-9]{1,10}(?![A-Za-z0-9])|[A-Za-z]:\\[^\s"'`<>|;,*?]{1,300}\.[A-Za-z0-9]{1,10}(?![A-Za-z0-9])/g;

/**
 * 收集本轮产物文件。`sinceMs` 是回合开始的 epoch ms；`projectRoot` 用于把
 * 写工具的相对路径参数解析成绝对路径（与 local-executor 的项目根解析一致）。
 * `homeDir` 仅测试注入用，默认 os.homedir()——exec 缺省 cwd 的回落值。
 */
export async function collectTurnFiles(options: {
  roots: string[];
  sinceMs: number;
  actions?: readonly TurnFileAction[];
  projectRoot?: string | null;
  homeDir?: string;
}): Promise<TurnFile[]> {
  const { roots, sinceMs, projectRoot = null } = options;
  const homeDir = options.homeDir ?? os.homedir();
  const byPath = new Map<string, TurnFile>();

  for (const root of roots) {
    await scanRoot(root, sinceMs, byPath);
  }
  const shallowRoots = new Set<string>();
  for (const action of options.actions ?? []) {
    await collectWriteToolPath(action, sinceMs, projectRoot, byPath);
    const cwd = execCwdOf(action, projectRoot, homeDir);
    if (cwd && !isCoveredByRoots(cwd, roots)) shallowRoots.add(cwd);
    await collectPathLiterals(action, sinceMs, homeDir, byPath);
  }
  for (const dir of shallowRoots) {
    await scanShallow(dir, sinceMs, byPath);
  }

  return [...byPath.values()]
    .sort((a, b) => a.path.localeCompare(b.path))
    .slice(0, MAX_FILES);
}

async function scanRoot(
  root: string,
  sinceMs: number,
  out: Map<string, TurnFile>,
): Promise<void> {
  const state = { visited: 0 };
  await walk(path.resolve(root), sinceMs, 0, state, out);
}

interface WalkState {
  visited: number;
}

async function walk(
  dir: string,
  sinceMs: number,
  depth: number,
  state: WalkState,
  out: Map<string, TurnFile>,
): Promise<void> {
  if (depth > MAX_DEPTH || state.visited >= MAX_VISITED) return;
  let entries;
  try {
    entries = await fs.readdir(dir, { withFileTypes: true });
  } catch {
    // 根目录被删/无权限：跳过该根，不影响其他根。
    return;
  }
  for (const entry of entries) {
    if (state.visited >= MAX_VISITED) return;
    state.visited += 1;
    const full = path.join(dir, entry.name);
    // 不跟随符号链接：避免链接环把扫描拖出根外。
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory()) {
      if (IGNORED_DIR_NAMES.has(entry.name)) continue;
      await walk(full, sinceMs, depth + 1, state, out);
      continue;
    }
    if (!entry.isFile() || IGNORED_FILE_NAMES.has(entry.name)) continue;
    const file = await statTurnFile(full, sinceMs);
    if (file) out.set(full, file);
  }
}

/** stat 一个文件，命中「本轮触碰过」则返回 TurnFile，否则 null。 */
async function statTurnFile(full: string, sinceMs: number): Promise<TurnFile | null> {
  let stat;
  try {
    stat = await fs.stat(full);
  } catch {
    // 扫描窗口内被删掉的文件直接略过。
    return null;
  }
  // 路径字面量可能指到目录（如 xxx.app 包）；产物列表只收文件。
  if (!stat.isFile()) return null;
  const touched = stat.mtimeMs >= sinceMs || stat.birthtimeMs >= sinceMs;
  if (!touched) return null;
  return { path: full, kind: kindOf(stat, sinceMs), size: stat.size };
}

function kindOf(stat: { mtimeMs: number; birthtimeMs: number }, sinceMs: number): 'created' | 'modified' {
  // birthtime 不可用的文件系统返回 0/负值——此时无法区分新建与修改，
  // 统一标 modified（标签降级，不漏文件）。
  return stat.birthtimeMs >= sinceMs ? 'created' : 'modified';
}

async function collectWriteToolPath(
  action: TurnFileAction,
  sinceMs: number,
  projectRoot: string | null,
  out: Map<string, TurnFile>,
): Promise<void> {
  if (typeof action.tool !== 'string' || !WRITE_TOOL_NAMES.has(action.tool)) return;
  // success === false 的调用没写成；undefined（流中未落定）按成功处理——
  // 文件是否真存在由下面的 stat 把关。
  if (action.success === false) return;
  const args = action.arguments;
  if (!args || typeof args !== 'object') return;
  const raw = (args as Record<string, unknown>).path;
  if (typeof raw !== 'string' || !raw.trim()) return;
  const full = path.isAbsolute(raw)
    ? path.normalize(raw)
    : projectRoot
      ? path.resolve(projectRoot, raw)
      : null;
  if (!full || out.has(full)) return;
  const file = await statTurnFile(full, sinceMs);
  if (file) out.set(full, file);
}

/**
 * exec 行动的实际工作目录（与 tool-router / local-executor 的解析规则对齐）：
 * 显式 cwd 展开 ~ 后按绝对/相对解析（相对在项目模式下按项目根、否则按
 * 进程 cwd）；缺省 cwd 回落到项目根，无项目时回落到 home——无项目对话里
 * 脚本产物最常见的落点。
 */
function execCwdOf(
  action: TurnFileAction,
  projectRoot: string | null,
  homeDir: string,
): string | null {
  if (typeof action.tool !== 'string' || !EXEC_TOOL_NAMES.has(action.tool)) return null;
  const args = action.arguments;
  if (!args || typeof args !== 'object') return null;
  const raw = (args as Record<string, unknown>).cwd;
  const cwdArg = typeof raw === 'string' ? raw.trim() : '';
  if (!cwdArg) return projectRoot ?? homeDir;
  const expanded = cwdArg.startsWith('~') ? path.join(homeDir, cwdArg.slice(1)) : cwdArg;
  if (path.isAbsolute(expanded)) return path.normalize(expanded);
  return projectRoot ? path.resolve(projectRoot, expanded) : path.resolve(expanded);
}

/** cwd 已落在某个递归根之内时无需再浅扫（递归扫描已覆盖且更深）。 */
function isCoveredByRoots(cwd: string, roots: readonly string[]): boolean {
  return roots.some((root) => {
    const rel = path.relative(path.resolve(root), cwd);
    return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
  });
}

/**
 * exec cwd 的浅扫描：只看顶层一层、跳过点文件。home 顶层的变化几乎全是
 * 工具状态（.zsh_history 之类），点文件过滤掉它们；真正的产物文档极少
 * 以点开头。浅扫不递归——home 这种根递归起来既慢又全是无关变化。
 */
async function scanShallow(
  dir: string,
  sinceMs: number,
  out: Map<string, TurnFile>,
): Promise<void> {
  let entries;
  try {
    entries = await fs.readdir(dir, { withFileTypes: true });
  } catch {
    // cwd 被删/无权限：跳过，不影响其他来源。
    return;
  }
  for (const entry of entries) {
    if (entry.name.startsWith('.') || IGNORED_FILE_NAMES.has(entry.name)) continue;
    if (entry.isSymbolicLink() || !entry.isFile()) continue;
    const full = path.join(dir, entry.name);
    if (out.has(full)) continue;
    const file = await statTurnFile(full, sinceMs);
    if (file) out.set(full, file);
  }
}

/**
 * 从 exec 命令 / snippet 代码文本里提取绝对路径字面量，stat + 水位线
 * 验证后并入。覆盖脚本写到与 cwd 无关的任意位置。local_run_script 的
 * 脚本内容在注册表里、行动参数只有 scriptId，不在此覆盖（其 cwd 语义
 * 与 exec_shell 相同，浅扫描已兜底常见落点）。
 */
async function collectPathLiterals(
  action: TurnFileAction,
  sinceMs: number,
  homeDir: string,
  out: Map<string, TurnFile>,
): Promise<void> {
  if (typeof action.tool !== 'string' || !EXEC_TOOL_NAMES.has(action.tool)) return;
  const args = action.arguments;
  if (!args || typeof args !== 'object') return;
  const record = args as Record<string, unknown>;
  const text = [record.command, record.code]
    .filter((value): value is string => typeof value === 'string')
    .join('\n');
  if (!text) return;

  const candidates = new Set<string>();
  for (const pattern of [QUOTED_PATH_PATTERN, BARE_PATH_PATTERN]) {
    pattern.lastIndex = 0;
    for (const match of text.matchAll(pattern)) {
      if (candidates.size >= MAX_PATH_LITERALS_PER_ACTION) break;
      const literal = match[1] ?? match[0];
      const expanded = literal.startsWith('~/')
        ? path.join(homeDir, literal.slice(2))
        : literal;
      candidates.add(path.normalize(expanded));
    }
  }
  for (const full of candidates) {
    if (out.has(full)) continue;
    const file = await statTurnFile(full, sinceMs);
    if (file) out.set(full, file);
  }
}
