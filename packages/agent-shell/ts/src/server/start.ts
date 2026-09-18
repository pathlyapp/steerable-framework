/**
 * Composable BS host assembly for executable and embedded deployments.
 */
import { existsSync } from 'node:fs';
import type { Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import dotenv from 'dotenv';
import log from 'electron-log';

import { getBrand } from '../brand.js';
import { registerPackHttpRoutes } from '../host/http-routes.js';
import { createHostRuntime } from '../host/runtime.js';
import { getUserDataDir } from '../runtime.js';
import type { TenantScope } from '../storage/driver.js';
import {
  createBsServer,
  type BsMiddleware,
} from './http-server.js';
import { SseBus } from './sse-bus.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Match the Electron entry's dotenv precedence: local file first, existing
// process variables always win.
for (const file of ['.env.local', '.env']) {
  const fullPath = path.join(path.resolve(__dirname, '..', '..'), file);
  if (existsSync(fullPath)) dotenv.config({ path: fullPath, override: false });
}

log.transports.file.resolvePathFn = () =>
  path.join(getUserDataDir(), 'logs', 'main.log');

export interface BsHostOptions {
  host?: string;
  port?: number;
  authToken?: string;
  middleware?: readonly BsMiddleware[];
  /** Fixed storage owner for a per-user host process. */
  scope?: TenantScope;
}

export interface BsHostHandle {
  readonly server: Server;
  readonly host: string;
  readonly port: number;
  /** Stops accepting requests and shuts down all host runtime services. */
  shutdown(): Promise<void>;
}

/**
 * Assembles and starts the BS HTTP host.
 *
 * Product composition roots may register providers, package contributions,
 * and middleware before calling this function.
 */
export async function startBsHost(
  options: BsHostOptions = {},
): Promise<BsHostHandle> {
  const brand = getBrand();
  const bus = new SseBus();
  const broadcast = (channel: string, payload: unknown) =>
    bus.broadcast(channel, payload);
  const runtime = await createHostRuntime({
    scope: options.scope,
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

  const webDistDir =
    process.env.DEEPPATH_WEB_DIST ??
    path.join(__dirname, '..', '..', 'web', 'dist');
  if (!existsSync(path.join(webDistDir, 'index.html'))) {
    await runtime.shutdown();
    throw new Error(
      `[bs] web build not found at ${webDistDir} — run \`pnpm build\` first.`,
    );
  }

  for (const [packId, handle] of packHandles) {
    const routes = handle.httpRoutes?.();
    if (routes) registerPackHttpRoutes(packId, routes);
  }

  const server = createBsServer({
    store: runtime.store,
    localBackendRouter,
    localExecutor,
    localScriptRegistry,
    terminalManager,
    approvalBridge,
    askUserBridge,
    maybeExecInTerminal,
    bus,
    webDistDir,
    authToken: options.authToken,
    middleware: options.middleware,
  });
  const host = options.host ?? (process.env.DEEPPATH_BS_HOST || '127.0.0.1');
  const requestedPort =
    options.port ?? Number(process.env.DEEPPATH_BS_PORT || 4787);

  try {
    await new Promise<void>((resolve, reject) => {
      const onError = (error: Error) => reject(error);
      server.once('error', onError);
      server.listen(requestedPort, host, () => {
        server.off('error', onError);
        resolve();
      });
    });
  } catch (error) {
    await runtime.shutdown();
    throw error;
  }

  const address = server.address() as AddressInfo;
  const port = address.port;
  console.log(
    `[bs] ${brand.displayName} server listening at http://${host}:${port}  (flavor=${brand.flavor})`,
  );
  await runtime.start();

  let shutdownPromise: Promise<void> | null = null;
  return {
    server,
    host,
    port,
    shutdown(): Promise<void> {
      shutdownPromise ??= (async () => {
        try {
          await new Promise<void>((resolve, reject) => {
            server.close((error) => {
              if (error) reject(error);
              else resolve();
            });
          });
        } finally {
          await runtime.shutdown();
        }
      })();
      return shutdownPromise;
    },
  };
}

export type { BsMiddleware };
