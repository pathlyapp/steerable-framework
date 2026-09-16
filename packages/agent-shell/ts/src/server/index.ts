/**
 * BS 模式入口：`node dist/server/index.js`。
 *
 * 装配与 Electron 主进程（main.ts）相同的一组服务——同一份装配住在
 * src/host/runtime.ts（HostRuntime），这里只保留 BS 特有的部分：SSE
 * 总线广播、HTTP 出口（http-server.ts）、SIGINT/SIGTERM 生命周期。
 * 数据目录、sidecar、工具链、审批流全部共用；两种模式因此操作同一份
 * SQLite / JSON 配置（不要同时启动两个宿主——write-lease 会拒绝第二个写者）。
 *
 * 默认绑定 127.0.0.1:4787（DEEPPATH_BS_HOST / DEEPPATH_BS_PORT 可改）。
 * 无认证：与桌面版同一信任模型，不要暴露到非回环地址。
 */
// 产品组装根必须是第一个 import：包迁移注册要先于 storage 单例构造（0.3b）。
// 产品组装根（products/<id>/server.ts 或 devtools/dev-server.ts）必须先于
// 本模块完成 import：包迁移注册要先于 storage 单例构造（0.3b / 2.3）。
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import dotenv from 'dotenv';
import log from 'electron-log';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// 与 main.ts 相同的 .env 加载顺序（.env.local 优先；已有环境变量胜出）。
(function loadDotenvFiles(): void {
  const repoRoot = path.resolve(__dirname, '..', '..');
  for (const file of ['.env.local', '.env']) {
    const fullPath = path.join(repoRoot, file);
    if (existsSync(fullPath)) dotenv.config({ path: fullPath, override: false });
  }
})();

import { getBrand } from '../brand.js';
import { getUserDataDir } from '../runtime.js';
import { createHostRuntime } from '../host/runtime.js';
import { registerPackHttpRoutes } from '../host/http-routes.js';
import { SseBus } from './sse-bus.js';
import { createBsServer } from './http-server.js';

// BS 模式没有 Electron 的 userData 概念，electron-log 默认会写
// ~/.config/<name>/logs；在 HOME 只读（容器/只读环境）或用户显式指定
// DEEPPATH_USER_DATA_DIR 时，日志应跟随应用数据目录走。
log.transports.file.resolvePathFn = () => path.join(getUserDataDir(), 'logs', 'main.log');

async function main(): Promise<void> {
  const brand = getBrand();
  const bus = new SseBus();
  const broadcast = (channel: string, payload: unknown) => bus.broadcast(channel, payload);

  // 与 CS 同一份服务装配；BS 的差异只有广播（SSE 总线）与"有浏览器连着
  // 事件总线 = 有人能应答审批/提问"的 hasWindow 判定。
  const runtime = createHostRuntime({
    broadcast,
    hasWindow: () => bus.size > 0,
    onLog: (line) => log.info('[sidecar]', line),
    taskSweepReason: '服务重启，任务流已中断',
  });
  const {
    localExecutor,
    localScriptRegistry,
    terminalManager,
    packHandles,
    localBackendRouter,
    approvalBridge,
    askUserBridge,
    maybeExecInTerminal,
  } = runtime;

  // 2.3 起 web 产物按产品构建：产品入口注入 DEEPPATH_WEB_DIST
  //（products/<id>/web/dist）；缺省回退到 shell 包自带的 web/dist
  //（packages/agent-shell/web/dist），仅用于给出可读的报错。
  const webDistDir =
    process.env.DEEPPATH_WEB_DIST ??
    path.join(__dirname, '..', '..', 'web', 'dist');
  if (!existsSync(path.join(webDistDir, 'index.html'))) {
    console.error(`[bs] web build not found at ${webDistDir} — run \`pnpm build\` first.`);
    process.exit(1);
  }

  // 场景包 HTTP 路由（/host/<packId>/*）由装配产物给出，宿主循环注册
  //（2.3；路由路径命名空间由 registerPackHttpRoutes 校验）。
  for (const [packId, handle] of packHandles) {
    const routes = handle.httpRoutes?.();
    if (routes) registerPackHttpRoutes(packId, routes);
  }

  const server = createBsServer({
    localBackendRouter,
    localExecutor,
    localScriptRegistry,
    terminalManager,
    approvalBridge,
    askUserBridge,
    maybeExecInTerminal,
    bus,
    webDistDir,
  });

  const host = process.env.DEEPPATH_BS_HOST || '127.0.0.1';
  const port = Number(process.env.DEEPPATH_BS_PORT || 4787);
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, host, () => resolve());
  });
  console.log(`[bs] ${brand.displayName} server listening at http://${host}:${port}  (flavor=${brand.flavor})`);

  // 任务清扫 / mock 广播 / sidecar 启动 / MCP 刷新 / 终端预热。不阻塞
  // HTTP 就绪；竞速的回合由 router 回退处理（同 main.ts）。
  runtime.start();

  let shuttingDown = false;
  const shutdown = () => {
    if (shuttingDown) return;
    shuttingDown = true;
    console.log('[bs] shutting down…');
    void runtime.shutdown().finally(() => process.exit(0));
    // 兜底：sidecar 卡住也不拖住退出。
    setTimeout(() => process.exit(0), 5_000).unref();
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((err) => {
  console.error('[bs] failed to start:', err);
  process.exit(1);
});
