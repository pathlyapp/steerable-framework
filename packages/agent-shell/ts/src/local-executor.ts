import { shellOpenExternal, shellOpenPath } from './runtime.js';
import { access, mkdir, readFile, rename, stat, writeFile } from 'fs/promises';
import os from 'os';
import path from 'path';
import { createHash } from 'crypto';
import { spawn, execSync } from 'child_process';
import log from 'electron-log';
import { constants as fsConstants } from 'fs';
import { adaptCommandForPowerShell } from './shell-adapt.js';
import { applyEdits, EditError, type ApplyEditsResult, type EditOp } from './local-edit.js';
// `he` ships as CommonJS and does not expose ESM named exports, so we have to
// take the default import and destructure at runtime in this ESM module.
import he from 'he';
const { decode } = he;

let cachedWineCommand: string | null | undefined = undefined;

export function getWineCommand(): string | null {
  if (cachedWineCommand !== undefined) return cachedWineCommand;
  if (process.platform === 'win32') {
    cachedWineCommand = null;
    return null;
  }
  try {
    try {
      execSync('which wine64', { stdio: 'ignore' });
      cachedWineCommand = 'wine64';
    } catch {
      try {
        execSync('which wine', { stdio: 'ignore' });
        cachedWineCommand = 'wine';
      } catch {
        cachedWineCommand = null;
      }
    }
  } catch {
    cachedWineCommand = null;
  }
  return cachedWineCommand;
}

