/** Executable BS entry; reusable assembly lives in `start.ts`. */
import { startBsHost } from './start.js';

async function main(): Promise<void> {
  const handle = await startBsHost();
  let shuttingDown = false;
  const shutdown = () => {
    if (shuttingDown) return;
    shuttingDown = true;
    console.log('[bs] shutting down…');
    void handle.shutdown().finally(() => process.exit(0));
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
