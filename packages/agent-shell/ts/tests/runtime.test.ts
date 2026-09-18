/**
 * 宿主运行时抽象（runtime.ts）在非 Electron 环境（vitest = BS/Node 平面）
 * 的行为测试：
 *  - isElectronRuntime / getNativeImage / will-quit 钩子的降级；
 *  - getUserDataDir 的 env 覆盖与产品注入目录名回退；
 *  - setAppRootDir / setPreloadPath 的注入语义（重复注入不同值 fail fast）；
 *  - resolveProductWebDist 的源码平面 / 编译平面双探测。
 *
 * shellOpenPath/shellOpenExternal 会真实 spawn 系统 open——不在单测里碰。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  getAppRootDir,
  getNativeImage,
  getPreloadPath,
  getUserDataDir,
  isElectronRuntime,
  offAppWillQuit,
  onAppWillQuit,
  resolveProductWebDist,
  setAppRootDir,
  setPreloadPath,
} from '../src/runtime.js';

const ENV_KEYS = ['DEEPPATH_USER_DATA_DIR'];
let savedEnv: Record<string, string | undefined> = {};

beforeEach(() => {
  savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
});

afterEach(() => {
  for (const k of ENV_KEYS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
});

describe('非 Electron 平面降级', () => {
  it('isElectronRuntime=false，getNativeImage=null，will-quit 钩子安全 no-op', () => {
    expect(isElectronRuntime()).toBe(false);
    expect(getNativeImage()).toBeNull();
    const listener = () => {};
    expect(() => {
      onAppWillQuit(listener);
      offAppWillQuit(listener);
    }).not.toThrow();
  });
});

describe('getUserDataDir', () => {
  it('DEEPPATH_USER_DATA_DIR 环境变量优先', () => {
    process.env.DEEPPATH_USER_DATA_DIR = '/tmp/custom-data-dir';
    expect(getUserDataDir()).toBe('/tmp/custom-data-dir');
  });

  it('无 env 无 Electron：落到 home 下的产品注入目录名（中性缺省 .agent-shell）', () => {
    delete process.env.DEEPPATH_USER_DATA_DIR;
    const dir = getUserDataDir();
    expect(dir.startsWith(os.homedir())).toBe(true);
    expect(path.basename(dir)).toMatch(/^\.?[a-z-]+$/);
  });
});

describe('应用根 / preload 路径注入', () => {
  it('setAppRootDir 注入后 getAppRootDir 用注入值；同值重复注入幂等；不同值抛错', () => {
    const injected = path.join(os.tmpdir(), 'app-root-injected');
    setAppRootDir(injected);
    expect(getAppRootDir()).toBe(injected);
    expect(() => setAppRootDir(injected)).not.toThrow();
    expect(() => setAppRootDir(path.join(os.tmpdir(), 'other-root'))).toThrow(/already set/);
  });

  it('setPreloadPath 注入后优先于 shell 自带默认；不同值重复注入抛错', () => {
    const injected = path.join(os.tmpdir(), 'product-preload.cjs');
    setPreloadPath(injected);
    expect(getPreloadPath('/shell/dir')).toBe(injected);
    expect(() => setPreloadPath(injected)).not.toThrow();
    expect(() => setPreloadPath('/other/preload.cjs')).toThrow(/already set/);
  });
});

describe('resolveProductWebDist · 双平面探测', () => {
  let root: string;

  beforeEach(() => {
    root = mkdtempSync(path.join(os.tmpdir(), 'web-dist-'));
  });

  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  it('源码平面：入口同级 web/dist 有 index.html → 命中', () => {
    const productDir = path.join(root, 'products', 'demo');
    const webDist = path.join(productDir, 'web', 'dist');
    mkdirSync(webDist, { recursive: true });
    writeFileSync(path.join(webDist, 'index.html'), '<html></html>');
    const entry = `file://${path.join(productDir, 'main.ts')}`;
    expect(resolveProductWebDist(entry)).toBe(webDist);
  });

  it('编译平面：入口在 products/<id>/dist/products/<id>/，web dist 在仓库根 products/<id>/web/dist', () => {
    // 布局：<root>/products/demo/web/dist/index.html
    //       <root>/products/demo/dist/products/demo/main.js
    const webDist = path.join(root, 'products', 'demo', 'web', 'dist');
    mkdirSync(webDist, { recursive: true });
    writeFileSync(path.join(webDist, 'index.html'), '<html></html>');
    const entryDir = path.join(root, 'products', 'demo', 'dist', 'products', 'demo');
    mkdirSync(entryDir, { recursive: true });
    const entry = `file://${path.join(entryDir, 'main.js')}`;
    expect(resolveProductWebDist(entry)).toBe(webDist);
  });

  it('编译平面：入口在仓根 dist/products/<id>/，同样命中 products/<id>/web/dist', () => {
    // 布局：<root>/products/demo/web/dist/index.html
    //       <root>/dist/products/demo/main.js
    const webDist = path.join(root, 'products', 'demo', 'web', 'dist');
    mkdirSync(webDist, { recursive: true });
    writeFileSync(path.join(webDist, 'index.html'), '<html></html>');
    const entryDir = path.join(root, 'dist', 'products', 'demo');
    mkdirSync(entryDir, { recursive: true });
    const entry = `file://${path.join(entryDir, 'main.js')}`;
    expect(resolveProductWebDist(entry)).toBe(webDist);
  });

  it('两处都没有 index.html → null（打包 CS 不走这里）', () => {
    const productDir = path.join(root, 'products', 'empty');
    mkdirSync(productDir, { recursive: true });
    expect(resolveProductWebDist(`file://${path.join(productDir, 'main.ts')}`)).toBeNull();
  });
});