export function rewriteExeCommandIfNeeded(command: string): string {
  if (process.platform === 'win32') return command;
  if (!command) return command;

  const runner = getWineCommand() || 'wine';
  const exeBoundaryRegex = /(^|[&|;({]\s*)\s*("([^"]+\.exe)"|'([^']+\.exe)'|([^\s]+\.exe))/gim;
  
  return command.replace(exeBoundaryRegex, (match, prefix, fullExePath) => {
    return `${prefix || ''}${runner} ${fullExePath}`;
  });
}

export type ShellType = 'zsh' | 'bash' | 'wsl' | 'powershell' | 'cmd';

export interface LocalExecRequest {
  command: string;
  cwd?: string;
  timeout?: number;
  env?: Record<string, string>;
  shell?: ShellType;
}

export interface LocalExecResult {
  success: boolean;
  stdout?: string;
  stderr?: string;
  exitCode?: number;
  error?: string;
  truncated?: boolean;
  shell?: ShellType;
  platform?: string;
  /** 命令因超时被终止（不代表命令本身出错——长任务 / GUI 程序常见）。 */
  timedOut?: boolean;
  /**
   * GUI/长交互命令超时后进程未被终止，仍在后台运行。此时 success=true：
   * "程序已启动并在运行" 对启动类命令就是成功，不能让 LLM 当失败重试。
   */
  stillRunning?: boolean;
}

export interface LocalFileReadRequest {
  path: string;
  encoding?: BufferEncoding;
  maxSize?: number;
  /** 1-based 起始行（CC Read 风格分页）。与 limit 配合做部分视图读取。 */
  offset?: number;
  /** 最多返回的行数（配合 offset 分页）；缺省返回到文件尾。 */
  limit?: number;
}

export interface LocalFileReadResult {
  success: boolean;
  content?: string;
  /** 内容版本令牌（SHA-256）。读后写场景把它回传给 edit/write 做冲突检测。 */
  version?: string;
  /**
   * 本次返回的只是部分视图（offset/limit 分页或超长裁剪）——模型没看到完整
   * 内容，本会话对该路径的整体覆写将被拒绝（框架 partial_reads / CC
   * isPartialView 对齐）；定点修改走 local_edit_file 不受此限。
   */
  partial?: boolean;
  error?: string;
}

export interface LocalFileWriteRequest {
  path: string;
  content: string;
  encoding?: BufferEncoding;
  createDirs?: boolean;
  /** 可选：期望的当前版本令牌（read 返回的 version）。不匹配则拒绝写入。 */
  expectedVersion?: string;
}

export interface LocalFileWriteResult {
  success: boolean;
  /** 写入后的新版本令牌。 */
  version?: string;
  error?: string;
}

/** local_edit_file 的单条编辑。 */
export interface LocalFileEditOp extends EditOp {}

export interface LocalFileEditRequest {
  path: string;
  edits: LocalFileEditOp[];
  encoding?: BufferEncoding;
  /** 可选：期望的当前版本令牌（read 返回的 version）。不匹配则拒绝（冲突）。 */
  expectedVersion?: string;
}

export interface LocalFileEditResult {
  success: boolean;
  /** 编辑后的新版本令牌。 */
  version?: string;
  /** 统一 diff 预览（供工具卡渲染）。 */
  diff?: string;
  /** 每条编辑的命中信息（定位级别 / 起始行）。 */
  matches?: Array<{ level: string; startLine: number; oldLineCount: number }>;
  /** 命中的编辑条数。 */
  applied?: number;
  error?: string;
}

export interface LocalOpenRequest {
  target: string;
}

export interface LocalOpenResult {
  success: boolean;
  error?: string;
}

/* ---------------- 主动编程（local_run_snippet） ---------------- */

export type RunCodeLanguage = 'python' | 'node';

export interface LocalRunCodeRequest {
  language: string;
  code: string;
  cwd?: string;
  timeout?: number;
  /** 运行前要 pip 安装的依赖（如 ["pandas", "openpyxl"]）。 */
  pipPackages?: string[];
  /** 运行前要 npm 安装到 scratch 目录的依赖（如 ["xlsx"]）。 */
  npmPackages?: string[];
}

export interface LocalRunCodeResult extends LocalExecResult {
  language?: RunCodeLanguage;
  /** 实际使用的解释器命令/路径（venv 兜底时是 venv 里的 python）。 */
  interpreter?: string;
  /** 代码片段落盘位置（保留不删，便于排查/复用）。 */
  scriptPath?: string;
  /** 本次调用实际安装成功的依赖。 */
  installedPackages?: string[];
}

/**
 * 依赖名白名单（防 shell 注入——包名会拼进命令行）。不满足即拒绝该参数：
 * - pip：`requests`、`requests[security]>=2.0`、`pandas~=2.2`
 * - npm：`xlsx`、`@scope/pkg`、`pkg@^1.2.3`、`pkg@latest`
 */
const PIP_PACKAGE_RE = /^[A-Za-z0-9][A-Za-z0-9._\-[\](),=<>!~+]*$/;
const NPM_PACKAGE_RE = /^(@[A-Za-z0-9._-]+\/)?[A-Za-z0-9._-]+(@[A-Za-z0-9._~^<>=+*/xX|-]+)?$/;

const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_MAX_OUTPUT_BYTES = 100 * 1024;
const DEFAULT_MAX_READ_SIZE = 1024 * 1024;
/**
 * 单次读取返回给模型的字符上限（框架 workspace_tools `_MAX_OUTPUT` 对齐）。
 * 超出部分头尾裁剪并打标记——保留尾部，日志/编译错误的关键行通常在末尾。
 */
const MAX_READ_OUTPUT_CHARS = 100_000;

/**
 * 部分视图裁剪（框架 `_clip` / CC Read 对齐）。version 永远基于完整内容计算，
 * 部分视图的 CAS 令牌保持有效；返回的 partial 标记驱动整体覆写门。
 *
 * - 给了 offset/limit：按 1-based 行号切片，并在省略处打行数标记（含继续
 *   分页的下一个 offset）；
 * - 没给但内容超长：头尾裁剪，中间打省略字符数标记。
 */
export function slicePartialView(
  content: string,
  offset?: number,
  limit?: number,
): { content: string; partial: boolean } {
  if (offset !== undefined || limit !== undefined) {
    const start = offset ?? 1;
    if (!Number.isInteger(start) || start < 1) {
      throw new Error(`offset 必须是 >= 1 的整数（1-based 行号），收到 ${offset}`);
    }
    if (limit !== undefined && (!Number.isInteger(limit) || limit < 1)) {
      throw new Error(`limit 必须是 >= 1 的整数，收到 ${limit}`);
    }
    const lines = content.split('\n');
    if (start > lines.length) {
      throw new Error(`offset ${start} 超出文件行数（共 ${lines.length} 行）。`);
    }
    const end = limit === undefined ? lines.length : Math.min(lines.length, start - 1 + limit);
    let view = lines.slice(start - 1, end).join('\n');
    const omittedAbove = start - 1;
    const omittedBelow = lines.length - end;
    if (omittedAbove > 0) {
      view = `...[{上文省略 ${omittedAbove} 行}]...\n${view}`;
    }
    if (omittedBelow > 0) {
      view = `${view}\n...[{下文省略 ${omittedBelow} 行；用 offset=${end + 1} 继续}]...`;
    }
    return { content: view, partial: omittedAbove > 0 || omittedBelow > 0 };
  }
  if (content.length <= MAX_READ_OUTPUT_CHARS) {
    return { content, partial: false };
  }
  const head = Math.floor(MAX_READ_OUTPUT_CHARS / 5);
  const tail = MAX_READ_OUTPUT_CHARS - head;
  const omitted = content.length - head - tail;
  return {
    content: `${content.slice(0, head)}\n...[{省略 ${omitted} 字符；用 offset/limit 分段读取}]...\n${content.slice(-tail)}`,
    partial: true,
  };
}

/** 内容版本令牌：UTF-8 内容的 SHA-256。用于 read-before-write 冲突检测。 */
/**
 * read-before-write 冲突令牌：内容的 SHA-256（UTF-8）十六进制。与框架
 * `steerable_sidecar.file_edit.content_version` 是同一算法（WS4 契约
 * `versionAlgorithm: sha256-utf8-hex`），两侧由共享测试向量防漂移。
 * 导出供 `tests/tool-contract.test.ts` 断言算法一致性。
 */
export function hashContent(content: string): string {
  return createHash('sha256').update(content, 'utf-8').digest('hex');
}

/**
 * P2b 硬门开关（与框架 `STEERABLE_REQUIRE_READ_BEFORE_WRITE` 同名同语义）：
 * 默认开启（CC 硬门对齐）：对已存在但未在本会话读过（也无显式
 * expectedVersion）的文件做 write/edit 直接拒绝。新建文件永不拦截。
 * 设为 0/false/no/off 可关闭。调用时读取以便测试切换。
 */
function requireReadBeforeWrite(): boolean {
  const v = (process.env.STEERABLE_REQUIRE_READ_BEFORE_WRITE ?? '').trim().toLowerCase();
  return v !== '0' && v !== 'false' && v !== 'no' && v !== 'off';
}

async function fileExists(filePath: string): Promise<boolean> {
  try {
    await access(filePath);
    return true;
  } catch {
    return false;
  }
}

/**
 * GUI 命令（见 isGuiLaunchCommand）没显式给 timeout 时的等待时长：只等一小段
 * 时间捕获"立即启动失败"类错误；到点后按"已启动、仍在运行"成功返回，不杀进程。
 */
export const GUI_LAUNCH_WAIT_MS = 15_000;

/**
 * 用户在设置界面配置的"命令默认超时"（毫秒）。null = 用内置 DEFAULT_TIMEOUT_MS。
 * 放在模块级（而不是 LocalStore）是因为本模块被 vitest 直接 import，不能拖入
 * better-sqlite3/electron 依赖；main 进程在启动和保存设置时调用 setter 同步。
 */
let configuredDefaultTimeoutMs: number | null = null;

export function setDefaultExecTimeoutMs(ms: number | null | undefined): void {
  configuredDefaultTimeoutMs = typeof ms === 'number' && ms >= 1000 ? ms : null;
}

export function getDefaultExecTimeoutMs(): number {
  return configuredDefaultTimeoutMs ?? DEFAULT_TIMEOUT_MS;
}

/** 用户配置的默认超时；未配置返回 null（调用方可用各自的内置默认）。 */
export function getConfiguredExecTimeoutMs(): number | null {
  return configuredDefaultTimeoutMs;
}

/**
 * 判断命令是否在启动 GUI/交互式程序。场景应用启动图形图件的命令行约定
 * 带有 "gui" 字样（如 `xxx.exe gui replay ...`、`--gui`、`start_gui.bat`），
 * 以非字母数字为边界匹配独立的 "gui" token，避免误伤 guide/guid 之类的词。
 *
 * 这类命令的语义是"把程序拉起来给用户操作"，等不到退出码是常态——超时后
 * 不应杀进程、更不应判失败重跑（那会把 Qt 图件重复启动多次）。
 */
export function isGuiLaunchCommand(command: string): boolean {
  return /(^|[^a-z0-9])gui([^a-z0-9]|$)/i.test(command || '');
}

/**
 * 项目模式的路径围栏：判断 resolvedPath 是否落在 root 之内（含 root 本身）。
 * 两个入参都应先经过 resolve（`~` 展开 + path.resolve）——本函数只做纯
 * 字符串/相对路径判断，不碰 fs，方便 ToolRouter 与 LocalExecutor 共用。
 */
export function isPathWithinRoot(resolvedPath: string, resolvedRoot: string): boolean {
  const rel = path.relative(resolvedRoot, resolvedPath);
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}

/** 越界错误消息（给模型看，引导它把操作收回项目目录内）。 */
export function buildProjectRootViolation(resolvedPath: string, resolvedRoot: string): string {
  return (
    `路径越界：${resolvedPath} 不在当前对话绑定的项目目录 ${resolvedRoot} 内。` +
    `项目模式下文件操作被限制在项目目录中——请改用项目内的相对/绝对路径；` +
    `如确需访问目录外文件，请告知用户该限制并请其把文件放进项目目录。`
  );
}

export interface CommandSafetyConfigPayload {
  disabledPatternIds: string[];
  customPatterns: Array<{
    id: string;
    label: string;
    pattern: string;
    category: string;
    enabled: boolean;
  }>;
}

interface BuiltinPatternDef {
  id: string;
  pattern: string;
  platform: 'unix' | 'windows' | 'all';
}

const BUILTIN_DANGEROUS: BuiltinPatternDef[] = [
  { id: 'rm_rf_root', pattern: 'rm\\s+-rf\\s+\\/(?:\\s|$)', platform: 'unix' },
  { id: 'sudo', pattern: '\\bsudo\\s', platform: 'unix' },
  { id: 'mkfs', pattern: '\\bmkfs\\b', platform: 'unix' },
  { id: 'dd_if', pattern: '\\bdd\\s+if=', platform: 'unix' },
  { id: 'chmod_777_root', pattern: 'chmod\\s+-R\\s+777\\s+\\/(?:\\s|$)', platform: 'unix' },
  { id: 'fork_bomb', pattern: ':\\(\\)\\s*\\{\\s*:\\|:&\\s*\\};:', platform: 'unix' },
  // "format <盘符>:"（含 format.com）才算格式化磁盘；不要误伤 PowerShell
  // 的 Format-List / Format-Table 输出格式化 cmdlet。
  { id: 'win_format_cmd', pattern: '\\bformat(\\.com)?\\s+[a-z]:', platform: 'windows' },
  { id: 'win_del_force', pattern: '\\bdel\\s+\\/f\\s+\\/s\\s+\\/q\\s+[a-z]:\\\\', platform: 'windows' },
  { id: 'win_rd_force', pattern: '\\brd\\s+\\/s\\s+\\/q\\s+[a-z]:\\\\', platform: 'windows' },
];

interface ResolvedShell {
  shell: string;
  args: string[];
  type: ShellType;
}

function toWslPath(input: string): string {
  const normalized = input.replace(/\\/g, '/');
  const winDriveMatch = normalized.match(/^([A-Za-z]):\/(.*)$/);
  if (!winDriveMatch) return normalized;
  const drive = winDriveMatch[1].toLowerCase();
  const rest = winDriveMatch[2];
  return `/mnt/${drive}/${rest}`;
}

/** 结构化编辑算法（默认走 sidecar 的 Python 真源；测试可注入 stub）。 */
type ApplyEditsFn = (
  content: string,
  edits: EditOp[],
  filePath?: string,
) => Promise<ApplyEditsResult>;

export class LocalExecutor {
  private readonly maxOutputBytes: number;
  private readonly defaultShell: ResolvedShell;
  private readonly applyEditsFn: ApplyEditsFn;
  private wslAvailable = false;
  private shellCacheInitialized = false;
  private dangerousPatterns: RegExp[] | null = null;
  /** 同文件读-改-写的串行队列（key = resolvedPath）。 */
  private readonly fileQueues = new Map<string, Promise<void>>();
  /**
   * P2b read-before-write 证据：resolvedPath → 读取时的内容版本
   * （CC `seed_read_state` 对齐）。read 成功时记录；write/edit 缺省
   * expectedVersion 时用它做自动 CAS；写成功后回写新版本。会话恢复时
   * 由 sidecar 的 `read_state.seed` 反向调用重灌（seedReadState）。
   */
  private readonly readFileState = new Map<string, string>();
  /**
   * 部分视图门（框架 partial_reads / CC isPartialView 对齐）：本会话只读到
   * 部分内容的分页/裁剪读取把路径登记在这里；writeLocalFile 拒绝整体覆写
   * 未见全文的文件。完整读取或本会话自己的写/编辑成功后销记。会话恢复不
   * 重灌此集合（与框架一致：resume 只重建 version 证据，门退化为 CAS）。
   */
  private readonly partialReadPaths = new Set<string>();

  constructor(maxOutputBytes = DEFAULT_MAX_OUTPUT_BYTES, applyEditsFn: ApplyEditsFn = applyEdits) {
    this.maxOutputBytes = maxOutputBytes;
    this.applyEditsFn = applyEditsFn;
    this.defaultShell = this.computeDefaultShell();
  }

  updateSafetyConfig(config: CommandSafetyConfigPayload): void {
    const disabled = new Set(config.disabledPatternIds);
    const isWin = process.platform === 'win32';
    const patterns: RegExp[] = [];
    for (const def of BUILTIN_DANGEROUS) {
      if (disabled.has(def.id)) continue;
      const matchesPlatform =
        def.platform === 'all' || (isWin && def.platform === 'windows') || (!isWin && def.platform === 'unix');
      if (!matchesPlatform) continue;
      const flags = def.platform === 'windows' ? 'i' : undefined;
      patterns.push(new RegExp(def.pattern, flags));
    }
    for (const cp of config.customPatterns) {
      if (!cp.enabled) continue;
      try {
        patterns.push(new RegExp(cp.pattern));
      } catch {
        log.warn(`[local-executor] invalid custom pattern: ${cp.pattern}`);
      }
    }
    this.dangerousPatterns = patterns;
  }

  /**
   * P2b：会话恢复时重灌 read-before-write 证据。sidecar 扫描持久记录里的
   * 文件工具结果，把 path → version 经 `read_state.seed` 推到这里——进程
   * 重启后自动 CAS 仍然成立（CC `seed_read_state` 语义）。键是 sidecar 侧
   * 已解析的绝对路径，原样存储；resolvePath 对绝对路径是恒等，查询能命中。
   * 返回实际收录的条目数（畸形条目跳过）。
   */
  seedReadState(state: Record<string, unknown>): number {
    let seeded = 0;
    for (const [p, version] of Object.entries(state)) {
      if (typeof p === 'string' && p.length > 0 && typeof version === 'string') {
        this.readFileState.set(p, version);
        seeded += 1;
      }
    }
    return seeded;
  }

  async init(): Promise<void> {
    if (this.shellCacheInitialized) return;
    if (process.platform === 'win32') {
      this.wslAvailable = await this.detectWslAvailable();
      // On Windows, the default shell should always be powershell.exe
      // to allow native Windows commands and paths by default.
      // WSL is still selectable explicitly by passing shell: 'wsl'.
      this.defaultShell.shell = 'powershell.exe';
      this.defaultShell.args = ['-Command'];
      this.defaultShell.type = 'powershell';
    }
    this.shellCacheInitialized = true;
  }

  getPlatformInfo(): { platform: string; shell: ShellType; wslAvailable: boolean; osVersion: string; osArch: string } {
    return {
      platform: process.platform,
      shell: this.defaultShell.type,
      wslAvailable: this.wslAvailable,
      osVersion: os.version(),
      osArch: os.arch(),
    };
  }

  async executeShell(request: LocalExecRequest): Promise<LocalExecResult> {
    try {
      await this.init();
      let command = request.command?.trim();
      if (!command) {
        return { success: false, error: 'command is required' };
      }
      command = rewriteExeCommandIfNeeded(command);
      const dangerousReason = this.detectDangerousCommand(command);
      if (dangerousReason) {
        return { success: false, error: `Blocked dangerous command: ${dangerousReason}` };
      }

      const resolvedShell = this.resolveShell(request.shell);
      if (resolvedShell.type === 'powershell') {
        // LLM 经常混写 cmd / bash 方言（&&、||、%VAR%）；Windows PowerShell 5.1
        // 不支持这些语法，执行前做确定性转换，避免整条命令直接报解析错误。
        const adapted = adaptCommandForPowerShell(command);
        if (adapted !== command) {
          log.info('[local-executor] adapted command for PowerShell', {
            before: command.slice(0, 200),
            after: adapted.slice(0, 200),
          });
          command = adapted;
        }
        // powershell.exe -Command 只回 0/1，会丢掉原生命令的真实退出码
        // （如 python sys.exit(3) 变成 1）。追加 exit $LASTEXITCODE 透传；
        // $LASTEXITCODE 未设置（纯 cmdlet 命令）时等价 exit 0，行为不变。
        command = `${command}\nexit $LASTEXITCODE`;
      }
      // GUI 命令：显式 timeout 仍然尊重；没给的话只等一小段（GUI_LAUNCH_WAIT_MS）
      // ——反正到点会按"已启动"成功返回，等太久只会拖慢 agent 的下一步。
      const guiCommand = isGuiLaunchCommand(command);
      let rawTimeout =
        request.timeout ?? (guiCommand ? GUI_LAUNCH_WAIT_MS : getDefaultExecTimeoutMs());
      if (rawTimeout > 0 && rawTimeout < 1000) {
        rawTimeout *= 1000;
      }
      const timeout = Math.max(1000, rawTimeout);
      const cwd = this.resolveCwd(request.cwd, resolvedShell.type);
      const env = { ...process.env, ...(request.env || {}) };

      const stdoutChunks: Buffer[] = [];
      const stderrChunks: Buffer[] = [];
      let stdoutBytes = 0;
      let stderrBytes = 0;
      let truncated = false;
      const args = [...resolvedShell.args, command];

      const child = spawn(resolvedShell.shell, args, {
        cwd,
        env,
        windowsHide: true,
      });

      child.stdout.on('data', (chunk: Buffer) => {
        if (stdoutBytes >= this.maxOutputBytes) {
          truncated = true;
          return;
        }
        const remain = this.maxOutputBytes - stdoutBytes;
        const safeChunk = chunk.length > remain ? chunk.subarray(0, remain) : chunk;
        stdoutChunks.push(safeChunk);
        stdoutBytes += safeChunk.length;
        if (safeChunk.length < chunk.length) truncated = true;
      });

      child.stderr.on('data', (chunk: Buffer) => {
        if (stderrBytes >= this.maxOutputBytes) {
          truncated = true;
          return;
        }
        const remain = this.maxOutputBytes - stderrBytes;
        const safeChunk = chunk.length > remain ? chunk.subarray(0, remain) : chunk;
        stderrChunks.push(safeChunk);
        stderrBytes += safeChunk.length;
        if (safeChunk.length < chunk.length) truncated = true;
      });

      const result = await new Promise<LocalExecResult>(resolve => {
        const timer = setTimeout(() => {
          if (guiCommand) {
            // GUI/交互式命令（命令行带 "gui" token）：进程留着继续跑，把
            // "已启动、等用户操作"作为成功结果返回。杀掉或判失败都会诱导
            // LLM 重发启动命令，把 Qt 图件重复拉起多次。
            resolve({
              success: true,
              timedOut: true,
              stillRunning: true,
              stdout:
                Buffer.concat(stdoutChunks).toString('utf-8') +
                `\n[GUI 程序已启动，仍在运行（等待 ${timeout}ms 后未退出，进程未被终止）。` +
                `请在打开的界面中继续操作；不要重新执行同一条启动命令。]`,
              stderr: Buffer.concat(stderrChunks).toString('utf-8'),
              truncated,
              shell: resolvedShell.type,
              platform: process.platform,
            });
            return;
          }
          child.kill();
          resolve({
            success: false,
            timedOut: true,
            stdout: Buffer.concat(stdoutChunks).toString('utf-8'),
            stderr: Buffer.concat(stderrChunks).toString('utf-8'),
            // 超时 ≠ 命令出错。GUI/长任务（如启动 Qt 图件）超时后其窗口进程
            // 往往仍在运行——直接原样重跑启动命令会把程序重复启动多次。
            error:
              `Command timed out after ${timeout}ms (shell terminated; a launched GUI app may still be running). ` +
              `DO NOT blindly re-run the same launch command — if this launched a GUI/long-running program, ` +
              `first check whether it is already running, or re-run with a larger \`timeout\`, ` +
              `or start it detached (e.g. PowerShell Start-Process) so it does not block.`,
            truncated,
            shell: resolvedShell.type,
            platform: process.platform,
          });
        }, timeout);

        child.on('error', err => {
          clearTimeout(timer);
          resolve({
            success: false,
            stdout: Buffer.concat(stdoutChunks).toString('utf-8'),
            stderr: Buffer.concat(stderrChunks).toString('utf-8'),
            error: err.message,
            truncated,
            shell: resolvedShell.type,
            platform: process.platform,
          });
        });

        child.on('close', exitCode => {
          clearTimeout(timer);
          resolve({
            success: exitCode === 0,
            stdout: Buffer.concat(stdoutChunks).toString('utf-8'),
            stderr: Buffer.concat(stderrChunks).toString('utf-8'),
            exitCode: exitCode ?? -1,
            truncated,
            shell: resolvedShell.type,
            platform: process.platform,
          });
        });
      });
      return result;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return { success: false, error: message, platform: process.platform };
    }
  }

  async readLocalFile(
    request: LocalFileReadRequest,
    projectRoot?: string | null,
    additionalReadRoots?: string[] | null,
  ): Promise<LocalFileReadResult> {
    try {
      const filePath = this.resolvePath(request.path);
      if (projectRoot) {
        const root = this.resolvePath(projectRoot);
        const inAdditionalRoot = (additionalReadRoots ?? []).some((r) =>
          isPathWithinRoot(filePath, this.resolvePath(r)),
        );
        if (!isPathWithinRoot(filePath, root) && !inAdditionalRoot) {
          return { success: false, error: buildProjectRootViolation(filePath, root) };
        }
      }
      const maxSize = request.maxSize ?? DEFAULT_MAX_READ_SIZE;
      const encoding = request.encoding ?? 'utf-8';
      const fileStat = await stat(filePath);
      if (fileStat.size > maxSize) {
        return { success: false, error: `File too large (${fileStat.size} bytes), max=${maxSize}` };
      }
      const content = await readFile(filePath, { encoding });
      // version 基于完整内容：部分视图的读证据 CAS 仍然有效（框架对齐）。
      const version = hashContent(content);
      // P2b: 记录读证据，后续 write/edit 缺省 expectedVersion 时自动 CAS。
      this.readFileState.set(filePath, version);
      const view = slicePartialView(content, request.offset, request.limit);
      // 部分视图登记/销记：整体覆写门（writeLocalFile）据此拒绝未见全文的覆写。
      if (view.partial) {
        this.partialReadPaths.add(filePath);
      } else {
        this.partialReadPaths.delete(filePath);
      }
      return { success: true, content: view.content, version, partial: view.partial };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return { success: false, error: message };
    }
  }

  async writeLocalFile(
    request: LocalFileWriteRequest,
    projectRoot?: string | null,
  ): Promise<LocalFileWriteResult> {
    const filePath = this.resolvePath(request.path);
    if (projectRoot) {
      const root = this.resolvePath(projectRoot);
      if (!isPathWithinRoot(filePath, root)) {
        return { success: false, error: buildProjectRootViolation(filePath, root) };
      }
    }
    // 同文件串行化：读-改-写必须排队，否则并发写会互相覆盖（pi file-mutation-queue）。
    return this.serializeFileOp(filePath, async () => {
      try {
        const encoding = request.encoding ?? 'utf-8';
        // 部分视图门：本会话对该路径只读到分页/裁剪后的内容，整体覆写会销毁
        // 未见内容（框架 partial_reads / CC isPartialView 对齐）。定点修改走
        // local_edit_file；确需整体重写时先用 offset/limit 分段读完。
        if (this.partialReadPaths.has(filePath)) {
          return {
            success: false,
            error:
              `拒绝整体覆写：${filePath} 本会话只读到部分内容（分页/裁剪），` +
              '覆写会销毁未见内容。请改用 local_edit_file 做定点修改；' +
              '确需整体重写时先用 offset/limit 分段读完。',
          };
        }
        // P2b 自动 CAS：调用方没给 expectedVersion 时回落到本会话的读证据
        // （含 read_state.seed 重灌的）。隐式期望只对仍存在的文件生效——
        // 读后被删除的文件重新写入是新建，没有可覆盖的他人改动。
        const implicit = request.expectedVersion === undefined;
        const expected = request.expectedVersion ?? this.readFileState.get(filePath);
        if (expected !== undefined) {
          const conflict = await this.checkVersion(filePath, expected, encoding, {
            missingOk: implicit,
          });
          if (conflict) return { success: false, error: conflict };
        } else if (requireReadBeforeWrite() && (await fileExists(filePath))) {
          return {
            success: false,
            error:
              `未读先写已拒绝：${filePath} 不在本会话的已读记录里` +
              '（STEERABLE_REQUIRE_READ_BEFORE_WRITE 已启用）。请先 local_read_file 再写入。',
          };
        }
        if (request.createDirs) {
          await mkdir(path.dirname(filePath), { recursive: true });
        }
        await this.atomicWrite(filePath, request.content, encoding);
        const version = hashContent(request.content);
        // 写成功后回写读证据：本会话自己的连续写不互相冲突。
        this.readFileState.set(filePath, version);
        // 模型亲自写了全文，部分视图门不再适用（框架对齐）。
        this.partialReadPaths.delete(filePath);
        return { success: true, version };
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return { success: false, error: message };
      }
    });
  }

  /**
   * local_edit_file 的实现（W6-1）：结构化批量编辑。先按 expectedVersion 做
   * read-before-write 冲突检测，再在原文上做三级模糊定位 + 逆序替换，最后
   * 原子落盘。返回新版本令牌与统一 diff。
   */
  async editLocalFile(
    request: LocalFileEditRequest,
    projectRoot?: string | null,
  ): Promise<LocalFileEditResult> {
    const filePath = this.resolvePath(request.path);
    if (projectRoot) {
      const root = this.resolvePath(projectRoot);
      if (!isPathWithinRoot(filePath, root)) {
        return { success: false, error: buildProjectRootViolation(filePath, root) };
      }
    }
    return this.serializeFileOp(filePath, async () => {
      try {
        const encoding = request.encoding ?? 'utf-8';
        const current = await readFile(filePath, { encoding });
        // P2b 自动 CAS（同 writeLocalFile）：缺省 expectedVersion 时用本会话
        // 读证据。edit 的目标必然已存在（上面 readFile 失败即返回），无需
        // missingOk；硬门对 edit 等价于「读过才可改」。
        const expected = request.expectedVersion ?? this.readFileState.get(filePath);
        if (expected !== undefined) {
          const conflict = await this.checkVersion(filePath, expected, encoding);
          if (conflict) return { success: false, error: conflict };
        } else if (requireReadBeforeWrite()) {
          return {
            success: false,
            error:
              `未读先改已拒绝：${filePath} 不在本会话的已读记录里` +
              '（STEERABLE_REQUIRE_READ_BEFORE_WRITE 已启用）。请先 local_read_file 再编辑。',
          };
        }
        const result = await this.applyEditsFn(current, request.edits, path.basename(filePath));
        await this.atomicWrite(filePath, result.content, encoding);
        const version = hashContent(result.content);
        this.readFileState.set(filePath, version);
        // 注意：编辑不销记 partialReadPaths——定点编辑不代表模型见过全文，
        // 后续盲写仍会销毁未见内容（框架「Cleared by a full read or a write」对齐）。
        return {
          success: true,
          version,
          diff: result.diff,
          applied: result.matches.length,
          matches: result.matches.map(m => ({
            level: m.level,
            startLine: m.startLine,
            oldLineCount: m.oldLineCount,
          })),
        };
      } catch (error) {
        if (error instanceof EditError) {
          return { success: false, error: error.message };
        }
        const message = error instanceof Error ? error.message : String(error);
        return { success: false, error: message };
      }
    });
  }

  /**
   * read-before-write 冲突检测：当前内容版本与调用方期望不一致时返回错误
   * 消息（引导重新读取），一致返回 null。文件不存在且期望存在也算冲突，
   * 除非 `missingOk`（隐式期望来自读证据：读后被删的文件再写是新建，
   * 没有可覆盖的他人改动）。
   */
  private async checkVersion(
    filePath: string,
    expectedVersion: string,
    encoding: BufferEncoding,
    options?: { missingOk?: boolean },
  ): Promise<string | null> {
    let current: string;
    try {
      current = await readFile(filePath, { encoding });
    } catch {
      if (options?.missingOk) return null;
      return `无法冲突检测：读取 ${filePath} 失败（文件可能不存在）。若确认要新建，请去掉 expectedVersion。`;
    }
    const currentVersion = hashContent(current);
    if (currentVersion !== expectedVersion) {
      return (
        `冲突：${filePath} 在你读取后已被修改（版本令牌不匹配）。` +
        `为避免覆盖他人/其它操作的改动，本次写入已拒绝——请重新 local_read_file 拿到最新内容与 version，再基于它重新编辑。`
      );
    }
    return null;
  }

  /** 原子写：先写同目录临时文件再 rename（同卷 rename 原子），失败清理临时文件。 */
  private async atomicWrite(filePath: string, content: string, encoding: BufferEncoding): Promise<void> {
    const tmp = `${filePath}.tmp-${process.pid}-${Math.random().toString(36).slice(2, 8)}`;
    try {
      await writeFile(tmp, content, { encoding });
      await rename(tmp, filePath);
    } catch (error) {
      try {
        await writeFile(tmp, '', { encoding }).catch(() => undefined);
      } catch {
        /* 忽略清理失败 */
      }
      throw error;
    }
  }

  /**
   * 同文件操作串行队列：把同一 resolvedPath 的读-改-写串成链，避免并发
   * 写互相覆盖。不同文件互不影响。
   */
  private serializeFileOp<T>(filePath: string, fn: () => Promise<T>): Promise<T> {
    const prev = this.fileQueues.get(filePath) ?? Promise.resolve();
    const next = prev.then(fn, fn);
    this.fileQueues.set(
      filePath,
      next.then(
        () => undefined,
        () => undefined,
      ),
    );
    return next;
  }

  /**
   * local_run_snippet 的实现：把代码片段写到 scratch 目录（os.tmpdir()/
   * agent-shell-code），用本机解释器跑起来；可选先装 pip/npm 依赖。
   *
   * 设计要点：
   * - 解释器探测结果缓存（python3 → python → py；node）；缺失时返回可操作错误。
   * - pip 优先 `--user`；被 PEP 668（externally-managed，Homebrew Python 常见）
   *   拒绝时在 scratch 目录建一次性 venv，之后所有 python 运行都用 venv 解释器。
   * - npm 依赖装进 scratchDir，node 脚本也落盘在 scratchDir——node 的
   *   node_modules 解析会沿脚本所在目录向上找到依赖，无需 NODE_PATH。
   * - cwd 的项目沙箱在 ToolRouter 层完成，这里只负责跑。
   */
  async runCode(request: LocalRunCodeRequest): Promise<LocalRunCodeResult> {
    try {
      await this.init();
      const language = request.language?.trim().toLowerCase();
      const code = request.code ?? '';
      if (!code.trim()) {
        return { success: false, error: 'code is required' };
      }
      if (language !== 'python' && language !== 'node') {
        return {
          success: false,
          error: `不支持的语言: ${request.language}（目前支持 python / node；其他语言请用 local_exec_shell 自行调用）`,
        };
      }

      let interpreter: string | null;
      if (language === 'python') {
        interpreter = this.pythonVenvPath ?? (await this.resolveInterpreter('python'));
        if (!interpreter) {
          return {
            success: false,
            language,
            error:
              '本机未找到 Python 解释器（已探测 python3 / python / py）。' +
              '请先帮用户安装 Python 3（如 `brew install python3` / python.org 安装包），或改用 language: "node"。',
          };
        }
      } else {
        interpreter = await this.resolveInterpreter('node');
        if (!interpreter) {
          return {
            success: false,
            language,
            error:
              '本机未找到 Node.js（node 命令不可用）。' +
              '请先帮用户安装 Node.js（如 `brew install node` / nodejs.org 安装包），或改用 language: "python"。',
          };
        }
      }

      const scratchDir = this.getCodeScratchDir();
      await mkdir(scratchDir, { recursive: true });

      const installed: string[] = [];
      if (language === 'python' && request.pipPackages?.length) {
        const bad = request.pipPackages.find((p) => !PIP_PACKAGE_RE.test(p));
        if (bad) {
          return { success: false, language, error: `非法 pip 包名: ${bad}` };
        }
        const install = await this.installPythonPackages(
          request.pipPackages,
          scratchDir,
          interpreter,
        );
        if (!install.success) {
          return { success: false, language, interpreter, error: install.error };
        }
        interpreter = install.python ?? interpreter;
        installed.push(...request.pipPackages);
      }
      if (language === 'node' && request.npmPackages?.length) {
        const bad = request.npmPackages.find((p) => !NPM_PACKAGE_RE.test(p));
        if (bad) {
          return { success: false, language, error: `非法 npm 包名: ${bad}` };
        }
        const pkgs = request.npmPackages.map((p) => `"${p}"`).join(' ');
        const install = await this.executeShell({
          command: `npm install --silent --no-audit --no-fund --prefix "${scratchDir}" ${pkgs}`,
          timeout: 180_000,
        });
        if (!install.success) {
          return {
            success: false,
            language,
            interpreter,
            error:
              `npm 依赖安装失败：${(install.stderr || install.error || '').slice(0, 1500)}` +
              '。可考虑换用 python 实现，或把失败原因如实告诉用户。',
          };
        }
        installed.push(...request.npmPackages);
      }

      const ext = language === 'python' ? 'py' : 'js';
      const scriptPath = path.join(
        scratchDir,
        `snippet-${Date.now()}-${Math.random().toString(36).slice(2, 6)}.${ext}`,
      );
      await writeFile(scriptPath, code, 'utf-8');

      // Windows 默认 PowerShell：带引号的命令名需要 `&` 调用运算符，否则被当成字符串。
      const callOperator = process.platform === 'win32' ? '& ' : '';
      const result = await this.executeShell({
        command: `${callOperator}"${interpreter}" "${scriptPath}"`,
        cwd: request.cwd,
        timeout: request.timeout,
      });
      return {
        ...result,
        language,
        interpreter,
        scriptPath,
        ...(installed.length > 0 ? { installedPackages: installed } : {}),
      };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return { success: false, error: message, platform: process.platform };
    }
  }

  private getCodeScratchDir(): string {
    return path.join(os.tmpdir(), 'agent-shell-code');
  }

  /** 解释器探测缓存：undefined=未探测，null=探测过但没有。 */
  private pythonCommand: string | null | undefined = undefined;
  private nodeCommand: string | null | undefined = undefined;
  /** PEP 668 兜底 venv 的解释器路径；一旦建立，后续 python 运行都用它。 */
  private pythonVenvPath: string | null = null;

  private async resolveInterpreter(kind: 'python' | 'node'): Promise<string | null> {
    if (kind === 'python' && this.pythonCommand !== undefined) return this.pythonCommand;
    if (kind === 'node' && this.nodeCommand !== undefined) return this.nodeCommand;
    const candidates =
      kind === 'python'
        ? process.platform === 'win32'
          ? ['python', 'py', 'python3']
          : ['python3', 'python']
        : ['node'];
    for (const cmd of candidates) {
      if (await this.probeCommand(cmd, ['--version'])) {
        if (kind === 'python') this.pythonCommand = cmd;
        else this.nodeCommand = cmd;
        return cmd;
      }
    }
    if (kind === 'python') this.pythonCommand = null;
    else this.nodeCommand = null;
    return null;
  }

  private probeCommand(command: string, args: string[]): Promise<boolean> {
    return new Promise((resolve) => {
      let child;
      try {
        child = spawn(command, args, { windowsHide: true, stdio: 'ignore' });
      } catch {
        resolve(false);
        return;
      }
      child.once('error', () => resolve(false));
      child.once('close', (code: number | null) => resolve(code === 0));
    });
  }

  /**
   * pip 安装：`--user` 优先；遇到 PEP 668（externally-managed-environment，
   * Homebrew/系统 Python 的默认限制）就在 scratch 目录建 venv 兜底，
   * 并把 venv 解释器缓存下来供后续运行使用。
   *
   * 返回的 `python` 字段是后续运行脚本应使用的解释器（可能已被 venv 替换）。
   */
  private async installPythonPackages(
    packages: string[],
    scratchDir: string,
    python: string,
  ): Promise<{ success: boolean; python?: string; error?: string }> {
    const callOperator = process.platform === 'win32' ? '& ' : '';
    const pkgs = packages.map((p) => `"${p}"`).join(' ');
    const inVenv = this.pythonVenvPath !== null;
    const targetPython = this.pythonVenvPath ?? python;
    const userFlag = inVenv ? '' : '--user ';
    const install = await this.executeShell({
      command: `${callOperator}"${targetPython}" -m pip install ${userFlag}--quiet ${pkgs}`,
      timeout: 180_000,
    });
    if (install.success) return { success: true, python: targetPython };

    const installError = (install.stderr || install.error || '').slice(0, 1500);
    if (!inVenv && /externally[- ]managed|PEP\s*668/i.test(installError)) {
      const venvDir = path.join(scratchDir, '.venv');
      const create = await this.executeShell({
        command: `${callOperator}"${python}" -m venv "${venvDir}"`,
        timeout: 120_000,
      });
      if (!create.success) {
        return {
          success: false,
          error:
            `pip --user 被 PEP 668 拒绝，且创建 venv 失败：${(create.stderr || create.error || '').slice(0, 1000)}`,
        };
      }
      const venvPython =
        process.platform === 'win32'
          ? path.join(venvDir, 'Scripts', 'python.exe')
          : path.join(venvDir, 'bin', 'python');
      const retry = await this.executeShell({
        command: `${callOperator}"${venvPython}" -m pip install --quiet ${pkgs}`,
        timeout: 180_000,
      });
      if (!retry.success) {
        return {
          success: false,
          error: `venv 内 pip 安装失败：${(retry.stderr || retry.error || '').slice(0, 1500)}`,
        };
      }
      this.pythonVenvPath = venvPython;
      return { success: true, python: venvPython };
    }
    return {
      success: false,
      error:
        `pip 依赖安装失败：${installError}` +
        '。可检查包名/网络，或把失败原因如实告诉用户。',
    };
  }

  async openLocalTarget(request: LocalOpenRequest): Promise<LocalOpenResult> {
    try {
      const target = decode(request.target).trim();
      if (!target) return { success: false, error: 'target is required' };

      const normalizeAppleMapsUrl = (input: string): string | null => {
        if (!/^(maps:\/\/maps\.apple\.com|https?:\/\/maps\.apple\.com)/i.test(input)) return null;
        return input.replace(/^maps:\/\//i, 'https://');
      };

      const urlLikeTarget = /^[a-z][a-z0-9+\-.]*:\/\//i.test(target);
      if (urlLikeTarget) {
        const safeTarget = normalizeAppleMapsUrl(target) || target;
        await shellOpenExternal(safeTarget);
      } else {
        const localPath = this.resolvePath(target);
        const openError = await shellOpenPath(localPath);
        if (openError) return { success: false, error: openError };
      }
      return { success: true };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return { success: false, error: message };
    }
  }

  /**
   * Public wrapper so other execution paths (e.g. the visible-terminal path
   * in main.ts) can run the same dangerous-command check that headless
   * `executeShell` applies, instead of silently bypassing it.
   */
  detectDangerousCommand(command: string): string | null {
    return this.detectDangerousCommandInternal(command);
  }

  private detectDangerousCommandInternal(command: string): string | null {
    const patterns = this.dangerousPatterns ?? this.getDefaultPatterns();
    for (const pattern of patterns) {
      if (pattern.test(command)) return pattern.toString();
    }
    return null;
  }

  private getDefaultPatterns(): RegExp[] {
    const isWin = process.platform === 'win32';
    return BUILTIN_DANGEROUS.filter(
      d => d.platform === 'all' || (isWin && d.platform === 'windows') || (!isWin && d.platform === 'unix')
    ).map(d => {
      const flags = d.platform === 'windows' ? 'i' : undefined;
      return new RegExp(d.pattern, flags);
    });
  }

  private resolvePath(inputPath: string): string {
    const expanded = inputPath.startsWith('~') ? path.join(os.homedir(), inputPath.slice(1)) : inputPath;
    return path.resolve(expanded);
  }

  private resolveCwd(cwd: string | undefined, shellType: ShellType): string {
    const resolved = this.resolvePath(cwd || os.homedir());
    if (process.platform === 'win32' && shellType === 'wsl') {
      return toWslPath(resolved);
    }
    return resolved;
  }

  private resolveShell(shellType?: ShellType): ResolvedShell {
    if (!shellType) return this.defaultShell;
    if (shellType === this.defaultShell.type) return this.defaultShell;
    if (shellType === 'zsh') return { shell: '/bin/zsh', args: ['-l', '-c'], type: 'zsh' };
    if (shellType === 'bash') return { shell: '/bin/bash', args: ['-l', '-c'], type: 'bash' };
    if (shellType === 'wsl') return { shell: 'wsl.exe', args: ['bash', '-c'], type: 'wsl' };
    if (shellType === 'cmd') return { shell: 'cmd.exe', args: ['/c'], type: 'cmd' };
    return { shell: 'powershell.exe', args: ['-Command'], type: 'powershell' };
  }

  private computeDefaultShell(): ResolvedShell {
    if (process.platform === 'darwin') return { shell: '/bin/zsh', args: ['-l', '-c'], type: 'zsh' };
    if (process.platform === 'linux') return { shell: '/bin/bash', args: ['-l', '-c'], type: 'bash' };
    if (process.platform === 'win32') return { shell: 'powershell.exe', args: ['-Command'], type: 'powershell' };
    return { shell: '/bin/sh', args: ['-c'], type: 'bash' };
  }

  private async detectWslAvailable(): Promise<boolean> {
    try {
      await access('C:\\Windows\\System32\\wsl.exe', fsConstants.X_OK);
      const result = await new Promise<boolean>(resolve => {
        const child = spawn('wsl.exe', ['--status'], { windowsHide: true });
        child.once('error', () => resolve(false));
        child.once('close', code => resolve(code === 0));
      });
      return result;
    } catch {
      return false;
    }
  }
}
