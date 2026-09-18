/**
 * Sidecar 持久化库（sessions/traces/history）路径解析。
 *
 * Kept outside boot.ts so history readers can resolve this separate database
 * without initializing host storage.
 */
import path from 'node:path';
import { homedir } from 'node:os';
import { getBrand } from '../brand.js';

/**
 *  1. `STEERABLE_SIDECAR_STORAGE_PATH` 显式覆盖（dev 脚本与测试的逃生门——
 *     with-flavor.mjs 会给开发实例注入 sessions-dev-<flavor>.db）；
 *  2. `DEEPPATH_USER_DATA_DIR`（BS 模式/只读 HOME 场景）优先于 `~/.steerable`：
 *     sidecar 库落到 `<userDataDir>/sidecar/sessions-<flavor>.db`，保证 HOME
 *     不可写时 BS server 仍能启动 sidecar；
 *  3. 按 flavor 隔离默认路径：`<flavor>` 非 generic 时 → sessions-<flavor>.db，
 *     generic → sessions.db（历史默认，保留存量数据）。
 *
 * 写锁是跨进程互斥的（Windows 命名 mutex / POSIX flock），两个宿主共用同
 * 一库时后到者 boot 失败并无限重启。flavor 的 userData 目录本已隔离，
 * sidecar 库同样隔离后两个 flavor 才能真正同机共存。
 */
export function resolveSidecarStoragePath(): string {
  const override = process.env.STEERABLE_SIDECAR_STORAGE_PATH;
  if (override) return override;
  // 按 flavor 派生（开放字符串，0.3g）——不含任何产品分支。
  const flavor = getBrand().flavor;
  const name = flavor === 'generic' ? 'sessions.db' : `sessions-${flavor}.db`;
  const userDataDir = process.env.DEEPPATH_USER_DATA_DIR;
  if (userDataDir) return path.join(userDataDir, 'sidecar', name);
  return path.join(homedir(), '.steerable', name);
}
