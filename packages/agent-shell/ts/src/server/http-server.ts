/**
 * BS 模式 HTTP server：把 CS（Electron）模式下挂在 IPC 后面的同一组服务
 * 暴露成真正的 HTTP 接口，并托管 apps/web/dist 静态产物。
 *
 * 路由分三层：
 *   - `/api/v2/*`        → LocalBackendRouter（chat / 设置 / 技能 / MCP …），
 *                          与 Electron 下 `local-backend:request` 同一入口；
 *                          流式路径（chats/:id/run|send|agent、…/regenerate）
 *                          走 handleStream，响应即标准 SSE。
 *   - `GET /api/v2/events` → SseBus 事件总线（替代 webContents.send 广播）。
 *   - `/host/*`          → preload 里 direct-IPC 能力的 HTTP 等价物
 *                          （terminal / local / approval 为宿主自带；
 *                          场景包路由经 host/http-routes 注册表匹配）。
 *
 * 安全约定：默认绑定 127.0.0.1。仅绑回环挡不住 DNS rebinding（恶意网站
 * 重绑定后受害者浏览器即同源驱动本服务），所以有两道门：
 *   1. Host 白名单——所有请求（含静态）的 Host 必须是 127.0.0.1 /
 *      localhost / [::1]，浏览器无法伪造 Host，rebinding 在此被拒；
 *   2. Bearer token——每次启动随机生成，经 index.html 注入
 *      `window.__DEEPPATH_BS__.token`；`/api/v2/*` 与 `/host/*` 全部要求
 *      `Authorization: Bearer <token>`（EventSource 不能设头，SSE 走
 *      `?token=` query）。静态资源不验 token（浏览器要靠它拿到 token）。
 * 不要绑到非回环地址，那会把 shell / 文件读写 / 场景包工具控制暴露给整个
 * 网络（token 只能挡没有 token 的人）。
 */
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { randomBytes } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import log from 'electron-log';
import type { LocalBackendRouter } from '../local-backend/router.js';
import { getActiveCoreLoopStreamId } from '../local-backend/coreloop-stream.js';
import { getSidecarSupervisor } from '../llm/index.js';
import { localStore } from '../storage/index.js';
import type { LocalExecutor, LocalExecRequest, LocalExecResult, LocalFileReadRequest, LocalFileWriteRequest, LocalOpenRequest, CommandSafetyConfigPayload } from '../local-executor.js';
import type { LocalScriptRegistry, CreateLocalScriptInput } from '../local-script-registry.js';
import type { TerminalManager, TerminalSpawnOptions } from '../terminal-manager.js';
import { matchPackHttpRoute } from '../host/http-routes.js';
import type { createApprovalBridge } from '../sidecar/reverse-approval.js';
import type { createAskUserBridge } from '../sidecar/reverse-ask-user.js';
import { getBrand } from '../brand.js';
import { saveAttachmentFiles } from '../attachments.js';
import type { SseBus } from './sse-bus.js';

/** 与 router.handleStream 内部的路由正则保持一致——只有这些路径是流式。 */
const STREAM_PATH_PATTERNS = [
  /^\/api\/v2\/chats\/[^/]+\/(send|run|agent)$/,
  /^\/api\/v2\/chats\/[^/]+\/messages\/[^/]+\/regenerate$/,
];

const MAX_BODY_BYTES = 25 * 1024 * 1024;

const MIME: Record<string, string> = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.webp': 'image/webp',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.txt': 'text/plain; charset=utf-8',
};

export interface BsServerDeps {
  localBackendRouter: LocalBackendRouter;
  localExecutor: LocalExecutor;
  localScriptRegistry: LocalScriptRegistry;
  terminalManager: TerminalManager;
  approvalBridge: ReturnType<typeof createApprovalBridge>;
  askUserBridge: ReturnType<typeof createAskUserBridge>;
  maybeExecInTerminal: (req: LocalExecRequest) => Promise<LocalExecResult | null>;
  bus: SseBus;
  /** apps/web/dist 的绝对路径。 */
  webDistDir: string;
  /** Bearer token；缺省每次启动随机生成。测试注入固定值。 */
  authToken?: string;
}

/** Host 白名单：仅回环（可带端口）。浏览器禁止伪造 Host，rebinding 到此为止。 */
const LOOPBACK_HOST = /^(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/;

function sendJson(res: ServerResponse, status: number, data: unknown): void {
  const body = JSON.stringify(data ?? null);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

async function readJsonBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > MAX_BODY_BYTES) throw new Error('request body too large');
    chunks.push(chunk as Buffer);
  }
  if (chunks.length === 0) return undefined;
  const text = Buffer.concat(chunks).toString('utf8').trim();
  if (!text) return undefined;
  return JSON.parse(text);
}

