/**
 * LocalBackendRouter.handle() 路由测试。
 *
 * 技能导入/删除路由走真实 fs（临时目录），验证路径穿越围栏。
 *
 * 覆盖非流式 HTTP 路由面：路由匹配、请求体解析、4xx/5xx 错误形状、
 * 分页参数归一、项目注册表/任务服务/sidecar 的存在性分支，以及 fallback
 * 温柔降级。所有进程外依赖（localStore、sidecar、egress、insights 等）
 * 走 router-testkit 的内存 mock；不起真实 HTTP 服务器、不访问网络。
 *
 * 流式路由（/send、/regenerate、SSE）在 router-stream.test.ts。
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  DEFAULT_TOOL_SCHEMAS,
  h,
  makeBroadcast,
  makeMcpRegistry,
  makeProjectRegistry,
  makeSupervisor,
  makeToolRouter,
  resetRouterTestkit,
} from './router-testkit.js';
import { LocalBackendRouter, userFacingCoreLoopFailure } from '../../src/local-backend/router.js';
import { SidecarSupervisor } from '../../src/sidecar/index.js';
import type { ToolRouter } from '../../src/tool-router.js';
import type { TaskService } from '../../src/local-backend/task-service.js';

function makeRouter(options: {
  toolRouter?: Record<string, unknown>;
  broadcast?: ReturnType<typeof makeBroadcast>['broadcast'];
  taskService?: Partial<TaskService>;
} = {}): LocalBackendRouter {
  const toolRouter = (options.toolRouter ?? makeToolRouter()) as unknown as ToolRouter;
  return new LocalBackendRouter(toolRouter, {
    broadcast: options.broadcast,
    taskService: options.taskService as TaskService | undefined,
  });
}

beforeEach(() => {
  resetRouterTestkit();
  (SidecarSupervisor as unknown as { lastSpawnRefusal: unknown }).lastSpawnRefusal = null;
});

// ---------------------------------------------------------------------------
// userFacingCoreLoopFailure：底层错误 → 用户可读中文提示
// ---------------------------------------------------------------------------

describe('userFacingCoreLoopFailure', () => {
  it('HTTP 401/403 映射为认证失败提示', () => {
    expect(userFacingCoreLoopFailure('HTTP 401 Unauthorized')).toContain('认证失败');
    expect(userFacingCoreLoopFailure('HTTP 403 Forbidden')).toContain('认证失败');
  });

  it('HTTP 404 映射为模型/地址不存在提示', () => {
    expect(userFacingCoreLoopFailure('HTTP 404 Not Found')).toContain('baseUrl 与 model');
  });

  it('连接类错误（ECONNREFUSED/ENOTFOUND/ETIMEDOUT/fetch failed）映射为网络提示', () => {
    for (const reason of ['ECONNREFUSED', 'ENOTFOUND', 'ETIMEDOUT', 'fetch failed']) {
      expect(userFacingCoreLoopFailure(reason)).toContain('无法连接模型服务');
    }
  });

  it('其他错误原样拼进提示', () => {
    expect(userFacingCoreLoopFailure('some weird error')).toBe('[回复失败] some weird error');
  });
});

// ---------------------------------------------------------------------------
// 本地身份与虚拟 Agent
// ---------------------------------------------------------------------------

describe('本地身份与虚拟 Agent', () => {
  it('GET /api/v2/auth/me 返回本地用户（含 membership 与 settings）', async () => {
    const res = await makeRouter().handle({ method: 'GET', path: '/api/v2/auth/me' });
    expect(res.status).toBe(200);
    const user = res.data as Record<string, any>;
    expect(user.id).toBe('local');
    expect(user.isAdmin).toBe(true);
    expect(user.membership.isPro).toBe(true);
    expect(user.settings.timezone).toBe('Asia/Shanghai');
  });

  it('GET /api/v2/agents 返回永远在线的本地 Agent', async () => {
    const res = await makeRouter().handle({ method: 'GET', path: '/api/v2/agents' });
    expect(res.status).toBe(200);
    const data = res.data as { agents: Array<Record<string, unknown>>; total: number };
    expect(data.total).toBe(1);
    expect(data.agents[0].isOnline).toBe(true);
    expect(data.agents[0].capabilities).toMatchObject({ shell: true, file: true });
  });

  it('POST /api/v2/agents/:id/exec 缺 tool 返回 400', async () => {
    const res = await makeRouter().handle({
      method: 'POST',
      path: '/api/v2/agents/local-agent/exec',
      body: {},
    });
    expect(res.status).toBe(400);
    expect(res.data).toEqual({ detail: 'tool is required' });
  });

  it('POST /api/v2/agents/:id/exec 成功时经 ToolRouter 执行并回包 result', async () => {
    const toolRouter = makeToolRouter();
    toolRouter.execute.mockResolvedValue({ stdout: 'hello' });
    const res = await makeRouter({ toolRouter }).handle({
      method: 'POST',
      path: '/api/v2/agents/local-agent/exec',
      body: { tool: 'local_exec_shell', arguments: { command: 'echo hello' } },
    });
    expect(res.status).toBe(200);
    const data = res.data as Record<string, any>;
    expect(data.success).toBe(true);
    expect(data.requestId).toEqual(expect.any(String));
    expect(data.result).toEqual({ stdout: 'hello' });
    expect(toolRouter.execute).toHaveBeenCalledWith({
      name: 'local_exec_shell',
      arguments: { command: 'echo hello' },
    });
  });

  it('POST /api/v2/agents/:id/exec 工具抛错时返回 200 + success:false（不是 5xx）', async () => {
    const toolRouter = makeToolRouter();
    toolRouter.execute.mockRejectedValue(new Error('沙箱拒绝'));
    const res = await makeRouter({ toolRouter }).handle({
      method: 'POST',
      path: '/api/v2/agents/local-agent/exec',
      body: { tool: 'local_exec_shell' },
    });
    expect(res.status).toBe(200);
    const data = res.data as Record<string, any>;
    expect(data.success).toBe(false);
    expect(data.error).toBe('沙箱拒绝');
  });

  it('PATCH /api/v2/agents/:id 返回本地 Agent；DELETE 拒绝删除', async () => {
    const router = makeRouter();
    const patched = await router.handle({ method: 'PATCH', path: '/api/v2/agents/local-agent' });
    expect(patched.status).toBe(200);
    const deleted = await router.handle({ method: 'DELETE', path: '/api/v2/agents/local-agent' });
    expect(deleted.status).toBe(400);
    expect(deleted.data).toEqual({ detail: '本地 Agent 不可删除' });
  });
});

// ---------------------------------------------------------------------------
// 会话列表与 CRUD
// ---------------------------------------------------------------------------

describe('会话路由', () => {
  it('GET /api/v2/chats 返回分页形状；非法 page/limit 回落默认值', async () => {
    h.store.createChat('对话A', 'agent-a', null);
    h.store.createChat('对话B', 'agent-a', null);
    const res = await makeRouter().handle({
      method: 'GET',
      path: '/api/v2/chats?page=abc&limit=',
    });
    expect(res.status).toBe(200);
    const data = res.data as Record<string, any>;
    expect(data.chats).toHaveLength(2);
    // page=abc / limit='' 都被 parsePositiveIntParam 归一为 1/50，而不是 NaN。
    expect(data.pagination).toEqual({
      page: 1,
      limit: 50,
      total: 2,
      totalPages: 1,
      hasMore: false,
    });
  });

  it('GET /api/v2/chats 分页：totalPages 与 hasMore 按 limit 计算', async () => {
    for (let i = 0; i < 3; i += 1) h.store.createChat(`对话${i}`, 'agent-a', null);
    const res = await makeRouter().handle({ method: 'GET', path: '/api/v2/chats?page=1&limit=2' });
    const data = res.data as Record<string, any>;
    expect(data.chats).toHaveLength(2);
    expect(data.pagination).toMatchObject({ total: 3, totalPages: 2, hasMore: true });
  });

  it('POST /api/v2/chats/new 创建无项目对话', async () => {
    const res = await makeRouter().handle({ method: 'POST', path: '/api/v2/chats/new', body: {} });
    expect(res.status).toBe(200);
    const data = res.data as Record<string, any>;
    expect(data.success).toBe(true);
    expect(data.projectId).toBeNull();
    expect(data.isTemporary).toBe(false);
    expect(h.store.getChat(data.chatId)).not.toBeNull();
  });

  it('POST /api/v2/chats/new 带存在的 projectId 绑定项目；不存在则 400', async () => {
    const registry = makeProjectRegistry([
      { id: 'proj-1', name: '项目一', folderPath: '/tmp/proj-1', trusted: false },
    ]);
    const toolRouter = makeToolRouter({ projectRegistry: registry });
    const router = makeRouter({ toolRouter });

    const ok = await router.handle({
      method: 'POST',
      path: '/api/v2/chats/new',
      body: { projectId: 'proj-1' },
    });
    expect(ok.status).toBe(200);
    expect((ok.data as Record<string, any>).projectId).toBe('proj-1');

    const bad = await router.handle({
      method: 'POST',
      path: '/api/v2/chats/new',
      body: { projectId: 'ghost' },
    });
    expect(bad.status).toBe(400);
    expect((bad.data as Record<string, any>).detail).toContain('项目不存在');
  });

  it('POST /api/v2/chats/prune-empty 清掉空会话并跳过 exceptChatId', async () => {
    const emptyA = h.store.createChat('空A', 'agent-a', null);
    const emptyB = h.store.createChat('空B', 'agent-a', null);
    const withMsg = h.store.createChat('有消息', 'agent-a', null);
    h.store.addMessage(withMsg.id, 'user', 'hi');

    const res = await makeRouter().handle({
      method: 'POST',
      path: '/api/v2/chats/prune-empty',
      body: { exceptChatId: emptyB.id },
    });
    expect(res.status).toBe(200);
    const data = res.data as { deletedChatIds: string[] };
    expect(data.deletedChatIds).toEqual([emptyA.id]);
    expect(h.store.getChat(emptyB.id)).not.toBeNull();
    expect(h.store.getChat(withMsg.id)).not.toBeNull();
  });

  it('GET /api/v2/chats/:id 返回会话；不存在返回 404 detail', async () => {
    const chat = h.store.createChat('标题', 'agent-a', null);
    const router = makeRouter();
    const ok = await router.handle({ method: 'GET', path: `/api/v2/chats/${chat.id}` });
    expect(ok.status).toBe(200);
    expect((ok.data as Record<string, any>).title).toBe('标题');

    const missing = await router.handle({ method: 'GET', path: '/api/v2/chats/nope' });
    expect(missing.status).toBe(404);
    expect(missing.data).toEqual({ detail: 'Chat not found' });
  });

  it('DELETE /api/v2/chats/:id 删除会话；onlyIfEmpty=1 时有消息不删', async () => {
    const router = makeRouter();
    const chat = h.store.createChat('待删', 'agent-a', null);
    h.store.addMessage(chat.id, 'user', 'hi');

    const kept = await router.handle({
      method: 'DELETE',
      path: `/api/v2/chats/${chat.id}?onlyIfEmpty=1`,
    });
    expect((kept.data as Record<string, any>).deleted).toBe(false);
    expect(h.store.getChat(chat.id)).not.toBeNull();

    const removed = await router.handle({ method: 'DELETE', path: `/api/v2/chats/${chat.id}` });
    expect(removed.status).toBe(200);
    expect(h.store.getChat(chat.id)).toBeNull();

    const missing = await router.handle({ method: 'DELETE', path: `/api/v2/chats/${chat.id}` });
    expect(missing.status).toBe(404);
  });

  it('PUT /api/v2/chats/:id/pin 置顶/取消置顶；不存在 404', async () => {
    const chat = h.store.createChat('置顶', 'agent-a', null);
    const router = makeRouter();
    const pinned = await router.handle({
      method: 'PUT',
      path: `/api/v2/chats/${chat.id}/pin`,
      body: { isPinned: true },
    });
    expect(pinned.status).toBe(200);
    expect((pinned.data as Record<string, any>).message).toBe('已置顶');
    expect(h.store.getChat(chat.id)?.isPinned).toBe(true);

    const missing = await router.handle({
      method: 'PUT',
      path: '/api/v2/chats/nope/pin',
      body: { isPinned: true },
    });
    expect(missing.status).toBe(404);
  });

  it('PATCH /api/v2/chats/:id/settings：projectId 三态（缺省不动 / null 移出 / 字符串校验存在）', async () => {
    const registry = makeProjectRegistry([
      { id: 'proj-1', name: '项目一', folderPath: '/tmp/proj-1', trusted: false },
    ]);
    const router = makeRouter({ toolRouter: makeToolRouter({ projectRegistry: registry }) });
    const chat = h.store.createChat('设置', 'agent-a', 'proj-1');

    // 缺省：不动 projectId
    const untouched = await router.handle({
      method: 'PATCH',
      path: `/api/v2/chats/${chat.id}/settings`,
      body: { title: '新标题' },
    });
    expect((untouched.data as Record<string, any>).projectId).toBe('proj-1');
    expect(h.store.getChat(chat.id)?.title).toBe('新标题');

    // null：移出项目
    const detached = await router.handle({
      method: 'PATCH',
      path: `/api/v2/chats/${chat.id}/settings`,
      body: { projectId: null },
    });
    expect((detached.data as Record<string, any>).projectId).toBeNull();

    // 幽灵项目：400
    const ghost = await router.handle({
      method: 'PATCH',
      path: `/api/v2/chats/${chat.id}/settings`,
      body: { projectId: 'ghost' },
    });
    expect(ghost.status).toBe(400);
    expect((ghost.data as Record<string, any>).detail).toContain('项目不存在');

    // 非法类型：400
    const invalid = await router.handle({
      method: 'PATCH',
      path: `/api/v2/chats/${chat.id}/settings`,
      body: { projectId: 42 },
    });
    expect(invalid.status).toBe(400);
    expect((invalid.data as Record<string, any>).detail).toContain('projectId 必须是');
  });

  it('GET /api/v2/chats/:id/messages：tool 角色映射为 assistant，附分页形状', async () => {
    const chat = h.store.createChat('消息', 'agent-a', null);
    h.store.addMessage(chat.id, 'user', '问');
    h.store.addMessage(chat.id, 'tool', '{"toolCallId":"t1"}');
    h.store.addMessage(chat.id, 'assistant', '答');

    const res = await makeRouter().handle({
      method: 'GET',
      path: `/api/v2/chats/${chat.id}/messages`,
    });
    expect(res.status).toBe(200);
    const data = res.data as Record<string, any>;
    // listMessages DESC → 响应保持 DESC（最新在前）
    expect(data.messages.map((m: { role: string }) => m.role)).toEqual([
      'assistant',
      'assistant', // tool → assistant
      'user',
    ]);
    expect(data.pagination).toEqual({ limit: 200, cursor: null, hasMore: false });
    expect(data.interrupted).toBe(false);
  });

  it('GET /api/v2/chats/:id/messages：残留 turn_active 且末尾非 assistant 报 interrupted', async () => {
    const chat = h.store.createChat('中断', 'agent-a', null);
    h.store.addMessage(chat.id, 'user', '问到一半');
    h.store.setTurnActive(chat.id);

    const res = await makeRouter().handle({
      method: 'GET',
      path: `/api/v2/chats/${chat.id}/messages`,
    });
    expect((res.data as Record<string, any>).interrupted).toBe(true);

    // 同进程内流式进行中不误报中断
    h.activeStreamIds.set(chat.id, 'stream-1');
    const live = await makeRouter().handle({
      method: 'GET',
      path: `/api/v2/chats/${chat.id}/messages`,
    });
    expect((live.data as Record<string, any>).interrupted).toBe(false);
  });

  it('GET /api/v2/chats/:id/live-stream：无快照 active:false；有快照携带内容', async () => {
    const router = makeRouter();
    const idle = await router.handle({ method: 'GET', path: '/api/v2/chats/c1/live-stream' });
    expect(idle.data).toEqual({ active: false });

    // 真实 live-stream 注册表：直接注册一个快照模拟进行中的回合
    const { registerLiveStream, removeLiveStream } = await import(
      '../../src/local-backend/live-stream.js'
    );
    const live = registerLiveStream('c1', { executedActions: [], timeline: [], children: [] });
    live.content = '部分回复';
    const active = await router.handle({ method: 'GET', path: '/api/v2/chats/c1/live-stream' });
    const data = active.data as Record<string, any>;
    expect(data.active).toBe(true);
    expect(data.content).toBe('部分回复');
    removeLiveStream('c1');
  });

  it('POST /api/v2/chats/:id/cancel：无活跃流 409；有活跃流调 supervisor.cancelChat', async () => {
    const router = makeRouter();
    const noStream = await router.handle({ method: 'POST', path: '/api/v2/chats/c1/cancel' });
    expect(noStream.status).toBe(409);
    expect(noStream.data).toEqual({ success: false, reason: 'no_active_turn' });

    const supervisor = makeSupervisor();
    h.supervisor = supervisor;
    h.activeStreamIds.set('c1', 'stream-9');
    const ok = await router.handle({ method: 'POST', path: '/api/v2/chats/c1/cancel' });
    expect(ok.status).toBe(200);
    expect(ok.data).toEqual({ success: true });
    expect(supervisor.cancelChat).toHaveBeenCalledWith('stream-9');
  });
});

// ---------------------------------------------------------------------------
// 分支族（W1.2.1）
// ---------------------------------------------------------------------------

describe('分支族路由', () => {
  it('GET /branches：chat 不存在 404；sidecar 关闭时退化为空族', async () => {
    const router = makeRouter();
    const missing = await router.handle({ method: 'GET', path: '/api/v2/chats/nope/branches' });
    expect(missing.status).toBe(404);

    const chat = h.store.createChat('分支', 'agent-a', null);
    const res = await router.handle({ method: 'GET', path: `/api/v2/chats/${chat.id}/branches` });
    expect(res.status).toBe(200);
    expect(res.data).toEqual({ activeRecordId: chat.id, lineage: [], children: [] });
  });

  it('GET /branches：sidecar 开启时返回 lineage/children', async () => {
    h.supervisor = makeSupervisor();
    const chat = h.store.createChat('分支', 'agent-a', null);
    const res = await makeRouter().handle({
      method: 'GET',
      path: `/api/v2/chats/${chat.id}/branches`,
    });
    const data = res.data as Record<string, any>;
    expect(data.lineage).toEqual([{ recordId: 'rec-1' }]);
    expect(data.children).toEqual([{ recordId: 'rec-2' }]);
  });

  it('GET /branches/tree：sidecar 关闭时 tree 为 null', async () => {
    const chat = h.store.createChat('树', 'agent-a', null);
    const res = await makeRouter().handle({
      method: 'GET',
      path: `/api/v2/chats/${chat.id}/branches/tree`,
    });
    expect(res.data).toEqual({ activeRecordId: chat.id, tree: null, nodeCount: 0, truncated: false });
  });

  it('POST /branches/activate：缺 recordId 400；sidecar 未运行 503', async () => {
    const chat = h.store.createChat('激活', 'agent-a', null);
    const router = makeRouter();

    const noId = await router.handle({
      method: 'POST',
      path: `/api/v2/chats/${chat.id}/branches/activate`,
      body: {},
    });
    expect(noId.status).toBe(400);
    expect(noId.data).toEqual({ detail: 'recordId is required' });

    const noSidecar = await router.handle({
      method: 'POST',
      path: `/api/v2/chats/${chat.id}/branches/activate`,
      body: { recordId: 'rec-2' },
    });
    expect(noSidecar.status).toBe(503);
    expect(noSidecar.data).toEqual({ detail: 'sidecar unavailable' });
  });

  it('POST /branches/activate：族外记录 403（fail-closed）；族内记录切换并重投影消息', async () => {
    const supervisor = makeSupervisor();
    h.supervisor = supervisor;
    const chat = h.store.createChat('激活', 'agent-a', null);
    h.store.addMessage(chat.id, 'user', '旧投影');
    const router = makeRouter();

    const outsider = await router.handle({
      method: 'POST',
      path: `/api/v2/chats/${chat.id}/branches/activate`,
      body: { recordId: 'rec-outsider' },
    });
    expect(outsider.status).toBe(403);
    expect((outsider.data as Record<string, any>).detail).toContain('outside the chat branch family');

    const ok = await router.handle({
      method: 'POST',
      path: `/api/v2/chats/${chat.id}/branches/activate`,
      body: { recordId: 'rec-2' },
    });
    expect(ok.status).toBe(200);
    // messageCount 是投影全量（含被过滤的 tool 消息）
    expect(ok.data).toEqual({ activeRecordId: 'rec-2', messageCount: 3 });
    expect(h.store.getChatRecordId(chat.id)).toBe('rec-2');
    // UI 存储被重投影：只剩 user/assistant，tool 消息被过滤
    const messages = h.store.listMessages(chat.id, 10);
    expect(messages.map((m) => m.role).sort()).toEqual(['assistant', 'user']);
  });

  it('POST /branches/activate：投影缺失时 404', async () => {
    const supervisor = makeSupervisor({ sessionMessages: vi.fn(async () => null) });
    h.supervisor = supervisor;
    const chat = h.store.createChat('激活', 'agent-a', null);
    const res = await makeRouter().handle({
      method: 'POST',
      path: `/api/v2/chats/${chat.id}/branches/activate`,
      body: { recordId: 'rec-2' },
    });
    expect(res.status).toBe(404);
    expect((res.data as Record<string, any>).detail).toContain('record not found');
  });
});

// ---------------------------------------------------------------------------
// 项目模式
// ---------------------------------------------------------------------------

describe('项目路由', () => {
  it('注册表未注入时 /api/v2/projects 返回 503', async () => {
    const res = await makeRouter().handle({ method: 'GET', path: '/api/v2/projects' });
    expect(res.status).toBe(503);
    expect(res.data).toEqual({ error: '项目注册表不可用' });
  });

  it('GET 列表 / POST 创建；创建校验失败返回 400 error', async () => {
    const registry = makeProjectRegistry();
    const router = makeRouter({ toolRouter: makeToolRouter({ projectRegistry: registry }) });

    const created = await router.handle({
      method: 'POST',
      path: '/api/v2/projects',
      body: { name: '演示', folderPath: '/tmp/demo' },
    });
    expect(created.status).toBe(200);
    expect((created.data as Record<string, any>).project.name).toBe('演示');

    const invalid = await router.handle({
      method: 'POST',
      path: '/api/v2/projects',
      body: { name: '', folderPath: '/tmp/demo' },
    });
    expect(invalid.status).toBe(400);
    expect((invalid.data as Record<string, any>).error).toContain('名称');

    const list = await router.handle({ method: 'GET', path: '/api/v2/projects' });
    expect((list.data as Record<string, any>).projects).toHaveLength(1);
  });

  it('PUT /api/v2/projects/:id：不存在 404，其他校验错误 400', async () => {
    const registry = makeProjectRegistry([
      { id: 'proj-1', name: '旧名', folderPath: '/tmp/p1', trusted: false },
    ]);
    const router = makeRouter({ toolRouter: makeToolRouter({ projectRegistry: registry }) });

    const renamed = await router.handle({
      method: 'PUT',
      path: '/api/v2/projects/proj-1',
      body: { name: '新名' },
    });
    expect(renamed.status).toBe(200);
    expect(registry.get('proj-1')?.name).toBe('新名');

    const missing = await router.handle({
      method: 'PUT',
      path: '/api/v2/projects/ghost',
      body: { name: 'x' },
    });
    expect(missing.status).toBe(404);
  });

  it('DELETE /api/v2/projects/:id：解绑会话但不删会话；不存在 404', async () => {
    const registry = makeProjectRegistry([
      { id: 'proj-1', name: '项目', folderPath: '/tmp/p1', trusted: false },
    ]);
    const router = makeRouter({ toolRouter: makeToolRouter({ projectRegistry: registry }) });
    const chat = h.store.createChat('项目对话', 'agent-a', 'proj-1');

    const res = await router.handle({ method: 'DELETE', path: '/api/v2/projects/proj-1' });
    expect(res.status).toBe(200);
    expect((res.data as Record<string, any>).detachedChats).toBe(1);
    // 会话降级为无项目对话，而不是被删
    expect(h.store.getChat(chat.id)?.projectId).toBeNull();

    const missing = await router.handle({ method: 'DELETE', path: '/api/v2/projects/proj-1' });
    expect(missing.status).toBe(404);
  });

  it('PUT /api/v2/projects/:id/trust：授予/撤销信任；不存在 404', async () => {
    const registry = makeProjectRegistry([
      { id: 'proj-1', name: '项目', folderPath: '/tmp/p1', trusted: false },
    ]);
    const router = makeRouter({ toolRouter: makeToolRouter({ projectRegistry: registry }) });

    const trusted = await router.handle({
      method: 'PUT',
      path: '/api/v2/projects/proj-1/trust',
      body: { trusted: true },
    });
    expect(trusted.status).toBe(200);
    expect(registry.isTrusted('proj-1')).toBe(true);

    const missing = await router.handle({
      method: 'PUT',
      path: '/api/v2/projects/ghost/trust',
      body: { trusted: true },
    });
    expect(missing.status).toBe(404);
  });

  it('GET /api/v2/chats/:id/project-context：未绑项目返回 project:null；已信任且含规则文件时 rulesActive', async () => {
    const registry = makeProjectRegistry([
      { id: 'proj-1', name: '项目', folderPath: '/tmp/p1', trusted: true },
    ]);
    h.loadProjectRuleFiles.mockReturnValue({ files: ['AGENTS.md'], content: '规则内容' });
    const router = makeRouter({ toolRouter: makeToolRouter({ projectRegistry: registry }) });

    const unbound = h.store.createChat('无项目', 'agent-a', null);
    const res1 = await router.handle({ method: 'GET', path: `/api/v2/chats/${unbound.id}/project-context` });
    expect(res1.data).toEqual({ project: null });

    const bound = h.store.createChat('有项目', 'agent-a', 'proj-1');
    const res2 = await router.handle({ method: 'GET', path: `/api/v2/chats/${bound.id}/project-context` });
    const data = res2.data as Record<string, any>;
    expect(data.project).toMatchObject({ id: 'proj-1', trusted: true });
    expect(data.ruleFileCount).toBe(1);
    expect(data.rulesActive).toBe(true);

    const missing = await router.handle({ method: 'GET', path: '/api/v2/chats/nope/project-context' });
    expect(missing.status).toBe(404);
  });
});

// ---------------------------------------------------------------------------
// 任务面板（4.6a/4.6c）
// ---------------------------------------------------------------------------

describe('任务路由', () => {
  it('未注入 taskService 时任务路由 503', async () => {
    const chat = h.store.createChat('任务', 'agent-a', null);
    const router = makeRouter();
    const list = await router.handle({ method: 'GET', path: `/api/v2/chats/${chat.id}/tasks` });
    expect(list.status).toBe(503);
    expect(list.data).toEqual({ detail: 'task service unavailable' });

    const proc = await router.handle({ method: 'GET', path: '/api/v2/tasks/t1/process' });
    expect(proc.status).toBe(503);

    const merge = await router.handle({ method: 'POST', path: '/api/v2/tasks/t1/merge' });
    expect(merge.status).toBe(503);
  });

  it('GET /api/v2/chats/:id/tasks：chat 不存在 404；存在返回任务列表', async () => {
    const taskService: Partial<TaskService> = {};
    const router = makeRouter({ taskService });
    const missing = await router.handle({ method: 'GET', path: '/api/v2/chats/nope/tasks' });
    expect(missing.status).toBe(404);

    const chat = h.store.createChat('任务', 'agent-a', null);
    h.store.state.tasks.push({ id: 't1', chatId: chat.id, task: '跑一下' } as never);
    const res = await router.handle({ method: 'GET', path: `/api/v2/chats/${chat.id}/tasks` });
    expect((res.data as Record<string, any>).tasks).toHaveLength(1);
  });

  it('GET /api/v2/tasks/:id/process：快照不存在 404；存在返回 task/timeline/live/stale', async () => {
    const snapshot = {
      task: { id: 't1' },
      timeline: [{ type: 'text' }],
      live: null,
      stale: false,
    };
    const taskService: Partial<TaskService> = {
      getProcess: vi.fn((id: string) => (id === 't1' ? snapshot : null)) as never,
    };
    const router = makeRouter({ taskService });

    const missing = await router.handle({ method: 'GET', path: '/api/v2/tasks/nope/process' });
    expect(missing.status).toBe(404);
    expect(missing.data).toEqual({ detail: '任务不存在' });

    const ok = await router.handle({ method: 'GET', path: '/api/v2/tasks/t1/process' });
    expect(ok.status).toBe(200);
    expect((ok.data as Record<string, any>).task).toEqual({ id: 't1' });
  });

  it('POST /api/v2/tasks/:id/merge|discard：404 任务不存在 / 409 状态冲突 / 200 成功', async () => {
    const taskService: Partial<TaskService> = {
      mergeTaskWorktree: vi.fn(async (id: string) => {
        if (id === 'ghost') throw new Error('任务不存在：ghost');
        if (id === 'settled') throw new Error('worktree 已合并');
        return { id } as never;
      }),
      discardTaskWorktree: vi.fn(async (id: string) => ({ id, discarded: true }) as never),
    };
    const router = makeRouter({ taskService });

    const missing = await router.handle({ method: 'POST', path: '/api/v2/tasks/ghost/merge' });
    expect(missing.status).toBe(404);

    const conflict = await router.handle({ method: 'POST', path: '/api/v2/tasks/settled/merge' });
    expect(conflict.status).toBe(409);
    expect((conflict.data as Record<string, any>).detail).toContain('worktree 已合并');

    const ok = await router.handle({ method: 'POST', path: '/api/v2/tasks/t1/discard' });
    expect(ok.status).toBe(200);
    expect((ok.data as Record<string, any>).task).toEqual({ id: 't1', discarded: true });
  });
});

// ---------------------------------------------------------------------------
// 智能体管理
// ---------------------------------------------------------------------------

describe('智能体路由', () => {
  it('GET /api/v2/chat-agents：默认不含已归档；include_archived=true 含', async () => {
    const active = h.store.createChatAgent({ name: '活跃' });
    const archived = h.store.createChatAgent({ name: '归档' });
    h.store.archiveChatAgent(archived.id);

    const router = makeRouter();
    const res = await router.handle({ method: 'GET', path: '/api/v2/chat-agents' });
    const data = res.data as Record<string, any>;
    expect(data.total).toBe(1);
    expect(data.agents[0].id).toBe(active.id);

    const all = await router.handle({
      method: 'GET',
      path: '/api/v2/chat-agents?include_archived=true',
    });
    expect((all.data as Record<string, any>).total).toBe(2);
  });

  it('POST /api/v2/chat-agents：缺省名称「新助手」，allowExternalSkills 默认 true', async () => {
    const res = await makeRouter().handle({
      method: 'POST',
      path: '/api/v2/chat-agents',
      body: {},
    });
    expect(res.status).toBe(200);
    const agent = (res.data as Record<string, any>).agent;
    expect(agent.name).toBe('新助手');
    expect(agent.allowExternalSkills).toBe(true);
    expect(agent.toolPolicy).toEqual({ mode: 'all', tools: [] });
  });

  it('GET/PATCH/DELETE /api/v2/chat-agents/:id', async () => {
    const agent = h.store.createChatAgent({ name: '小助手', rolePrompt: '你是小助手' });
    const router = makeRouter();

    const got = await router.handle({ method: 'GET', path: `/api/v2/chat-agents/${agent.id}` });
    expect((got.data as Record<string, any>).rolePrompt).toBe('你是小助手');

    const patched = await router.handle({
      method: 'PATCH',
      path: `/api/v2/chat-agents/${agent.id}`,
      body: { name: '改名', toolPolicy: { mode: 'denylist', tools: ['local_exec_shell'] } },
    });
    expect((patched.data as Record<string, any>).agent.name).toBe('改名');
    expect(h.store.getChatAgent(agent.id)?.toolPolicy).toEqual({
      mode: 'denylist',
      tools: ['local_exec_shell'],
    });

    const deleted = await router.handle({
      method: 'DELETE',
      path: `/api/v2/chat-agents/${agent.id}`,
    });
    expect(deleted.data).toEqual({ id: agent.id, status: 'archived' });
    expect(h.store.getChatAgent(agent.id)?.isArchived).toBe(true);

    const missing = await router.handle({ method: 'GET', path: '/api/v2/chat-agents/nope' });
    expect(missing.status).toBe(404);
  });

  it('POST /api/v2/chat-agents/generate 返回草稿（名称取描述前 16 字）', async () => {
    const res = await makeRouter().handle({
      method: 'POST',
      path: '/api/v2/chat-agents/generate',
      body: { description: '一个专门处理 CSV 数据清洗与分析的助手' },
    });
    const draft = (res.data as Record<string, any>).draft;
    expect(draft.name).toBe('一个专门处理 CSV 数据清洗与');
    expect(draft.allowExternalSkills).toBe(true);
  });

  it('GET /api/v2/chat-agents/tools：mcp_ 前缀归类 external，含 write 归类 write', async () => {
    const toolRouter = makeToolRouter({
      schemas: [
        ...DEFAULT_TOOL_SCHEMAS,
        { name: 'mcp__srv__read', description: '外部', inputSchema: {}, mode: 'read' as const },
      ],
    });
    const res = await makeRouter({ toolRouter }).handle({
      method: 'GET',
      path: '/api/v2/chat-agents/tools',
    });
    const tools = (res.data as Record<string, any>).tools as Array<Record<string, unknown>>;
    const byName = new Map(tools.map((t) => [t.name, t]));
    expect(byName.get('local_write_file')).toMatchObject({ category: 'local', classification: 'write' });
    expect(byName.get('local_read_file')).toMatchObject({ category: 'local', classification: 'read' });
    expect(byName.get('mcp__srv__read')).toMatchObject({ category: 'external' });
  });

  it('GET /api/v2/chat-agents/templates 返回空模板列表', async () => {
    const res = await makeRouter().handle({ method: 'GET', path: '/api/v2/chat-agents/templates' });
    expect(res.data).toEqual({ templates: [] });
  });

  it('GET /api/v2/chat-agents/skills：按 user > workspace > builtin 排序；加载失败 500', async () => {
    h.loadSkills.mockResolvedValue([
      { name: 'b1', dirName: 'b1', displayName: '', description: '', priority: 1, tags: [], layer: 'catalog', modelInvocable: true, skillsDir: '/tmp/builtin-skills/b1' },
      { name: 'u1', dirName: 'u1', displayName: '', description: '', priority: 1, tags: [], layer: 'catalog', modelInvocable: true, skillsDir: '/tmp/user-skills/u1' },
      { name: 'w1', dirName: 'w1', displayName: '', description: '', priority: 1, tags: [], layer: 'catalog', modelInvocable: true, skillsDir: '/tmp/workspace-skills/w1' },
    ]);
    const res = await makeRouter().handle({ method: 'GET', path: '/api/v2/chat-agents/skills' });
    const skills = (res.data as Record<string, any>).skills as Array<Record<string, unknown>>;
    expect(skills.map((s) => s.origin)).toEqual(['user', 'workspace', 'builtin']);
    expect(skills[0]).toMatchObject({ id: 'u1', enabled: true, isBuiltin: false });
    expect(skills[2]).toMatchObject({ isBuiltin: true });

    h.loadSkills.mockRejectedValue(new Error('磁盘炸了'));
    const failed = await makeRouter().handle({ method: 'GET', path: '/api/v2/chat-agents/skills' });
    expect(failed.status).toBe(500);
    expect((failed.data as Record<string, any>).error).toContain('磁盘炸了');
  });

  it('GET /api/v2/chat-agents/mcp-tools：无注册表返回空；已启用无缓存的服务触发后台刷新', async () => {
    const noRegistry = await makeRouter().handle({
      method: 'GET',
      path: '/api/v2/chat-agents/mcp-tools',
    });
    expect(noRegistry.data).toEqual({ mcpTools: [] });

    const mcpRegistry = makeMcpRegistry({
      servers: [
        { id: 's1', name: '服务一', enabled: true },
        { id: 's2', name: '服务二', enabled: false },
      ],
      toolEntries: [{ token: 'mcp__key-s1__ping', toolName: 'ping', serverName: '服务一' }],
    });
    const toolRouter = makeToolRouter({ mcpRegistry });
    const res = await makeRouter({ toolRouter }).handle({
      method: 'GET',
      path: '/api/v2/chat-agents/mcp-tools',
    });
    const data = res.data as { mcpTools: Array<Record<string, unknown>> };
    expect(data.mcpTools).toHaveLength(1);
    // 自愈：s1 已启用且无缓存 → 补一次后台刷新；s2 未启用不刷
    expect(mcpRegistry.refreshTools).toHaveBeenCalledWith('s1');
    expect(mcpRegistry.refreshTools).not.toHaveBeenCalledWith('s2');
  });
});

// ---------------------------------------------------------------------------
// 技能导入 / 删除（真实 fs + 临时目录；installSkillFromDirectory 为 mock）
// ---------------------------------------------------------------------------

describe('技能导入与删除', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'router-skills-'));
    h.userSkillsDir = path.join(tmpDir, 'user-skills');
    fs.mkdirSync(h.userSkillsDir, { recursive: true });
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('POST /skills/import：缺 path 400；路径不存在 404', async () => {
    const router = makeRouter();
    const noPath = await router.handle({
      method: 'POST',
      path: '/api/v2/chat-agents/skills/import',
      body: {},
    });
    expect(noPath.status).toBe(400);
    expect(noPath.data).toEqual({ error: 'path is required' });

    const missing = await router.handle({
      method: 'POST',
      path: '/api/v2/chat-agents/skills/import',
      body: { path: path.join(tmpDir, 'not-exist') },
    });
    expect(missing.status).toBe(404);
    expect((missing.data as Record<string, any>).error).toContain('未找到技能文件');
  });

  it('POST /skills/import：命中含 SKILL.md 的目录 → 安装并刷新缓存', async () => {
    const skillDir = path.join(tmpDir, 'my-skill');
    fs.mkdirSync(skillDir);
    fs.writeFileSync(path.join(skillDir, 'SKILL.md'), '---\nname: my-skill\n---\n正文');
    h.installSkillFromDirectory.mockReturnValue({ name: 'my-skill', dest: '/dest/my-skill' });

    const res = await makeRouter().handle({
      method: 'POST',
      path: '/api/v2/chat-agents/skills/import',
      body: { path: skillDir },
    });
    expect(res.status).toBe(200);
    expect(res.data).toEqual({ success: true, name: 'my-skill', status: 'imported' });
    expect(h.installSkillFromDirectory).toHaveBeenCalledWith(skillDir);
    // 安装后刷新技能缓存
    expect(h.loadSkills).toHaveBeenCalledWith({ reload: true, ignoreConditions: true });
  });

  it('POST /skills/import：直接指向 SKILL.md 文件也可导入', async () => {
    const skillDir = path.join(tmpDir, 'file-skill');
    fs.mkdirSync(skillDir);
    const skillFile = path.join(skillDir, 'SKILL.md');
    fs.writeFileSync(skillFile, '---\nname: file-skill\n---\n正文');

    const res = await makeRouter().handle({
      method: 'POST',
      path: '/api/v2/chat-agents/skills/import',
      body: { path: skillFile },
    });
    expect(res.status).toBe(200);
    expect(h.installSkillFromDirectory).toHaveBeenCalledWith(skillDir);
  });

  it('POST /skills/import：安装抛错 → 500', async () => {
    const skillDir = path.join(tmpDir, 'bad-skill');
    fs.mkdirSync(skillDir);
    fs.writeFileSync(path.join(skillDir, 'SKILL.md'), '---\nname: bad\n---\n');
    h.installSkillFromDirectory.mockImplementation(() => {
      throw new Error('目标目录已存在');
    });
    const res = await makeRouter().handle({
      method: 'POST',
      path: '/api/v2/chat-agents/skills/import',
      body: { path: skillDir },
    });
    expect(res.status).toBe(500);
    expect((res.data as Record<string, any>).error).toContain('目标目录已存在');
  });

  it('DELETE /skills/delete/：空名称 400', async () => {
    const res = await makeRouter().handle({
      method: 'DELETE',
      path: '/api/v2/chat-agents/skills/delete/',
    });
    expect(res.status).toBe(400);
    expect(res.data).toEqual({ error: 'skillName is required' });
  });

  it('DELETE /skills/delete/..：URL 归一化吃掉点段，根本到不了删除路由', async () => {
    // WHATWG URL 会把 '/delete/..' 归一成 '/skills/'，删除路由匹配不上，
    // 落到 fallback 的 noop——第一道防线在 URL 解析层。
    const inside = path.join(h.userSkillsDir, 'real-skill');
    fs.mkdirSync(inside);

    const res = await makeRouter().handle({
      method: 'DELETE',
      path: '/api/v2/chat-agents/skills/delete/..',
    });
    expect(res.status).toBe(200);
    expect(res.data).toEqual({ success: true, message: 'noop (local fallback)' });
    expect(fs.existsSync(inside)).toBe(true);
  });

  it('DELETE /skills/delete/：编码分隔符 decode 出 .. 仍被路由内围栏拦下，deleted:false', async () => {
    // 第二道防线在路由内：rmSync 目标必须 resolve 到 userSkillsDir 内部。
    // 'a%2F..%2F..' decode 后是 'a/../../..'——path.join 直接逃逸出技能
    // 目录，isInsideSkillsDir 拒绝；扫描匹配也不会命中。userSkillsDir
    // 内外的目录都必须原样保留。
    const inside = path.join(h.userSkillsDir, 'real-skill');
    const outside = path.join(tmpDir, 'outside-skill');
    fs.mkdirSync(inside);
    fs.mkdirSync(outside);

    const res = await makeRouter().handle({
      method: 'DELETE',
      path: '/api/v2/chat-agents/skills/delete/a%2F..%2F..',
    });
    expect(res.status).toBe(200);
    expect(res.data).toEqual({ success: true, deleted: false });
    expect(fs.existsSync(inside)).toBe(true);
    expect(fs.existsSync(outside)).toBe(true);
  });

  it('DELETE /skills/delete/:name：按目录名精确删除并刷新缓存', async () => {
    const skillDir = path.join(h.userSkillsDir, 'my-skill');
    fs.mkdirSync(skillDir);
    fs.writeFileSync(path.join(skillDir, 'SKILL.md'), '---\nname: my-skill\n---\n');

    const res = await makeRouter().handle({
      method: 'DELETE',
      path: '/api/v2/chat-agents/skills/delete/my-skill',
    });
    expect(res.status).toBe(200);
    expect(res.data).toEqual({ success: true, deleted: true });
    expect(fs.existsSync(skillDir)).toBe(false);
    expect(h.loadSkills).toHaveBeenCalledWith({ reload: true, ignoreConditions: true });
  });

  it('DELETE /skills/delete/:name：按 SKILL.md frontmatter 名称（大小写不敏感）匹配删除', async () => {
    // 目录名与 frontmatter name 不同：走扫描分支，按解析出的 name 匹配。
    const skillDir = path.join(h.userSkillsDir, 'dir-name-xyz');
    fs.mkdirSync(skillDir);
    fs.writeFileSync(path.join(skillDir, 'SKILL.md'), '---\nname: "FancySkill"\n---\n正文');

    const res = await makeRouter().handle({
      method: 'DELETE',
      path: '/api/v2/chat-agents/skills/delete/fancyskill',
    });
    expect(res.status).toBe(200);
    expect((res.data as Record<string, any>).deleted).toBe(true);
    expect(fs.existsSync(skillDir)).toBe(false);
  });

  it('DELETE /skills/delete/:name：含空格名称经 %20 编码后能匹配删除（路径段 decode）', async () => {
    // URL.pathname 保留百分号编码；不 decode 的话 fancy%20skill 永远
    // 匹配不上目录 fancy skill（projects/mcp 路由段都 decode，这里对齐）。
    const skillDir = path.join(h.userSkillsDir, 'fancy skill');
    fs.mkdirSync(skillDir);
    fs.writeFileSync(path.join(skillDir, 'SKILL.md'), '---\nname: fancy skill\n---\n');

    const res = await makeRouter().handle({
      method: 'DELETE',
      path: '/api/v2/chat-agents/skills/delete/fancy%20skill',
    });
    expect(res.status).toBe(200);
    expect(res.data).toEqual({ success: true, deleted: true });
    expect(fs.existsSync(skillDir)).toBe(false);
  });

  it('DELETE /skills/delete/:name：非法百分号编码 → 400 而不是抛异常', async () => {
    const res = await makeRouter().handle({
      method: 'DELETE',
      path: '/api/v2/chat-agents/skills/delete/%E4%B8',
    });
    expect(res.status).toBe(400);
  });

  it('DELETE /skills/delete/:name：不存在也返回 success（幂等，避免「删除失败」弹窗）', async () => {
    const res = await makeRouter().handle({
      method: 'DELETE',
      path: '/api/v2/chat-agents/skills/delete/ghost-skill',
    });
    expect(res.status).toBe(200);
    expect(res.data).toEqual({ success: true, deleted: false });
  });
});

// ---------------------------------------------------------------------------
// MCP 服务管理
// ---------------------------------------------------------------------------

describe('MCP 服务路由', () => {
  it('注册表未注入时 MCP 路由 503', async () => {
    const router = makeRouter();
    for (const [method, path] of [
      ['GET', '/api/v2/mcp/servers'],
      ['POST', '/api/v2/mcp/servers'],
      ['POST', '/api/v2/mcp/servers/import'],
      ['PUT', '/api/v2/mcp/servers/s1'],
      ['DELETE', '/api/v2/mcp/servers/s1'],
      ['POST', '/api/v2/mcp/servers/s1/test'],
    ] as const) {
      const res = await router.handle({ method, path });
      expect(res.status).toBe(503);
      expect(res.data).toEqual({ error: 'MCP 注册表不可用' });
    }
  });

  it('GET /api/v2/mcp/servers：附带 serverKey/工具数/预览/错误字段', async () => {
    const toolCache = new Map([
      ['s1', {
        tools: Array.from({ length: 10 }, (_, i) => ({ name: `tool-${i}` })),
        error: null,
        fetchedAt: '2026-09-16T00:00:00Z',
      }],
    ]);
    const mcpRegistry = makeMcpRegistry({
      servers: [{ id: 's1', name: '服务一', enabled: true }],
      toolCache,
    });
    const res = await makeRouter({ toolRouter: makeToolRouter({ mcpRegistry }) }).handle({
      method: 'GET',
      path: '/api/v2/mcp/servers',
    });
    const servers = (res.data as Record<string, any>).servers as Array<Record<string, unknown>>;
    expect(servers[0]).toMatchObject({
      serverKey: 'key-s1',
      toolCount: 10,
      toolsPreview: Array.from({ length: 8 }, (_, i) => `tool-${i}`), // 预览截断到 8
      lastError: null,
      lastFetchedAt: '2026-09-16T00:00:00Z',
    });
  });

  it('POST /api/v2/mcp/servers：创建后触发后台工具刷新；创建抛错 400', async () => {
    const mcpRegistry = makeMcpRegistry();
    const router = makeRouter({ toolRouter: makeToolRouter({ mcpRegistry }) });

    const created = await router.handle({
      method: 'POST',
      path: '/api/v2/mcp/servers',
      body: { name: 'fs', command: 'npx', args: ['-y', '@mcp/fs'], env: { A: 1 } },
    });
    expect(created.status).toBe(200);
    expect(mcpRegistry.create).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'fs', env: { A: '1' } }), // env 值归一为字符串
    );
    expect(mcpRegistry.refreshTools).toHaveBeenCalledWith('srv-1');

    mcpRegistry.create.mockImplementationOnce(() => {
      throw new Error('command is required');
    });
    const invalid = await router.handle({
      method: 'POST',
      path: '/api/v2/mcp/servers',
      body: { name: 'x' },
    });
    expect(invalid.status).toBe(400);
    expect((invalid.data as Record<string, any>).error).toContain('command is required');
  });

  it('PUT /api/v2/mcp/servers/:id：更新后 enabled 服务触发刷新；DELETE 返回 deleted', async () => {
    const mcpRegistry = makeMcpRegistry({
      servers: [{ id: 's1', name: '服务一', enabled: true }],
    });
    const router = makeRouter({ toolRouter: makeToolRouter({ mcpRegistry }) });

    const updated = await router.handle({
      method: 'PUT',
      path: '/api/v2/mcp/servers/s1',
      body: { name: '改名' },
    });
    expect(updated.status).toBe(200);
    expect((updated.data as Record<string, any>).server.name).toBe('改名');
    expect(mcpRegistry.refreshTools).toHaveBeenCalledWith('s1');

    mcpRegistry.update.mockImplementationOnce(() => {
      throw new Error('server not found');
    });
    const missing = await router.handle({
      method: 'PUT',
      path: '/api/v2/mcp/servers/ghost',
      body: {},
    });
    expect(missing.status).toBe(400);

    const deleted = await router.handle({ method: 'DELETE', path: '/api/v2/mcp/servers/s1' });
    expect(deleted.data).toEqual({ success: true, deleted: true });
  });

  it('POST /api/v2/mcp/servers/import：导入失败返回 400 并带「导入失败」前缀', async () => {
    const mcpRegistry = makeMcpRegistry();
    mcpRegistry.importClaudeConfig.mockImplementation(() => {
      throw new Error('JSON 解析失败');
    });
    const res = await makeRouter({ toolRouter: makeToolRouter({ mcpRegistry }) }).handle({
      method: 'POST',
      path: '/api/v2/mcp/servers/import',
      body: { json: '{bad' },
    });
    expect(res.status).toBe(400);
    expect((res.data as Record<string, any>).error).toContain('导入失败');
  });

  it('POST /api/v2/mcp/servers/:id/test：返回工具数与错误；抛错 400', async () => {
    const toolCache = new Map([
      ['s1', { tools: [{ name: 'ping', description: 'p' }], error: null, fetchedAt: 't' }],
    ]);
    const mcpRegistry = makeMcpRegistry({
      servers: [{ id: 's1', name: '服务一', enabled: true }],
      toolCache,
    });
    const router = makeRouter({ toolRouter: makeToolRouter({ mcpRegistry }) });
    const res = await router.handle({ method: 'POST', path: '/api/v2/mcp/servers/s1/test' });
    expect(res.status).toBe(200);
    expect(res.data).toMatchObject({ success: true, toolCount: 1, tools: [{ name: 'ping', description: 'p' }] });

    mcpRegistry.refreshTools.mockRejectedValueOnce(new Error('连接被拒'));
    const failed = await router.handle({ method: 'POST', path: '/api/v2/mcp/servers/s1/test' });
    expect(failed.status).toBe(400);
  });
});

// ---------------------------------------------------------------------------
// LLM 设置与 sidecar 服务化路由
// ---------------------------------------------------------------------------

describe('LLM 设置与 sidecar 服务化路由', () => {
  it('GET /api/v2/compat/flags：sidecar 未就绪 503；RPC 失败 502；成功 200', async () => {
    const router = makeRouter();
    const offline = await router.handle({ method: 'GET', path: '/api/v2/compat/flags' });
    expect(offline.status).toBe(503);
    expect(offline.data).toMatchObject({ flags: [] });

    const supervisor = makeSupervisor({
      call: vi.fn(async () => ({ flags: [{ key: 'supportsUsageInStreaming' }] })),
    });
    h.pendingSupervisor = supervisor;
    const ok = await router.handle({ method: 'GET', path: '/api/v2/compat/flags' });
    expect(ok.status).toBe(200);
    expect((ok.data as Record<string, any>).flags).toHaveLength(1);
    expect(supervisor.call).toHaveBeenCalledWith('compat.describe');

    supervisor.call.mockRejectedValueOnce(new Error('rpc down'));
    const failed = await router.handle({ method: 'GET', path: '/api/v2/compat/flags' });
    expect(failed.status).toBe(502);
    expect((failed.data as Record<string, any>).error).toContain('compat.describe 失败');
  });

  it('GET /api/v2/llm/presets 与 /presets/resolve、/catalog：503/200/502 三分支', async () => {
    const router = makeRouter();
    expect((await router.handle({ method: 'GET', path: '/api/v2/llm/presets' })).status).toBe(503);
    expect((await router.handle({ method: 'GET', path: '/api/v2/llm/presets/resolve' })).status).toBe(503);
    expect((await router.handle({ method: 'GET', path: '/api/v2/llm/catalog' })).status).toBe(503);

    const supervisor = makeSupervisor({
      call: vi.fn(async (method: string) => {
        if (method === 'presets.describe') return { presets: [{ id: 'p1' }] };
        if (method === 'presets.resolve') return { preset: { id: 'p1' } };
        if (method === 'catalog.describe') return { providers: [{ id: 'openai' }] };
        return {};
      }),
    });
    h.pendingSupervisor = supervisor;

    const presets = await router.handle({ method: 'GET', path: '/api/v2/llm/presets' });
    expect((presets.data as Record<string, any>).presets).toEqual([{ id: 'p1' }]);

    const resolved = await router.handle({
      method: 'GET',
      path: '/api/v2/llm/presets/resolve?baseUrl=http://x&model=m',
    });
    expect((resolved.data as Record<string, any>).preset).toEqual({ id: 'p1' });
    expect(supervisor.call).toHaveBeenCalledWith('presets.resolve', {
      baseUrl: 'http://x',
      model: 'm',
    });

    const catalog = await router.handle({ method: 'GET', path: '/api/v2/llm/catalog' });
    expect((catalog.data as Record<string, any>).providers).toEqual([{ id: 'openai' }]);

    supervisor.call.mockRejectedValueOnce(new Error('boom'));
    const failed = await router.handle({ method: 'GET', path: '/api/v2/llm/catalog' });
    expect(failed.status).toBe(502);
    expect((failed.data as Record<string, any>).providers).toEqual([]);
  });

  it('GET /api/v2/llm/models：放行草稿 baseUrl 出网后调 listModels；失败 502 带 offline', async () => {
    const supervisor = makeSupervisor();
    h.pendingSupervisor = supervisor;
    const router = makeRouter();

    const res = await router.handle({
      method: 'GET',
      path: '/api/v2/llm/models?baseUrl=http://draft:1/v1&apiKey=draft-key&refresh=1',
    });
    expect(res.status).toBe(200);
    // 设置页草稿地址不在 boot 白名单上，路由显式放行
    expect(h.allowEgressForBaseUrl).toHaveBeenCalled();
    expect(supervisor.listModels).toHaveBeenCalledWith(
      expect.objectContaining({ apiKey: 'draft-key', refresh: true }),
    );

    supervisor.listModels.mockRejectedValueOnce(new Error('网关超时'));
    const failed = await router.handle({ method: 'GET', path: '/api/v2/llm/models' });
    expect(failed.status).toBe(502);
    expect(failed.data).toMatchObject({ models: [], catalogStatus: 'offline' });

    h.pendingSupervisor = null;
    const offline = await router.handle({ method: 'GET', path: '/api/v2/llm/models' });
    expect(offline.status).toBe(503);
    expect(offline.data).toMatchObject({ catalogStatus: 'offline' });
  });

  it('POST /api/v2/llm/diagnose：缺 baseUrl 400；成功 200；诊断抛错 502', async () => {
    const router = makeRouter();
    h.llmSettings.baseUrl = '';
    const noBase = await router.handle({ method: 'POST', path: '/api/v2/llm/diagnose', body: {} });
    expect(noBase.status).toBe(400);
    expect(noBase.data).toEqual({ error: 'baseUrl is required' });

    const ok = await router.handle({
      method: 'POST',
      path: '/api/v2/llm/diagnose',
      body: { baseUrl: 'http://x/v1', model: 'm' },
    });
    expect(ok.status).toBe(200);
    expect(h.diagnoseLlmConnection).toHaveBeenCalledWith(
      expect.objectContaining({ baseUrl: 'http://x/v1', model: 'm' }),
    );

    h.diagnoseLlmConnection.mockRejectedValueOnce(new Error('DNS 失败'));
    const failed = await router.handle({
      method: 'POST',
      path: '/api/v2/llm/diagnose',
      body: { baseUrl: 'http://x/v1' },
    });
    expect(failed.status).toBe(502);
    expect((failed.data as Record<string, any>).error).toContain('diagnose 失败');
  });

  it('GET /api/v2/sidecar/sandbox-posture：未就绪 503；收容失败仍返回 lastSpawnRefusal', async () => {
    const router = makeRouter();
    const offline = await router.handle({ method: 'GET', path: '/api/v2/sidecar/sandbox-posture' });
    expect(offline.status).toBe(503);
    expect(offline.data).toMatchObject({ posture: null });

    (SidecarSupervisor as unknown as { lastSpawnRefusal: unknown }).lastSpawnRefusal = {
      sandboxed: false,
      reason: 'seatbelt 不可用',
    };
    const refused = await router.handle({ method: 'GET', path: '/api/v2/sidecar/sandbox-posture' });
    expect(refused.status).toBe(200);
    expect(refused.data).toMatchObject({ refused: true, posture: { sandboxed: false } });

    h.pendingSupervisor = makeSupervisor();
    const ok = await router.handle({ method: 'GET', path: '/api/v2/sidecar/sandbox-posture' });
    expect(ok.status).toBe(200);
    expect((ok.data as Record<string, any>).posture).toEqual({
      sandboxed: true,
      backend: 'seatbelt',
    });
  });

  it('GET/POST /api/v2/local-settings/llm：保存后同步执行超时常量并放行新 baseUrl', async () => {
    h.store.state.llmSettings = { provider: 'ollama', model: 'llama3.1:8b' };
    const router = makeRouter();

    const got = await router.handle({ method: 'GET', path: '/api/v2/local-settings/llm' });
    expect(got.data).toEqual({ provider: 'ollama', model: 'llama3.1:8b' });

    const saved = await router.handle({
      method: 'POST',
      path: '/api/v2/local-settings/llm',
      body: {
        provider: 'openai-compat',
        model: 'gpt-x',
        baseUrl: 'http://new-gateway/v1',
        execTimeoutSeconds: 30,
        maxTotalTokens: '8192', // 字符串数字也被解析
      },
    });
    expect(saved.status).toBe(200);
    expect(h.setSettings).toHaveBeenCalledWith(
      expect.objectContaining({ model: 'gpt-x', maxTotalTokens: 8192, execTimeoutSeconds: 30 }),
    );
    expect(h.setDefaultExecTimeoutMs).toHaveBeenCalledWith(30_000);
    expect(h.allowEgressForBaseUrl).toHaveBeenCalledWith('http://new-gateway/v1');
  });

  it('GET/POST /api/v2/local-settings/telemetry：privacyMode 只认 full，其余归一 metadata', async () => {
    const router = makeRouter();
    const saved = await router.handle({
      method: 'POST',
      path: '/api/v2/local-settings/telemetry',
      body: { endpoint: 'http://otlp:4318', privacyMode: 'bogus' },
    });
    expect((saved.data as Record<string, any>).privacyMode).toBe('metadata');

    const full = await router.handle({
      method: 'POST',
      path: '/api/v2/local-settings/telemetry',
      body: { privacyMode: 'full' },
    });
    expect((full.data as Record<string, any>).privacyMode).toBe('full');

    const got = await router.handle({ method: 'GET', path: '/api/v2/local-settings/telemetry' });
    expect((got.data as Record<string, any>).privacyMode).toBe('full');
  });

  it('GET/POST /api/v2/local-settings/web-search：provider 只认 ddg，其余归一 tavily', async () => {
    const router = makeRouter();
    const ddg = await router.handle({
      method: 'POST',
      path: '/api/v2/local-settings/web-search',
      body: { provider: 'ddg' },
    });
    expect((ddg.data as Record<string, any>).provider).toBe('ddg');

    const other = await router.handle({
      method: 'POST',
      path: '/api/v2/local-settings/web-search',
      body: { provider: 'bing' },
    });
    expect((other.data as Record<string, any>).provider).toBe('tavily');
  });
});

// ---------------------------------------------------------------------------
// Insights / 用量 / traces
// ---------------------------------------------------------------------------

describe('Insights 与用量路由', () => {
  it('POST /api/v2/insights/events：eventName 非法 400；合法落一条事件', async () => {
    const router = makeRouter();
    const bad = await router.handle({
      method: 'POST',
      path: '/api/v2/insights/events',
      body: { eventName: '1bad-name!' },
    });
    expect(bad.status).toBe(400);
    expect(bad.data).toEqual({ detail: 'invalid eventName' });

    const ok = await router.handle({
      method: 'POST',
      path: '/api/v2/insights/events',
      body: { eventName: 'skill_used', properties: { skill: 'csv' } },
    });
    expect(ok.status).toBe(200);
    expect(h.recordInsightEvent).toHaveBeenCalledWith('skill_used', { skill: 'csv' });
  });

  it('GET/POST /api/v2/local-settings/insights：profile 合并且触发 recordInsightProfile', async () => {
    const router = makeRouter();
    const got = await router.handle({ method: 'GET', path: '/api/v2/local-settings/insights' });
    expect((got.data as Record<string, any>).stats).toEqual({
      events: 0,
      turns: 0,
      profile: 0,
      pending: 0,
    });

    const saved = await router.handle({
      method: 'POST',
      path: '/api/v2/local-settings/insights',
      body: { shareBehavior: true, displayName: '王', markPrompted: true },
    });
    const data = saved.data as Record<string, any>;
    expect(data.shareBehavior).toBe(true);
    expect(data.profile.displayName).toBe('王');
    expect(data.promptedAt).toEqual(expect.any(String));
    expect(h.recordInsightProfile).toHaveBeenCalledWith(
      expect.objectContaining({ displayName: '王' }),
    );
    expect(h.flushInsightsOutbox).toHaveBeenCalled();
  });

  it('POST /api/v2/local-settings/insights：无 profile 字段时不记录 profile', async () => {
    const res = await makeRouter().handle({
      method: 'POST',
      path: '/api/v2/local-settings/insights',
      body: { shareConversation: false },
    });
    expect(res.status).toBe(200);
    expect(h.recordInsightProfile).not.toHaveBeenCalled();
  });

  it('GET /api/v2/insights/export 与 POST /upload-local（成功 200 / 失败 502）', async () => {
    const router = makeRouter();
    const exported = await router.handle({ method: 'GET', path: '/api/v2/insights/export' });
    expect(exported.data).toEqual({ exported: true });

    const ok = await router.handle({ method: 'POST', path: '/api/v2/insights/upload-local' });
    expect(ok.status).toBe(200);
    expect((ok.data as Record<string, any>).detail).toBe('uploaded');

    h.uploadInsightsBundle.mockResolvedValueOnce(false);
    const failed = await router.handle({ method: 'POST', path: '/api/v2/insights/upload-local' });
    expect(failed.status).toBe(502);
    expect((failed.data as Record<string, any>).detail).toBe('upload_failed_kept_local');
  });

  it('GET /api/v2/usage/summary：days 参数透传，非法值回落 30', async () => {
    const router = makeRouter();
    const res = await router.handle({ method: 'GET', path: '/api/v2/usage/summary?days=7' });
    expect((res.data as Record<string, any>).days).toBe(7);

    const fallback = await router.handle({ method: 'GET', path: '/api/v2/usage/summary?days=abc' });
    expect((fallback.data as Record<string, any>).days).toBe(30);
  });

  it('GET /api/v2/local/traces：缺 chatId 400；payload 经 safeJson 解析', async () => {
    const router = makeRouter();
    const noChat = await router.handle({ method: 'GET', path: '/api/v2/local/traces' });
    expect(noChat.status).toBe(400);

    h.store.saveTrace({
      id: 'trace-1',
      chatId: 'c1',
      messageId: 'm1',
      startedAtMs: 1,
      durationMs: 2,
      status: 'completed',
      payload: { coreloop: true },
    });
    const res = await router.handle({ method: 'GET', path: '/api/v2/local/traces?chatId=c1' });
    const traces = (res.data as Record<string, any>).traces as Array<Record<string, unknown>>;
    expect(traces).toHaveLength(1);
    expect(traces[0].payload).toEqual({ coreloop: true });

    const one = await router.handle({ method: 'GET', path: '/api/v2/local/traces/trace-1' });
    expect(one.status).toBe(200);
    const missing = await router.handle({ method: 'GET', path: '/api/v2/local/traces/nope' });
    expect(missing.status).toBe(404);
  });
});

// ---------------------------------------------------------------------------
// 固定形状的占位路由 + notifications + fallback
// ---------------------------------------------------------------------------

describe('占位与 fallback 路由', () => {
  it('suggest-replies / feedback / local-exec-result / variants / select-variant 返回固定形状', async () => {
    const router = makeRouter();
    const suggest = await router.handle({
      method: 'POST',
      path: '/api/v2/chats/c1/suggest-replies',
    });
    expect(suggest.data).toEqual({ suggestions: [] });

    const feedback = await router.handle({
      method: 'POST',
      path: '/api/v2/chats/c1/messages/feedback',
    });
    expect(feedback.data).toMatchObject({ success: true, feedback: 'like' });

    const execResult = await router.handle({
      method: 'POST',
      path: '/api/v2/chats/c1/messages/m1/local-exec-result',
    });
    expect(execResult.data).toEqual({ success: true, message: 'saved' });

    const variants = await router.handle({
      method: 'GET',
      path: '/api/v2/chats/c1/messages/m1/variants',
    });
    expect(variants.data).toMatchObject({ variantsLocked: true, variantsCount: 1, variants: [] });

    const select = await router.handle({
      method: 'POST',
      path: '/api/v2/chats/c1/messages/m1/select-variant',
    });
    expect(select.data).toMatchObject({ success: true, variantsLocked: true });
  });

  it('notifications 系列返回明确的空形状（含 unreadAutomations 字段）', async () => {
    const router = makeRouter();
    const bootstrap = await router.handle({
      method: 'GET',
      path: '/api/v2/notifications/bootstrap',
    });
    expect(bootstrap.data).toEqual({ unreadCount: 0, unreadAutomations: [] });

    const list = await router.handle({ method: 'GET', path: '/api/v2/notifications/list' });
    expect(list.data).toMatchObject({ items: [], unreadAutomations: [], total: 0 });

    const unread = await router.handle({
      method: 'GET',
      path: '/api/v2/notifications/unread-automations',
    });
    expect(unread.data).toEqual({ unreadAutomations: [], items: [] });
  });

  it('场景包路由命中时交给包 handler；未命中走 fallback', async () => {
    const handler = vi.fn(() => ({ status: 200, data: { from: 'pack' } }));
    h.packRoute = { params: { id: 'p1' }, route: { handler } };
    const router = makeRouter();
    const res = await router.handle({ method: 'GET', path: '/api/v2/docpack-items/p1' });
    expect(res.data).toEqual({ from: 'pack' });
    expect(handler).toHaveBeenCalledWith(
      expect.objectContaining({ params: { id: 'p1' } }),
    );
  });

  it('fallback：GET 列表型端点返回空集合形状；其他 GET 返回 {}；非 GET 返回 noop', async () => {
    const router = makeRouter();
    const listLike = await router.handle({ method: 'GET', path: '/api/v2/unknowns' });
    expect(listLike.status).toBe(200);
    expect(listLike.data).toMatchObject({ items: [], total: 0, unreadAutomations: [] });

    const single = await router.handle({ method: 'GET', path: '/api/v2/unknown' });
    expect(single.data).toEqual({});

    const post = await router.handle({ method: 'POST', path: '/api/v2/unknown' });
    expect(post.data).toEqual({ success: true, message: 'noop (local fallback)' });
  });
});

// ---------------------------------------------------------------------------
// open-path 路由（回合产物列表的「点击打开」）
// ---------------------------------------------------------------------------

describe('open-path 路由', () => {
  it('绝对路径 → 调 shellOpenPath 并返回 success:true', async () => {
    const router = makeRouter();
    const res = await router.handle({
      method: 'POST',
      path: '/api/v2/local/open-path',
      body: { path: '/tmp/自我介绍.pptx' },
    });
    expect(res.status).toBe(200);
    expect(res.data).toEqual({ success: true });
    expect(h.shellOpenPath).toHaveBeenCalledWith('/tmp/自我介绍.pptx');
  });

  it('空路径 / 相对路径 → 400，不触发打开', async () => {
    const router = makeRouter();
    const relative = await router.handle({
      method: 'POST',
      path: '/api/v2/local/open-path',
      body: { path: 'reports/a.txt' },
    });
    expect(relative.status).toBe(400);

    const missing = await router.handle({
      method: 'POST',
      path: '/api/v2/local/open-path',
      body: {},
    });
    expect(missing.status).toBe(400);
    expect(h.shellOpenPath).not.toHaveBeenCalled();
  });

  it('宿主打开失败 → 200 + success:false 带错误消息', async () => {
    h.shellOpenPath.mockResolvedValue('ENOENT: no such file or directory');
    const router = makeRouter();
    const res = await router.handle({
      method: 'POST',
      path: '/api/v2/local/open-path',
      body: { path: '/tmp/gone.txt' },
    });
    expect(res.status).toBe(200);
    expect(res.data).toMatchObject({ success: false });
    expect((res.data as { error: string }).error).toContain('ENOENT');
  });
});

describe('resolve-paths 路由', () => {
  it('绑定项目的会话：相对路径按项目根落地，不存在的候选不回', async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'resolve-paths-'));
    try {
      fs.writeFileSync(path.join(dir, '自我介绍.pptx'), 'ppt');
      const registry = makeProjectRegistry([
        { id: 'proj-1', name: '演示项目', folderPath: dir, trusted: true },
      ]);
      const router = makeRouter({ toolRouter: makeToolRouter({ projectRegistry: registry }) });
      const chat = h.store.createChat('新对话', 'agent-a', 'proj-1');

      const res = await router.handle({
        method: 'POST',
        path: '/api/v2/local/resolve-paths',
        body: { chatId: chat.id, candidates: ['./自我介绍.pptx', './不存在.pptx'] },
      });
      expect(res.status).toBe(200);
      expect(res.data).toEqual({
        resolved: [
          {
            candidate: './自我介绍.pptx',
            path: path.join(dir, '自我介绍.pptx'),
            isDirectory: false,
          },
        ],
      });
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it('未绑定项目的会话：相对路径按 home 落地', async () => {
    const name = `resolve-paths-home-${Date.now()}.txt`;
    const target = path.join(os.homedir(), name);
    fs.writeFileSync(target, 'x');
    try {
      const router = makeRouter();
      const chat = h.store.createChat('新对话', 'agent-a');
      const res = await router.handle({
        method: 'POST',
        path: '/api/v2/local/resolve-paths',
        body: { chatId: chat.id, candidates: [`./${name}`] },
      });
      expect(res.data).toEqual({
        resolved: [{ candidate: `./${name}`, path: target, isDirectory: false }],
      });
    } finally {
      fs.rmSync(target, { force: true });
    }
  });

  it('候选为空 / 非字符串时直接回空列表', async () => {
    const router = makeRouter();
    const empty = await router.handle({
      method: 'POST',
      path: '/api/v2/local/resolve-paths',
      body: { candidates: [] },
    });
    expect(empty.data).toEqual({ resolved: [] });

    const malformed = await router.handle({
      method: 'POST',
      path: '/api/v2/local/resolve-paths',
      body: { candidates: [1, null, {}] },
    });
    expect(malformed.data).toEqual({ resolved: [] });
  });
});
