/**
 * 宿主运行时抽象：Electron 主进程与 BS 独立 Node server 共用的环境探测。
 *
 * CS 模式（Electron）下路径来自 `app.getPath('userData')`（按 productName
 * 分目录）；BS 模式（`node dist/server/index.js`）没有 Electron，落到
 * `DEEPPATH_USER_DATA_DIR` 或产品注入的 `~/<dataDirName>`（3.1）。两种模式共用
 * 同一份 SQLite / JSON 存储代码，差异只在这一层。
 *
 * 本模块只允许依赖 node 内置模块（与 brand.ts 同约束）：storage 会被
 * vitest 直接 import，不能碰 electron 包。
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { getProductConfig } from './product-config.js';

/** Electron 主进程里 `process.versions.electron` 有值；普通 Node（BS server、vitest）没有。 */
export function isElectronRuntime(): boolean {
  return Boolean((process.versions as Record<string, string | undefined>).electron);
}

interface ElectronAppLike {
  getPath(name: 'userData'): string;
  getAppPath(): string;
  on?(event: 'will-quit', listener: () => void): void;
  removeListener?(event: 'will-quit', listener: () => void): void;
}

let cachedApp: ElectronAppLike | null | undefined;

/**
 * 同步拿 Electron app。普通 Node 下 `require('electron')` 返回二进制路径
 * 字符串（没有 app），必须拒绝；Electron 主进程里才是真正的 API 对象。
 */
function tryElectronApp(): ElectronAppLike | null {
  if (cachedApp !== undefined) return cachedApp;
  if (!isElectronRuntime()) {
    cachedApp = null;
    return null;
  }
  try {
    const require = createRequire(import.meta.url);
    const mod = require('electron') as { app?: ElectronAppLike };
    cachedApp = mod && typeof mod.app?.getPath === 'function' ? mod.app : null;
  } catch {
    cachedApp = null;
  }
  return cachedApp;
}

/** 应用数据目录（SQLite、JSON store、用户技能目录的根）。 */
export function getUserDataDir(): string {
  if (process.env.DEEPPATH_USER_DATA_DIR) return process.env.DEEPPATH_USER_DATA_DIR;
  const app = tryElectronApp();
  if (app) return app.getPath('userData');
  // BS：目录名是产品注入配置（3.1，product.json dataDirName），与 Electron
  // 下 productName 分目录的共存语义对齐；中性 shell 缺省 .agent-shell。
  const dirName = getProductConfig().dataDirName ?? '.agent-shell';
  return path.join(os.homedir(), dirName);
}

/**
 * 应用根目录（消费产品的仓库/打包根：含 products/manifest.json、assets、
 * scripts 的那层）。3.2 起 shell 是被消费的框架包，本模块自己的位置
 * （packages/agent-shell/ts/...）不再是应用根——产品组装根在入口最早
 *  import 时经 setAppRootDir 注入；未注入时（单测）回退到 cwd 探测。
 */
let appRootOverride: string | null = null;

/**
 * 注入应用根目录（产品组装根调用）。重复注入不同值抛错（组装期笔误，
 * fail fast——与 setProductBrand 同语义）。
 */
export function setAppRootDir(dir: string): void {
  if (appRootOverride && appRootOverride !== dir) {
    throw new Error('[runtime] app root already set');
  }
  appRootOverride = dir;
}