function asRecord(body: unknown): Record<string, unknown> {
  return body && typeof body === 'object' ? (body as Record<string, unknown>) : {};
}

export function createBsServer(deps: BsServerDeps): Server {
  const {
    localBackendRouter,
    localExecutor,
    localScriptRegistry,
    terminalManager,
    approvalBridge,
    askUserBridge,
    maybeExecInTerminal,
    bus,
    webDistDir,
  } = deps;
  const authToken = deps.authToken ?? randomBytes(24).toString('base64url');

  /** `/api/v2/*` 与 `/host/*` 的 Bearer 门；EventSource 不能设头，收 query token。 */
  function isAuthorized(req: IncomingMessage, url: URL): boolean {
    if (req.headers.authorization === `Bearer ${authToken}`) return true;
    return url.searchParams.get('token') === authToken;
  }

  async function handleApi(req: IncomingMessage, res: ServerResponse, pathname: string): Promise<void> {
    const isStream = req.method === 'POST' && STREAM_PATH_PATTERNS.some((p) => p.test(pathname));
    const body = req.method === 'POST' || req.method === 'PATCH' || req.method === 'PUT'
      ? await readJsonBody(req)
      : undefined;
    const search = new URL(req.url ?? '/', 'http://bs.local').search;
    const request = { method: req.method ?? 'GET', path: `${pathname}${search}`, body };

    if (isStream) {
      const controller = new AbortController();
      // 浏览器关页/取消 fetch → 中止 agent 循环（对齐 Electron 的 cancelStream）。
      req.on('close', () => controller.abort());
      res.writeHead(200, {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache, no-transform',
        Connection: 'keep-alive',
        'X-Accel-Buffering': 'no',
      });
      try {
        const result = await localBackendRouter.handleStream(
          request,
          (chunk) => res.write(chunk),
          { signal: controller.signal },
        );
        log.info(`[bs] stream ${request.method} ${pathname} -> ${result.status}`);
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        log.error(`[bs] stream ${request.method} ${pathname} threw`, err);
        try {
          res.write(`event: error\ndata: ${JSON.stringify({ message })}\n\n`);
        } catch { /* client gone */ }
      } finally {
        res.end();
      }
      return;
    }

    try {
      const response = await localBackendRouter.handle(request);
      log.info(`[bs] ${request.method} ${pathname} -> ${response.status}`);
      sendJson(res, response.status, response.data);
    } catch (err) {
      log.error(`[bs] ${request.method} ${pathname} threw`, err);
      sendJson(res, 500, { detail: err instanceof Error ? err.message : String(err) });
    }
  }

  /** `/host/*`：preload direct-IPC 的 HTTP 等价物。全部 POST JSON，除标记 GET 的。 */
  async function handleHost(req: IncomingMessage, res: ServerResponse, pathname: string): Promise<void> {
    const body = asRecord(await readJsonBody(req));
    const method = req.method ?? 'GET';

    if (method === 'GET' && pathname === '/host/info') {
      const brand = getBrand();
      sendJson(res, 200, {
        runtime: 'bs',
        platform: process.platform,
        flavor: brand.flavor,
        brandName: brand.displayName,
      });
      return;
    }

    // ─── terminal（对齐 main.ts 的 terminal:* IPC） ───
    if (method === 'GET' && pathname === '/host/terminal/list') {
      sendJson(res, 200, terminalManager.list());
      return;
    }
    if (method === 'POST' && pathname === '/host/terminal/spawn') {
      sendJson(res, 200, terminalManager.spawn(body as TerminalSpawnOptions));
      return;
    }
    if (method === 'POST' && pathname === '/host/terminal/ensure') {
      const session = terminalManager.ensurePrimary(body as TerminalSpawnOptions);
      // 对齐 Electron ensure：附带 attach 前的 replay buffer，前端补写一次。
      const replay = terminalManager.getReplayBuffer(session.id);
      sendJson(res, 200, { session, replay: replay ?? '' });
      return;
    }
    if (method === 'POST' && pathname === '/host/terminal/write') {
      sendJson(res, 200, terminalManager.write(String(body.id ?? ''), String(body.data ?? '')));
      return;
    }
    if (method === 'POST' && pathname === '/host/terminal/resize') {
      sendJson(res, 200, terminalManager.resize(String(body.id ?? ''), Number(body.cols ?? 80), Number(body.rows ?? 24)));
      return;
    }
    if (method === 'POST' && pathname === '/host/terminal/kill') {
      sendJson(res, 200, terminalManager.kill(String(body.id ?? '')));
      return;
    }
    if (method === 'POST' && pathname === '/host/terminal/exec') {
      let id = typeof body.id === 'string' ? body.id : undefined;
      if (!id) id = terminalManager.ensurePrimary().id;
      try {
        const result = await terminalManager.exec(id, String(body.command ?? ''), body.timeoutMs as number | undefined);
        sendJson(res, 200, result);
      } catch (err) {
        sendJson(res, 200, {
          success: false,
          exitCode: -1,
          stdout: '',
          stderr: err instanceof Error ? err.message : String(err),
          truncated: false,
          durationMs: 0,
        });
      }
      return;
    }

    // ─── 轮中转向（对齐 local-backend:steer IPC；不走 router） ───
    if (method === 'POST' && pathname === '/host/steer') {
      const chatId = typeof body.chatId === 'string' ? body.chatId : '';
      const content = typeof body.content === 'string' ? body.content : '';
      if (!chatId || !content.trim()) {
        sendJson(res, 200, { ok: false, reason: 'invalid_params' });
        return;
      }
      const streamId = getActiveCoreLoopStreamId(chatId);
      const supervisor = getSidecarSupervisor();
      if (!streamId || !supervisor) {
        sendJson(res, 200, { ok: false, reason: 'no_active_coreloop_turn' });
        return;
      }
      const ok = await supervisor.steerChat(streamId, content);
      // 与 main.ts 同理：接受即落库，重开对话能看到这条注入的用户消息。
      if (ok) localStore.addMessage(chatId, 'user', content);
      sendJson(res, 200, { ok, ...(ok ? {} : { reason: 'stream_not_active' }) });
      return;
    }

    // ─── approval（对齐 approval:decide IPC） ───
    if (method === 'POST' && pathname === '/host/approval/decide') {
      sendJson(res, 200, await approvalBridge.decide(body));
      return;
    }
    if (method === 'GET' && pathname === '/host/approval/pending') {
      sendJson(res, 200, approvalBridge.pending());
      return;
    }

    // ─── ask_user（对齐 ask-user:answer IPC） ───
    if (method === 'POST' && pathname === '/host/ask-user/answer') {
      sendJson(res, 200, askUserBridge.answer(body));
      return;
    }
    if (method === 'GET' && pathname === '/host/ask-user/pending') {
      sendJson(res, 200, askUserBridge.pending());
      return;
    }

    // ─── local（对齐 local:* IPC） ───
    if (method === 'POST' && pathname === '/host/local/exec-shell') {
      const request = body as unknown as LocalExecRequest;
      const viaTerminal = await maybeExecInTerminal(request);
      sendJson(res, 200, viaTerminal ?? (await localExecutor.executeShell(request)));
      return;
    }
    if (method === 'POST' && pathname === '/host/local/read-file') {
      sendJson(res, 200, await localExecutor.readLocalFile(body as unknown as LocalFileReadRequest));
      return;
    }
    if (method === 'POST' && pathname === '/host/local/write-file') {
      sendJson(res, 200, await localExecutor.writeLocalFile(body as unknown as LocalFileWriteRequest));
      return;
    }
    if (method === 'POST' && pathname === '/host/local/open-path') {
      sendJson(res, 200, await localExecutor.openLocalTarget(body as unknown as LocalOpenRequest));
      return;
    }
    if (method === 'GET' && pathname === '/host/local/scripts') {
      sendJson(res, 200, localScriptRegistry.list());
      return;
    }
    if (method === 'POST' && pathname === '/host/local/scripts/add') {
      sendJson(res, 200, localScriptRegistry.create(body as unknown as CreateLocalScriptInput));
      return;
    }
    if (method === 'POST' && pathname === '/host/local/scripts/update') {
      sendJson(res, 200, localScriptRegistry.update(String(body.id ?? ''), (body.updates ?? {}) as Partial<CreateLocalScriptInput>));
      return;
    }
    if (method === 'POST' && pathname === '/host/local/scripts/delete') {
      localScriptRegistry.delete(String(body.id ?? ''));
      sendJson(res, 200, { success: true });
      return;
    }
    if (method === 'POST' && pathname === '/host/local/scripts/run') {
      const script = localScriptRegistry.getById(String(body.id ?? ''));
      if (!script) {
        sendJson(res, 200, { success: false, error: `Script not found: ${String(body.id ?? '')}` });
        return;
      }
      sendJson(res, 200, await localExecutor.executeShell({
        command: script.command,
        cwd: script.cwd,
        timeout: script.timeout,
      }));
      return;
    }
    if (method === 'POST' && pathname === '/host/local/update-safety-config') {
      localExecutor.updateSafetyConfig(body as unknown as CommandSafetyConfigPayload);
      sendJson(res, 200, { success: true });
      return;
    }

    // ─── 场景包路由（0.4：/host/<pack>/* 由包经 host/http-routes 注册表
    // 贡献；在宿主自带路由之后、404 之前统一匹配，包不能遮蔽宿主路由）。
    // host 端点目前不用 query string，query 先传空表。 ───
    const packMatch = matchPackHttpRoute(method, pathname);
    if (packMatch) {
      sendJson(res, 200, await packMatch.route.handler({
        params: packMatch.params,
        query: {},
        body,
      }));
      return;
    }

    // ─── 会话附件（对齐 attachments:save IPC；BS 用 base64 字节） ───
    if (method === 'POST' && pathname === '/host/attachments/save') {
      const chatId = typeof body.chatId === 'string' ? body.chatId : '';
      const files = Array.isArray(body.files)
        ? (body.files as Array<{ path?: string; name?: string; data?: string }>).filter(
            (f) => f && (typeof f.path === 'string' || typeof f.data === 'string'),
          )
        : [];
      sendJson(res, 200, await saveAttachmentFiles(chatId, files));
      return;
    }

    sendJson(res, 404, { detail: `Unknown host endpoint: ${method} ${pathname}` });
  }

  /** 静态托管 + index.html 注入 BS 标记（前端据此切换 http-bridge）。 */
  async function serveStatic(req: IncomingMessage, res: ServerResponse, pathname: string): Promise<void> {
    const rel = pathname === '/' ? '/index.html' : pathname;
    const filePath = path.join(webDistDir, path.normalize(rel).replace(/^([/\\])+/, ''));
    if (!filePath.startsWith(webDistDir)) {
      sendJson(res, 403, { detail: 'forbidden' });
      return;
    }
    const target = existsSync(filePath) && !filePath.endsWith(path.sep) ? filePath : path.join(webDistDir, 'index.html');
    try {
      let content: Buffer | string = await readFile(target);
      const ext = path.extname(target).toLowerCase();
      if (ext === '.html') {
        // 注入 BS 引导信息：platform 是 preload 里的同步字段，http-bridge
        // 需要同步可见；brand 让 <title>/favicon 在 bridge 就绪前就正确。
        const brand = getBrand();
        const bootstrap = `<script>window.__DEEPPATH_BS__=${JSON.stringify({
          platform: process.platform,
          flavor: brand.flavor,
          brandName: brand.displayName,
          token: authToken,
        })};</script>`;
        content = content.toString('utf8').replace('<head>', `<head>${bootstrap}`);
      }
      res.writeHead(200, {
        'Content-Type': MIME[ext] ?? 'application/octet-stream',
        'Cache-Control': ext === '.html' ? 'no-cache' : 'public, max-age=3600',
      });
      res.end(content);
    } catch (err) {
      log.warn('[bs] static read failed', { target, err });
      sendJson(res, 404, { detail: 'not found' });
    }
  }

  return createServer((req, res) => {
    void (async () => {
      // 门 1：Host 白名单（含静态——rebound 浏览器连 index.html 里的
      // token 都不许拿到）。
      if (!LOOPBACK_HOST.test(req.headers.host ?? '')) {
        sendJson(res, 403, { detail: 'forbidden host' });
        return;
      }
      const url = new URL(req.url ?? '/', 'http://bs.local');
      const pathname = url.pathname;
      // 门 2：API 与宿主能力要 Bearer token；静态资源不验（浏览器靠它
      // 拿 token），静态面不暴露任何能力。
      const needsAuth = pathname.startsWith('/api/v2/') || pathname.startsWith('/host/');
      if (needsAuth && !isAuthorized(req, url)) {
        sendJson(res, 401, { detail: 'unauthorized' });
        return;
      }
      if (pathname === '/api/v2/events' && req.method === 'GET') {
        bus.attach(res);
        return;
      }
      if (pathname.startsWith('/api/v2/')) {
        await handleApi(req, res, pathname);
        return;
      }
      if (pathname.startsWith('/host/')) {
        await handleHost(req, res, pathname);
        return;
      }
      if (req.method === 'GET' || req.method === 'HEAD') {
        await serveStatic(req, res, pathname);
        return;
      }
      sendJson(res, 404, { detail: 'not found' });
    })().catch((err) => {
      log.error('[bs] unhandled request error', err);
      if (!res.headersSent) sendJson(res, 500, { detail: 'internal error' });
      else res.end();
    });
  });
}
