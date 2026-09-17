/**
 * BS 模式 HTTP server（server/http-server.ts）行为测试。
 *
 * 用真实 HTTP server（127.0.0.1 随机端口）+ fetch 覆盖三层路由：
 * `/api/v2/*`（LocalBackendRouter 代理 + SSE 流式）、`/host/*`（preload
 * direct-IPC 的 HTTP 等价物）、静态托管（index.html 注入 BS 引导标记）。
 * 依赖（router / executor / terminalManager / bridges / bus）全部 stub，
 * 验证的是 HTTP 层的请求解析、路由分发、响应形状与错误路径。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import type { AddressInfo } from 'node:net';
import type { Server } from 'node:http';

// http-server.ts 模块级 import storage 单例——不 mock 的话测试进程会把
// localStore 开到真实 ~/.agent-shell/agent-shell.db。本套件不测存储，
// 把边界 stub 掉（storage 行为由 tests/storage/ 用真 SQLite 覆盖）。
vi.mock('../../src/storage/index.js', () => ({
  localStore: { addMessage: vi.fn() },
}));

import { createBsServer, type BsServerDeps } from '../../src/server/http-server.js';
import { registerPackHttpRoutes, resetPackHttpRoutes } from '../../src/host/http-routes.js';

const mocks = vi.hoisted(() => ({
  routerHandle: vi.fn(),
  routerHandleStream: vi.fn(),
  executeShell: vi.fn(),
  readLocalFile: vi.fn(),
  writeLocalFile: vi.fn(),
  openLocalTarget: vi.fn(),
  updateSafetyConfig: vi.fn(),
  terminalList: vi.fn(),
  terminalSpawn: vi.fn(),
  terminalEnsurePrimary: vi.fn(),
  terminalGetReplayBuffer: vi.fn(),
  terminalWrite: vi.fn(),
  terminalResize: vi.fn(),
  terminalKill: vi.fn(),
  terminalExec: vi.fn(),
  approvalDecide: vi.fn(),
  approvalPending: vi.fn(),
  askUserAnswer: vi.fn(),
  askUserPending: vi.fn(),
  maybeExecInTerminal: vi.fn(),
  busAttach: vi.fn(),
  scriptList: vi.fn(),
  scriptCreate: vi.fn(),
  scriptUpdate: vi.fn(),
  scriptDelete: vi.fn(),
  scriptGetById: vi.fn(),
}));

function makeDeps(webDistDir: string): BsServerDeps {
  return {
    localBackendRouter: {
      handle: mocks.routerHandle,
      handleStream: mocks.routerHandleStream,
    } as unknown as BsServerDeps['localBackendRouter'],
    localExecutor: {
      executeShell: mocks.executeShell,
      readLocalFile: mocks.readLocalFile,
      writeLocalFile: mocks.writeLocalFile,
      openLocalTarget: mocks.openLocalTarget,
      updateSafetyConfig: mocks.updateSafetyConfig,
    } as unknown as BsServerDeps['localExecutor'],
    localScriptRegistry: {
      list: mocks.scriptList,
      create: mocks.scriptCreate,
      update: mocks.scriptUpdate,
      delete: mocks.scriptDelete,
      getById: mocks.scriptGetById,
    } as unknown as BsServerDeps['localScriptRegistry'],
    terminalManager: {
      list: mocks.terminalList,
      spawn: mocks.terminalSpawn,
      ensurePrimary: mocks.terminalEnsurePrimary,
      getReplayBuffer: mocks.terminalGetReplayBuffer,
      write: mocks.terminalWrite,
      resize: mocks.terminalResize,
      kill: mocks.terminalKill,
      exec: mocks.terminalExec,
    } as unknown as BsServerDeps['terminalManager'],
    approvalBridge: {
      decide: mocks.approvalDecide,
      pending: mocks.approvalPending,
    } as unknown as BsServerDeps['approvalBridge'],
    askUserBridge: {
      answer: mocks.askUserAnswer,
      pending: mocks.askUserPending,
    } as unknown as BsServerDeps['askUserBridge'],
    maybeExecInTerminal: mocks.maybeExecInTerminal,
    bus: { attach: mocks.busAttach } as unknown as BsServerDeps['bus'],
    webDistDir,
    authToken: 'test-token',
  };
}

const AUTH = { Authorization: 'Bearer test-token' };

describe('BS HTTP server', () => {
  let server: Server;
  let base: string;
  let webDistDir: string;

  beforeEach(async () => {
    vi.clearAllMocks();
    webDistDir = mkdtempSync(path.join(tmpdir(), 'bs-web-dist-'));
    writeFileSync(path.join(webDistDir, 'index.html'), '<html><head></head><body>app</body></html>');
    writeFileSync(path.join(webDistDir, 'app.js'), 'console.log(1)');
    server = createBsServer(makeDeps(webDistDir));
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });

  afterEach(async () => {
    resetPackHttpRoutes();
    await new Promise<void>((resolve) => server.close(() => resolve()));
    rmSync(webDistDir, { recursive: true, force: true });
  });

  const post = (p: string, body?: unknown) =>
    fetch(`${base}${p}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...AUTH },
      body: body === undefined ? undefined : JSON.stringify(body),
    });

  const get = (p: string) => fetch(`${base}${p}`, { headers: AUTH });

  describe('/api/v2/* 代理到 LocalBackendRouter', () => {
    it('非流式请求：method/path/body 透传，响应状态与 data 来自 router', async () => {
      mocks.routerHandle.mockResolvedValue({ status: 200, data: { ok: 1 } });
      const res = await post('/api/v2/chats', { title: 't' });
      expect(res.status).toBe(200);
      expect(await res.json()).toEqual({ ok: 1 });
      expect(mocks.routerHandle).toHaveBeenCalledWith({
        method: 'POST',
        path: '/api/v2/chats',
        body: { title: 't' },
      });
    });

    it('GET 不带 body；query string 拼进 path 传给 router', async () => {
      mocks.routerHandle.mockResolvedValue({ status: 200, data: [] });
      await get('/api/v2/chats?limit=3');
      expect(mocks.routerHandle).toHaveBeenCalledWith({
        method: 'GET',
        path: '/api/v2/chats?limit=3',
        body: undefined,
      });
    });

    it('router 抛异常 → 500 + { detail }', async () => {
      mocks.routerHandle.mockRejectedValue(new Error('boom'));
      const res = await post('/api/v2/chats', {});
      expect(res.status).toBe(500);
      expect(await res.json()).toEqual({ detail: 'boom' });
    });

    it('非法 JSON body → 500（readJsonBody 抛出，走未捕获兜底）', async () => {
      const res = await fetch(`${base}/api/v2/chats`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...AUTH },
        body: '{not-json',
      });
      expect(res.status).toBe(500);
    });

    it('流式路径（chats/:id/send）：SSE 头 + chunk 透传 + 结束', async () => {
      mocks.routerHandleStream.mockImplementation(
        async (_req: unknown, emit: (chunk: string) => void) => {
          emit('data: {"a":1}\n\n');
          emit('data: {"b":2}\n\n');
          return { status: 200 };
        },
      );
      const res = await post('/api/v2/chats/c1/send', { content: 'hi' });
      expect(res.status).toBe(200);
      expect(res.headers.get('content-type')).toContain('text/event-stream');
      const text = await res.text();
      expect(text).toBe('data: {"a":1}\n\ndata: {"b":2}\n\n');
      expect(mocks.routerHandleStream).toHaveBeenCalledOnce();
    });

    it('流式路径（messages/:id/regenerate）也走 SSE', async () => {
      mocks.routerHandleStream.mockResolvedValue({ status: 200 });
      const res = await post('/api/v2/chats/c1/messages/m1/regenerate', {});
      expect(res.headers.get('content-type')).toContain('text/event-stream');
      expect(mocks.routerHandleStream).toHaveBeenCalledOnce();
    });

    it('流式 handler 抛异常 → 写入 event: error 帧后结束', async () => {
      mocks.routerHandleStream.mockRejectedValue(new Error('stream boom'));
      const res = await post('/api/v2/chats/c1/run', {});
      const text = await res.text();
      expect(text).toContain('event: error');
      expect(text).toContain('stream boom');
    });

    it('非流式 chats 路径（GET）不走 handleStream', async () => {
      mocks.routerHandle.mockResolvedValue({ status: 200, data: {} });
      await get('/api/v2/chats/c1');
      expect(mocks.routerHandleStream).not.toHaveBeenCalled();
    });

    it('GET /api/v2/events → SSE 事件总线 attach', async () => {
      mocks.busAttach.mockImplementation((res: { end: () => void }) => res.end());
      const res = await get('/api/v2/events');
      expect(res.status).toBe(200);
      expect(mocks.busAttach).toHaveBeenCalledOnce();
    });
  });

  describe('/host/* direct-IPC 等价物', () => {
    it('GET /host/info → runtime/platform/brand 形状', async () => {
      const res = await get('/host/info');
      expect(res.status).toBe(200);
      const body = await res.json();
      expect(body.runtime).toBe('bs');
      expect(body.platform).toBe(process.platform);
      expect(typeof body.brandName).toBe('string');
    });

    it('terminal 系列：list/spawn/write/resize/kill 透传参数', async () => {
      mocks.terminalList.mockReturnValue([{ id: 't1' }]);
      expect(await (await get('/host/terminal/list')).json()).toEqual([{ id: 't1' }]);

      mocks.terminalSpawn.mockReturnValue({ id: 't2' });
      await post('/host/terminal/spawn', { cwd: '/tmp' });
      expect(mocks.terminalSpawn).toHaveBeenCalledWith({ cwd: '/tmp' });

      await post('/host/terminal/write', { id: 't2', data: 'ls\n' });
      expect(mocks.terminalWrite).toHaveBeenCalledWith('t2', 'ls\n');

      await post('/host/terminal/resize', { id: 't2', cols: 120, rows: 40 });
      expect(mocks.terminalResize).toHaveBeenCalledWith('t2', 120, 40);

      await post('/host/terminal/kill', { id: 't2' });
      expect(mocks.terminalKill).toHaveBeenCalledWith('t2');
    });

    it('terminal/ensure：附带 replay buffer（对齐 Electron ensure）', async () => {
      mocks.terminalEnsurePrimary.mockReturnValue({ id: 'main' });
      mocks.terminalGetReplayBuffer.mockReturnValue('replay-text');
      const body = await (await post('/host/terminal/ensure', {})).json();
      expect(body).toEqual({ session: { id: 'main' }, replay: 'replay-text' });
    });

    it('terminal/exec：缺 id 时用 ensurePrimary 的主会话；异常 → 200 + success:false', async () => {
      mocks.terminalEnsurePrimary.mockReturnValue({ id: 'main' });
      mocks.terminalExec.mockResolvedValue({ success: true, stdout: 'ok' });
      const ok = await (await post('/host/terminal/exec', { command: 'ls' })).json();
      expect(ok.success).toBe(true);
      expect(mocks.terminalExec).toHaveBeenCalledWith('main', 'ls', undefined);

      mocks.terminalExec.mockRejectedValue(new Error('dead pty'));
      const fail = await (await post('/host/terminal/exec', { id: 't9', command: 'ls' })).json();
      expect(fail).toMatchObject({ success: false, exitCode: -1, stderr: 'dead pty' });
    });

    it('/host/steer：缺 chatId/content → invalid_params', async () => {
      expect(await (await post('/host/steer', { content: 'x' })).json()).toEqual({
        ok: false,
        reason: 'invalid_params',
      });
      expect(await (await post('/host/steer', { chatId: 'c1', content: '  ' })).json()).toEqual({
        ok: false,
        reason: 'invalid_params',
      });
    });

    it('/host/steer：无活跃 coreloop 轮 → no_active_coreloop_turn', async () => {
      // 测试环境没有 sidecar supervisor，也没有活跃 streamId。
      const body = await (await post('/host/steer', { chatId: 'c1', content: '转向' })).json();
      expect(body).toEqual({ ok: false, reason: 'no_active_coreloop_turn' });
    });

    it('approval/decide 与 ask-user 端点透传到 bridge', async () => {
      mocks.approvalDecide.mockResolvedValue({ kind: 'allow_once' });
      expect(await (await post('/host/approval/decide', { id: 'a1' })).json()).toEqual({
        kind: 'allow_once',
      });
      expect(mocks.approvalDecide).toHaveBeenCalledWith({ id: 'a1' });
      const approvalPending = [{ requestId: 'a1', toolName: 'bash' }];
      mocks.approvalPending.mockReturnValue(approvalPending);
      expect(await (await get('/host/approval/pending')).json()).toEqual(approvalPending);

      mocks.askUserAnswer.mockReturnValue({ ok: true });
      await post('/host/ask-user/answer', { id: 'q1', answer: 'y' });
      expect(mocks.askUserAnswer).toHaveBeenCalledWith({ id: 'q1', answer: 'y' });

      const pending = [{ requestId: 'q1', intro: '', questions: [] }];
      mocks.askUserPending.mockReturnValue(pending);
      expect(await (await get('/host/ask-user/pending')).json()).toEqual(pending);
    });

    it('local/exec-shell：优先 maybeExecInTerminal，返回 null 时回落 executor', async () => {
      mocks.maybeExecInTerminal.mockResolvedValue({ success: true, stdout: 'via-terminal' });
      const via = await (await post('/host/local/exec-shell', { command: 'ls' })).json();
      expect(via.stdout).toBe('via-terminal');
      expect(mocks.executeShell).not.toHaveBeenCalled();

      mocks.maybeExecInTerminal.mockResolvedValue(null);
      mocks.executeShell.mockResolvedValue({ success: true, stdout: 'via-executor' });
      const direct = await (await post('/host/local/exec-shell', { command: 'ls' })).json();
      expect(direct.stdout).toBe('via-executor');
    });

    it('local/read-file、write-file、open-path 透传请求体', async () => {
      mocks.readLocalFile.mockResolvedValue({ success: true, content: 'x' });
      await post('/host/local/read-file', { path: '/a', offset: 1, limit: 5 });
      expect(mocks.readLocalFile).toHaveBeenCalledWith({ path: '/a', offset: 1, limit: 5 });

      mocks.writeLocalFile.mockResolvedValue({ success: true, version: 'v' });
      await post('/host/local/write-file', { path: '/a', content: 'c' });
      expect(mocks.writeLocalFile).toHaveBeenCalledWith({ path: '/a', content: 'c' });

      mocks.openLocalTarget.mockResolvedValue({ success: true });
      await post('/host/local/open-path', { target: '/a' });
      expect(mocks.openLocalTarget).toHaveBeenCalledWith({ target: '/a' });
    });

    it('local/scripts：CRUD + run（未找到 → success:false 错误形状）', async () => {
      mocks.scriptList.mockReturnValue([{ id: 's1' }]);
      expect(await (await get('/host/local/scripts')).json()).toEqual([{ id: 's1' }]);

      mocks.scriptCreate.mockReturnValue({ id: 's2' });
      await post('/host/local/scripts/add', { name: 'n', command: 'c' });
      expect(mocks.scriptCreate).toHaveBeenCalledWith({ name: 'n', command: 'c' });

      mocks.scriptUpdate.mockReturnValue({ id: 's2' });
      await post('/host/local/scripts/update', { id: 's2', updates: { name: 'n2' } });
      expect(mocks.scriptUpdate).toHaveBeenCalledWith('s2', { name: 'n2' });

      await post('/host/local/scripts/delete', { id: 's2' });
      expect(mocks.scriptDelete).toHaveBeenCalledWith('s2');

      mocks.scriptGetById.mockReturnValue(undefined);
      const missing = await (await post('/host/local/scripts/run', { id: 'nope' })).json();
      expect(missing.success).toBe(false);
      expect(missing.error).toContain('nope');

      mocks.scriptGetById.mockReturnValue({ id: 's3', command: 'make', cwd: '/r', timeout: 1000 });
      mocks.executeShell.mockResolvedValue({ success: true });
      await post('/host/local/scripts/run', { id: 's3' });
      expect(mocks.executeShell).toHaveBeenCalledWith({ command: 'make', cwd: '/r', timeout: 1000 });
    });

    it('local/update-safety-config 透传并回 success:true', async () => {
      const body = await (await post('/host/local/update-safety-config', { disabledPatternIds: ['x'], customPatterns: [] })).json();
      expect(body).toEqual({ success: true });
      expect(mocks.updateSafetyConfig).toHaveBeenCalledOnce();
    });

    it('场景包路由：注册表命中时调用 handler（宿主路由优先，包不能遮蔽）', async () => {
      const handler = vi.fn().mockResolvedValue({ from: 'pack' });
      registerPackHttpRoutes('demo-pack', [{ method: 'POST', path: '/host/demo-pack/ping', handler }]);
      const body = await (await post('/host/demo-pack/ping', { x: 1 })).json();
      expect(body).toEqual({ from: 'pack' });
      expect(handler).toHaveBeenCalledWith({ params: {}, query: {}, body: { x: 1 } });
    });

    it('未知 /host 端点 → 404 + detail', async () => {
      const res = await post('/host/nope/nothing', {});
      expect(res.status).toBe(404);
      expect((await res.json()).detail).toContain('Unknown host endpoint');
    });
  });

  describe('安全门（Host 白名单 + Bearer token）', () => {
    /** fetch 禁设 Host 头，用 node:http 原生请求模拟 rebinding 来的 Host。 */
    const rawGet = async (p: string, headers: Record<string, string>) => {
      const { request } = await import('node:http');
      const port = (server.address() as AddressInfo).port;
      return new Promise<{ status: number; body: string }>((resolve, reject) => {
        const req = request({ host: '127.0.0.1', port, path: p, method: 'GET', headers }, (res) => {
          const chunks: Buffer[] = [];
          res.on('data', (c) => chunks.push(c));
          res.on('end', () =>
            resolve({ status: res.statusCode ?? 0, body: Buffer.concat(chunks).toString('utf8') }),
          );
        });
        req.on('error', reject);
        req.end();
      });
    };

    it('Host 非回环 → 403（rebinding 拦截，含静态路径）', async () => {
      const api = await rawGet('/api/v2/chats', { Host: 'evil.com' });
      expect(api.status).toBe(403);
      const html = await rawGet('/', { Host: 'evil.com' });
      expect(html.status).toBe(403);
      expect(html.body).not.toContain('test-token');
    });

    it('/api/v2 与 /host 无 token → 401', async () => {
      expect((await fetch(`${base}/api/v2/chats`)).status).toBe(401);
      expect((await fetch(`${base}/host/info`)).status).toBe(401);
      expect((await fetch(`${base}/api/v2/chats`, { headers: { Authorization: 'Bearer wrong' } })).status).toBe(401);
    });

    it('query token 可过 /api/v2/events（EventSource 不能设头）', async () => {
      mocks.busAttach.mockImplementation((res: { end: () => void }) => res.end());
      const res = await fetch(`${base}/api/v2/events?token=test-token`);
      expect(res.status).toBe(200);
      expect(mocks.busAttach).toHaveBeenCalledOnce();
    });

    it('静态路径无 token 仍 200（浏览器靠它拿 bootstrap 里的 token）', async () => {
      const res = await fetch(`${base}/`);
      expect(res.status).toBe(200);
    });
  });

  describe('静态托管', () => {
    it('GET / 返回 index.html 并注入 BS 引导标记（含 token）', async () => {
      const res = await fetch(`${base}/`);
      expect(res.status).toBe(200);
      expect(res.headers.get('content-type')).toContain('text/html');
      const html = await res.text();
      expect(html).toContain('window.__DEEPPATH_BS__');
      expect(html).toContain(process.platform);
      expect(html).toContain('"token":"test-token"');
    });

    it('静态资源按扩展名给 MIME 与长缓存', async () => {
      const res = await fetch(`${base}/app.js`);
      expect(res.headers.get('content-type')).toContain('text/javascript');
      expect(res.headers.get('cache-control')).toContain('max-age');
      expect(await res.text()).toBe('console.log(1)');
    });

    it('未知路径回落 index.html（SPA 路由）', async () => {
      const res = await fetch(`${base}/some/spa/route`);
      expect(res.status).toBe(200);
      expect(await res.text()).toContain('<body>app</body>');
    });

    it('路径穿越拿不到 webDistDir 外的文件（URL 规范化 + startsWith 双保险）', async () => {
      // WHATWG URL 解析会先折叠 `..`（`/../../../../etc/passwd` → `/etc/passwd`），
      // 所以穿越尝试实际命中的是 webDistDir 内不存在的路径 → SPA 回落 index.html；
      // startsWith 守卫是第二道。无论哪道，断言安全性质：响应不是目标文件内容。
      const { request } = await import('node:http');
      const port = (server.address() as AddressInfo).port;
      const body = await new Promise<string>((resolve, reject) => {
        const req = request(
          { host: '127.0.0.1', port, path: '/../../../../etc/passwd', method: 'GET' },
          (res) => {
            const chunks: Buffer[] = [];
            res.on('data', (c) => chunks.push(c));
            res.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
          },
        );
        req.on('error', reject);
        req.end();
      });
      expect(body).not.toContain('root:');
      expect(body).toContain('<body>app</body>');
    });

    it('POST 到非 api/host 路径 → 404', async () => {
      const res = await post('/whatever', {});
      expect(res.status).toBe(404);
    });
  });
});