export function getAppRootDir(): string {
  const app = tryElectronApp();
  if (app) return app.getAppPath();
  if (appRootOverride) return appRootOverride;
  // 单测/脚本平面：从 cwd 向上找含 package.json 的目录。
  for (let dir = process.cwd(); ; ) {
    if (fs.existsSync(path.join(dir, 'package.json'))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return process.cwd();
    dir = parent;
  }
}

/**
 * preload  bundle 路径（3.4 修复）：shell main.ts 创建窗口时需要
 * preload.cjs——shell 自带默认面（dist/preload.cjs，buildPreloadApi()
 * 无包贡献），但带 invoke 型包贡献的产品（如 ciflog）由产品构建把
 * 组合后的 preload.cjs 出在产品 main.js 旁，产品入口在动态 import
 * shell main 之前经本函数注入。未注入时回退 shell 自带默认。
 */
let preloadPathOverride: string | null = null;

/**
 * 注入 preload bundle 绝对路径（产品组装根/入口调用）。重复注入不同值
 * 抛错（组装期笔误，fail fast——与 setAppRootDir 同语义）。
 */
export function setPreloadPath(file: string): void {
  if (preloadPathOverride && preloadPathOverride !== file) {
    throw new Error('[runtime] preload path already set');
  }
  preloadPathOverride = file;
}

/**
 * 解析窗口应加载的 preload bundle：产品注入优先，其次 shell 自带默认
 * （shellDir 一般为 shell main.js 所在目录）。
 */
export function getPreloadPath(shellDir: string): string {
  return preloadPathOverride ?? path.join(shellDir, 'preload.cjs');
}

/**
 * 产品 web 产物目录（2.3）：产品入口（products/<id>/{main,server}.ts）
 * 用本函数把 DEEPPATH_WEB_DIST 注入环境，shell 的 main/server 据此定位
 * web dist。双平面探测：源码平面入口在 products/<id>/（web dist 在同级
 * web/dist）；编译平面入口在 products/<id>/dist/products/<id>/（web dist
 * 在仓库根的 products/<id>/web/dist）。两处都没有返回 null（打包后的
 * CS 走 app.getAppPath()/web-dist 约定，不经本函数）。
 */
export function resolveProductWebDist(entryUrl: string): string | null {
  const here = path.dirname(fileURLToPath(entryUrl));
  const productId = path.basename(here);
  for (const candidate of [
    path.join(here, 'web', 'dist'),
    path.resolve(here, '..', '..', '..', '..', 'products', productId, 'web', 'dist'),
  ]) {
    if (fs.existsSync(path.join(candidate, 'index.html'))) return candidate;
  }
  return null;
}

/** 用系统默认应用打开本地路径（Electron `shell.openPath` 的跨宿主等价物）。 */
export async function shellOpenPath(target: string): Promise<string> {
  if (tryElectronApp()) {
    const { shell } = (await import('electron')) as typeof import('electron');
    return shell.openPath(target);
  }
  const [cmd, args] = openCommand(target, false);
  return spawnDetached(cmd, args);
}

/** 用系统浏览器打开 URL（Electron `shell.openExternal` 的跨宿主等价物）。 */
export async function shellOpenExternal(url: string): Promise<void> {
  if (tryElectronApp()) {
    const { shell } = (await import('electron')) as typeof import('electron');
    await shell.openExternal(url);
    return;
  }
  const [cmd, args] = openCommand(url, true);
  const error = await spawnDetached(cmd, args);
  if (error) throw new Error(error);
}

function openCommand(target: string, isUrl: boolean): [string, string[]] {
  if (process.platform === 'darwin') return ['open', [target]];
  if (process.platform === 'win32') {
    return isUrl
      ? ['rundll32', ['url.dll,FileProtocolHandler', target]]
      : ['explorer', [target]];
  }
  return ['xdg-open', [target]];
}

/** 返回空串表示成功（对齐 shell.openPath 的约定），否则是错误消息。 */
function spawnDetached(cmd: string, args: string[]): Promise<string> {
  return new Promise((resolve) => {
    try {
      const child = spawn(cmd, args, { detached: true, stdio: 'ignore' });
      child.on('error', (err) => resolve(err.message));
      child.on('spawn', () => {
        child.unref();
        resolve('');
      });
    } catch (err) {
      resolve(err instanceof Error ? err.message : String(err));
    }
  });
}

/**
 * 注册应用退出钩子（sidecar 的兜底清理）。BS 下没有 app 生命周期——
 * 宿主退出由 server 的 SIGINT/SIGTERM 钩子统一负责，这里 no-op。
 */
export function onAppWillQuit(listener: () => void): void {
  try {
    tryElectronApp()?.on?.('will-quit', listener);
  } catch {
    /* 非 Electron 宿主 */
  }
}

export function offAppWillQuit(listener: () => void): void {
  try {
    tryElectronApp()?.removeListener?.('will-quit', listener);
  } catch {
    /* 非 Electron 宿主 */
  }
}

interface NativeImageInstance {
  isEmpty(): boolean;
  getSize(): { width: number; height: number };
  resize(o: { width?: number; height?: number; quality?: string }): NativeImageInstance;
  toPNG(): Buffer;
  toJPEG(quality: number): Buffer;
}

interface NativeImageLike {
  createFromPath(p: string): NativeImageInstance;
}

/**
 * Electron `nativeImage`（图片解码/缩放）。BS / 单测等无 Electron 环境返回
 * null——调用方按"不支持图片解码"降级（image-attachment 已有该分支）。
 */
export function getNativeImage(): NativeImageLike | null {
  if (!isElectronRuntime()) return null;
  try {
    const require = createRequire(import.meta.url);
    const mod = require('electron') as { nativeImage?: NativeImageLike };
    return typeof mod.nativeImage?.createFromPath === 'function' ? mod.nativeImage : null;
  } catch {
    return null;
  }
}
