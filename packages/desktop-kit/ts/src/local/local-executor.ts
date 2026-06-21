import { shell } from 'electron';
import { access, mkdir, readFile, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import log from 'electron-log';
import { constants as fsConstants } from 'node:fs';
import he from 'he';
const { decode } = he;

import type {
  LocalExecRequest,
  LocalExecResult,
  LocalFileReadRequest,
  LocalFileReadResult,
  LocalFileWriteRequest,
  LocalFileWriteResult,
  LocalOpenRequest,
  LocalOpenResult,
  CommandSafetyConfigPayload,
} from '../bridge/types.js';

export type ShellType = 'zsh' | 'bash' | 'wsl' | 'powershell' | 'cmd';

const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_MAX_OUTPUT_BYTES = 100 * 1024;
const DEFAULT_MAX_READ_SIZE = 1024 * 1024;

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
  { id: 'win_format_cmd', pattern: '\\bformat\\b', platform: 'windows' },
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

export class LocalExecutor {
  private readonly maxOutputBytes: number;
  private readonly defaultShell: ResolvedShell;
  private wslAvailable = false;
  private shellCacheInitialized = false;
  private dangerousPatterns: RegExp[] | null = null;

  constructor(maxOutputBytes = DEFAULT_MAX_OUTPUT_BYTES) {
    this.maxOutputBytes = maxOutputBytes;
    this.defaultShell = this.computeDefaultShell();
  }

  updateSafetyConfig(config: CommandSafetyConfigPayload): void {
    // Adapter to match original array values or safety model config
    const disabled = new Set(config.blockedCommands || []);
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
    for (const cp of config.allowedPatterns || []) {
      try {
        patterns.push(new RegExp(cp));
      } catch {
        log.warn(`[local-executor] invalid custom pattern: ${cp}`);
      }
    }
    this.dangerousPatterns = patterns;
  }

  async init(): Promise<void> {
    if (this.shellCacheInitialized) return;
    if (process.platform === 'win32') {
      this.wslAvailable = await this.detectWslAvailable();
      this.defaultShell.shell = this.wslAvailable ? 'wsl.exe' : 'powershell.exe';
      this.defaultShell.args = this.wslAvailable ? ['bash', '-c'] : ['-Command'];
      this.defaultShell.type = this.wslAvailable ? 'wsl' : 'powershell';
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
      const command = request.command?.trim();
      if (!command) {
        return { success: false, error: 'command is required' };
      }
      const dangerousReason = this.detectDangerousCommand(command);
      if (dangerousReason) {
        return { success: false, error: `Blocked dangerous command: ${dangerousReason}` };
      }

      const resolvedShell = this.resolveShell(request.env?.SHELL as ShellType);
      const timeout = Math.max(1000, request.timeoutMs ?? DEFAULT_TIMEOUT_MS);
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
          child.kill();
          resolve({
            success: false,
            stdout: Buffer.concat(stdoutChunks).toString('utf-8'),
            stderr: Buffer.concat(stderrChunks).toString('utf-8'),
            error: `Command timed out after ${timeout}ms`,
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

  async readLocalFile(request: LocalFileReadRequest): Promise<LocalFileReadResult> {
    try {
      const filePath = this.resolvePath(request.path);
      const maxSize = DEFAULT_MAX_READ_SIZE;
      const encoding = 'utf-8';
      const fileStat = await stat(filePath);
      if (fileStat.size > maxSize) {
        return { success: false, error: `File too large (${fileStat.size} bytes), max=${maxSize}` };
      }
      const content = await readFile(filePath, { encoding });
      return { success: true, content };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return { success: false, error: message };
    }
  }

  async writeLocalFile(request: LocalFileWriteRequest): Promise<LocalFileWriteResult> {
    try {
      const filePath = this.resolvePath(request.path);
      const encoding = 'utf-8';
      if (request.createDirs) {
        await mkdir(path.dirname(filePath), { recursive: true });
      }
      await writeFile(filePath, request.content, { encoding });
      return { success: true };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return { success: false, error: message };
    }
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
        await shell.openExternal(safeTarget);
      } else {
        const localPath = this.resolvePath(target);
        const openError = await shell.openPath(localPath);
        if (openError) return { success: false, error: openError };
      }
      return { success: true };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return { success: false, error: message };
    }
  }

  private detectDangerousCommand(command: string): string | null {
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
