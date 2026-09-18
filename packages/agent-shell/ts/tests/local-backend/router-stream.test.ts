/**
 * LocalBackendRouter.handleStream() 流式路由测试。
 *
 * 覆盖 /api/v2/chats/:id/{send,run,agent} 与 /messages/:mid/regenerate 的
 * SSE 行为：路由匹配、会话自动补建、regenerate 截断/fork、resume 续跑、
 * sidecar 缺失 503、SSE chunk 序列（content/executed_actions/message_id/
 * [DONE]/error）、turn_active 中断标记生命周期、usage/trace 落库、
 * 后台标题生成、plan 模式工具过滤、工具策略、@提及人设、项目围栏提示、
 * "/技能" 显式触发与图片附件注入。
 *
 * streamCoreLoopTurn（sidecar 流边界）按用例脚本回放；localStore 为内存
 * 假实现；不起 HTTP 服务器、不访问网络。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

import {
  h,
  makeBroadcast,
  makeEmitCapture,
  makeProjectRegistry,
  makeSupervisor,
  makeToolRouter,
  resetRouterTestkit,
} from './router-testkit.js';
import { LocalBackendRouter } from '../../src/local-backend/router.js';
import type { ToolRouter } from '../../src/tool-router.js';

function makeRouter(options: {
  toolRouter?: Record<string, unknown>;
  broadcast?: ReturnType<typeof makeBroadcast>['broadcast'];
} = {}): LocalBackendRouter {
  const toolRouter = (options.toolRouter ?? makeToolRouter()) as unknown as ToolRouter;
  return new LocalBackendRouter(toolRouter, { broadcast: options.broadcast });
}

/** 挂上一个 sidecar，并让流桩按 script 回放后返回给定终态。 */
function installStream(
  script: (options: Record<string, any>) => void | Promise<void>,
  outcome: Record<string, unknown> = { status: 'completed' },
) {
  const supervisor = makeSupervisor();
  h.supervisor = supervisor;
  const seen: Array<Record<string, any>> = [];
  h.streamImpl = async (options) => {
    seen.push(options);
    await script(options);
    return outcome;
  };
  return { supervisor, seen };
}

/** 建一个带标题「新对话」的空会话（触发标题生成的默认条件）。 */
function seedChat(title = '新对话') {
  return h.store.createChat(title, 'agent-a', null);
}

beforeEach(() => {
  resetRouterTestkit();
});

afterEach(() => {
  delete process.env.STEERABLE_AUTO_CONTINUE;
  delete process.env.STEERABLE_APPROVAL;
});

// ---------------------------------------------------------------------------
// 路由匹配与前置校验
// ---------------------------------------------------------------------------

describe('流式路由前置校验', () => {
  it('非流式路径返回 404 并 emit error 事件', async () => {
    const cap = makeEmitCapture();
    const res = await makeRouter().handleStream(
      { method: 'POST', path: '/api/v2/chats/c1' },
      cap.emit,
    );
    expect(res.status).toBe(404);
    expect(cap.events()).toHaveLength(1);
    expect(cap.events()[0]).toMatchObject({ event: 'error' });
    expect((cap.events()[0].data as { message: string }).message).toContain('Stream route not found');
  });

  it('流式路径上的非 POST 方法返回 404', async () => {
    const cap = makeEmitCapture();
    const res = await makeRouter().handleStream(
      { method: 'GET', path: '/api/v2/chats/c1/send' },
      cap.emit,
    );
    expect(res.status).toBe(404);
    expect(cap.events()[0].event).toBe('error');
  });

  it('chat 不存在且无有效消息 → 404 chat not found（不凭空补建）', async () => {
    const cap = makeEmitCapture();
    const res = await makeRouter().handleStream(
      { method: 'POST', path: '/api/v2/chats/ghost/send', body: {} },
      cap.emit,
    );
    expect(res.status).toBe(404);
    expect((cap.events()[0].data as { message: string }).message).toBe('chat not found');
    expect(h.store.getChat('ghost')).toBeNull();
  });

  it('chat 不存在但带了用户消息 → 按 URL id 现场补建并广播 chat-created', async () => {
    const { broadcast, calls } = makeBroadcast();
    installStream(() => {});
    const cap = makeEmitCapture();
    const res = await makeRouter({ broadcast }).handleStream(
      { method: 'POST', path: '/api/v2/chats/revived/send', body: { message: '复活这条会话' } },
      cap.emit,
    );
    expect(res.status).toBe(200);
    expect(h.store.getChat('revived')).not.toBeNull();
    expect(calls).toContainEqual({
      event: 'chat-created',
      payload: { chatId: 'revived', agentId: 'local-assistant' },
    });
  });

  it('resume=true 且 chat 不存在 → 不补建，404', async () => {
    const cap = makeEmitCapture();
    const res = await makeRouter().handleStream(
      {
        method: 'POST',
        path: '/api/v2/chats/ghost/send',
        body: { message: 'x', resume: true },
      },
      cap.emit,
    );
    expect(res.status).toBe(404);
    expect(h.store.getChat('ghost')).toBeNull();
  });

  it('message 为空白 → 400 message is required', async () => {
    seedChat();
    const chat = h.store.listChats().chats[0];
    const cap = makeEmitCapture();
    const res = await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '   ' } },
      cap.emit,
    );
    expect(res.status).toBe(400);
    expect((cap.events()[0].data as { message: string }).message).toBe('message is required');
  });

  it('sidecar 未运行 → 503 + error 事件；turn_active 不残留', async () => {
    const chat = seedChat();
    const cap = makeEmitCapture();
    const res = await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '你好' } },
      cap.emit,
    );
    expect(res.status).toBe(503);
    const errors = cap.events().filter((c) => c.event === 'error');
    expect(errors).toHaveLength(1);
    expect((errors[0].data as { message: string }).message).toContain('sidecar is not running');
    // 503 在 setTurnActive 之前 return，record 里没有这一轮，无可续跑标记
    expect(h.store.getTurnActive(chat.id)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 正常流式回合：SSE 序列与落库
// ---------------------------------------------------------------------------

describe('流式回合 SSE 序列', () => {
  it('文本流：user_message → content 片段 → message_id → [DONE]，双消息落库', async () => {
    const chat = seedChat();
    const { seen } = installStream((opts) => {
      opts.onText('你好');
      opts.onText('，世界');
    });
    const cap = makeEmitCapture();
    const res = await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '打个招呼' } },
      cap.emit,
    );
    expect(res.status).toBe(200);

    // SSE 序列：首帧是 user_message 同步，随后两个 content 片段，message_id，[DONE]
    const events = cap.events();
    expect(events[0].data).toMatchObject({ type: 'user_message' });
    expect(cap.textDeltas()).toEqual(['你好', '，世界']);
    const messageId = cap.byType('message_id');
    expect(messageId).toHaveLength(1);
    expect(events.at(-1)?.data).toBe('[DONE]');

    // 落库：user + assistant 各一条；assistant metadata 带 completionStatus
    const messages = h.store.listMessages(chat.id, 10);
    expect(messages.map((m) => m.role)).toEqual(['assistant', 'user']);
    expect(messages[0].content).toBe('你好，世界');
    const metadata = JSON.parse(messages[0].messageMetadata!);
    expect(metadata).toMatchObject({ completionStatus: 'completed', mode: 'agent', coreloop: true });

    // 发给 sidecar 的消息：历史为空 + 当前用户消息
    expect(seen[0].messages).toEqual([{ role: 'user', content: '打个招呼' }]);
    expect(seen[0].chatId).toBe(chat.id);
  });

  it('工具事件：onToolStart 先登记、onToolAction 回填结果，executed_actions 逐帧广播', async () => {
    const chat = seedChat();
    installStream((opts) => {
      opts.onToolStart({ id: 'call-1', tool: 'local_read_file', arguments: { path: '/a' } });
      opts.onToolAction({
        id: 'call-1',
        tool: 'local_read_file',
        arguments: { path: '/a' },
        result: { success: true, content: '文件内容' },
        success: true,
        durationMs: 7,
      });
      opts.onText('读完了');
    });
    const cap = makeEmitCapture();
    await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '读文件' } },
      cap.emit,
    );

    const actionEvents = cap.byType('executed_actions');
    expect(actionEvents.length).toBeGreaterThanOrEqual(2);
    // 第一帧：只有调用登记，还没有 result
    const first = (actionEvents[0].data as { actions: Array<Record<string, unknown>> }).actions;
    expect(first).toHaveLength(1);
    expect(first[0]).toMatchObject({ id: 'call-1', tool: 'local_read_file' });
    expect(first[0].result).toBeUndefined();
    // 第二帧：结果回填到同一行
    const second = (actionEvents[1].data as { actions: Array<Record<string, unknown>> }).actions;
    expect(second[0]).toMatchObject({ success: true, durationMs: 7 });

    // 落库 metadata 携带完整 executedActions 与 timeline
    const assistant = h.store.listMessages(chat.id, 10)[0];
    const metadata = JSON.parse(assistant.messageMetadata!);
    expect(metadata.executedActions[0]).toMatchObject({ tool: 'local_read_file', success: true });
    expect(metadata.timeline.some((b: { type: string }) => b.type === 'tools')).toBe(true);
  });

  it('reasoning 片段以 type:reasoning 转发', async () => {
    const chat = seedChat();
    installStream((opts) => {
      opts.onReasoning('思考一下');
      opts.onText('结论');
    });
    const cap = makeEmitCapture();
    await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '想想' } },
      cap.emit,
    );
    expect(cap.byType('reasoning')[0].data).toMatchObject({ content: '思考一下' });
  });

  it('budget_exhausted notice 与子代理事件分别转成 SSE', async () => {
    const chat = seedChat();
    installStream((opts) => {
      opts.onNotice('budget_exhausted', { budget: 'tokens' });
      opts.onChildEvent({ childId: 'sub-1', status: 'spawned' });
      opts.onText('done');
    });
    const cap = makeEmitCapture();
    await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '干活' } },
      cap.emit,
    );
    expect(cap.byType('budget_exhausted')[0].data).toEqual({
      type: 'budget_exhausted',
      budget: { kind: 'tokens' },
      message: 'budget_exhausted: tokens',
    });
    expect(cap.byType('orchestration_child')[0].data).toMatchObject({
      childId: 'sub-1',
      status: 'spawned',
    });
  });

  it('turn_active 标记：流式期间存在、回合落库后清除', async () => {
    const chat = seedChat();
    let markerDuringStream: unknown;
    installStream(() => {
      markerDuringStream = h.store.getTurnActive(chat.id);
    });
    await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: 'hi' } },
      makeEmitCapture().emit,
    );
    expect(markerDuringStream).not.toBeNull();
    expect(h.store.getTurnActive(chat.id)).toBeNull();
  });

  it('usage 与 trace：recordUsageEvent 落一条；trace.fetch 取回后 saveTrace', async () => {
    const chat = seedChat();
    const { supervisor } = installStream(() => {}, {
      status: 'completed',
      traceId: 'trace-abc',
      usage: { promptTokens: 10, completionTokens: 5, totalTokens: 15, costUsd: 0.001 },
    });
    await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: 'hi' } },
      makeEmitCapture().emit,
    );

    expect(h.store.state.usageEvents).toHaveLength(1);
    expect(h.store.state.usageEvents[0]).toMatchObject({
      chatId: chat.id,
      kind: 'chat',
      totalTokens: 15,
      costUsd: 0.001,
    });

    expect(supervisor.call).toHaveBeenCalledWith('trace.fetch', { traceId: 'trace-abc' }, { timeoutMs: 5000 });
    const trace = h.store.getTrace('trace-abc');
    expect(trace).not.toBeNull();
    expect(trace!.status).toBe('completed');
    expect(JSON.parse(trace!.payload)).toMatchObject({ coreloop: true, trace: { durationMs: 42 } });
    // trace 挂到本轮那条 assistant 消息上
    const assistant = h.store.listMessages(chat.id, 10)[0];
    expect(trace!.messageId).toBe(assistant.id);
  });

  it('配置 OTLP endpoint 后导出 trace（metadata 档位）', async () => {
    const chat = seedChat();
    h.store.state.telemetry = {
      endpoint: 'http://otlp:4318',
      privacyMode: 'metadata',
      serviceName: 'svc',
    };
    const { supervisor } = installStream(() => {}, { status: 'completed', traceId: 'trace-t' });
    await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: 'hi' } },
      makeEmitCapture().emit,
    );
    await vi.waitFor(() => {
      expect(supervisor.call).toHaveBeenCalledWith(
        'trace.export',
        expect.objectContaining({ traceId: 'trace-t', endpoint: 'http://otlp:4318' }),
        { timeoutMs: 10_000 },
      );
    });
  });
});

// ---------------------------------------------------------------------------
// 失败与取消路径
// ---------------------------------------------------------------------------

describe('流式回合失败与取消', () => {
  it('status=failed 带 reason：补发 error 事件，metadata 记录失败', async () => {
    const chat = seedChat();
    installStream(() => {}, { status: 'failed', reason: 'HTTP 401 Unauthorized' });
    const cap = makeEmitCapture();
    const res = await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: 'hi' } },
      cap.emit,
    );
    expect(res.status).toBe(200); // 流正常关闭，错误经 SSE 透出
    const errors = cap.events().filter((c) => c.event === 'error');
    expect(errors).toHaveLength(1);
    expect((errors[0].data as { message: string }).message).toContain('HTTP 401');

    const assistant = h.store.listMessages(chat.id, 10)[0];
    expect(JSON.parse(assistant.messageMetadata!)).toMatchObject({
      completionStatus: 'failed',
      completionReason: 'HTTP 401 Unauthorized',
    });
  });

  it('streamCoreLoopTurn 抛 AbortError → cancelled，不发 error 事件', async () => {
    const chat = seedChat();
    installStream(() => {
      throw new DOMException('aborted', 'AbortError');
    });
    const cap = makeEmitCapture();
    await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: 'hi' } },
      cap.emit,
    );
    expect(cap.events().filter((c) => c.event === 'error')).toHaveLength(0);
    const assistant = h.store.listMessages(chat.id, 10)[0];
    expect(JSON.parse(assistant.messageMetadata!)).toMatchObject({
      completionStatus: 'cancelled',
      completionReason: 'aborted_by_user',
    });
  });

  it('streamCoreLoopTurn 抛普通错误 → failed + error 事件；err.traceId 被收进 passTraceIds', async () => {
    const chat = seedChat();
    const boom = Object.assign(new Error('连接重置'), { traceId: 'trace-fail' });
    const { supervisor } = installStream(() => {
      throw boom;
    });
    const cap = makeEmitCapture();
    await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: 'hi' } },
      cap.emit,
    );
    const errors = cap.events().filter((c) => c.event === 'error');
    expect((errors[0].data as { message: string }).message).toBe('连接重置');

    const assistant = h.store.listMessages(chat.id, 10)[0];
    const metadata = JSON.parse(assistant.messageMetadata!);
    expect(metadata).toMatchObject({ completionStatus: 'failed', traceId: 'trace-fail' });
    // 失败趟的部分 trace 也被持久化（dogfood 信号）
    expect(supervisor.call).toHaveBeenCalledWith('trace.fetch', { traceId: 'trace-fail' }, { timeoutMs: 5000 });
    expect(h.store.getTrace('trace-fail')?.status).toBe('failed');
  });

  it('turn_active 标记在失败落库后同样清除', async () => {
    const chat = seedChat();
    installStream(() => {
      throw new Error('boom');
    });
    await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: 'hi' } },
      makeEmitCapture().emit,
    );
    expect(h.store.getTurnActive(chat.id)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 自动续跑（budget_exhausted → resume 通道）
// ---------------------------------------------------------------------------

describe('自动续跑', () => {
  it('budget_exhausted 且有进展 → 第二趟走 resume:true + 空 messages', async () => {
    const chat = seedChat();
    const supervisor = makeSupervisor();
    h.supervisor = supervisor;
    const seen: Array<Record<string, any>> = [];
    let pass = 0;
    h.streamImpl = async (options) => {
      seen.push(options);
      pass += 1;
      if (pass === 1) {
        options.onText('第一段');
        return { status: 'budget_exhausted', traceId: 'trace-p1' };
      }
      options.onText('第二段');
      return { status: 'completed', traceId: 'trace-p2' };
    };

    const cap = makeEmitCapture();
    await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '长任务' } },
      cap.emit,
    );

    expect(seen).toHaveLength(2);
    expect(seen[0].resume).toBe(false);
    expect(seen[1].resume).toBe(true);
    expect(seen[1].messages).toEqual([]);

    // 两趟的 trace 都挂到同一条 assistant 消息；中间趟标记 budget_exhausted
    expect(h.store.getTrace('trace-p1')?.status).toBe('budget_exhausted');
    expect(h.store.getTrace('trace-p2')?.status).toBe('completed');
    const assistant = h.store.listMessages(chat.id, 10)[0];
    const metadata = JSON.parse(assistant.messageMetadata!);
    expect(metadata.autoContinuations).toBe(1);
    expect(assistant.content).toBe('第一段第二段');
  });

  it('STEERABLE_AUTO_CONTINUE=0 时撞墙即停，不续跑', async () => {
    process.env.STEERABLE_AUTO_CONTINUE = '0';
    const chat = seedChat();
    const { seen } = installStream(
      (opts) => opts.onText('半段'),
      { status: 'budget_exhausted' },
    );
    await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '长任务' } },
      makeEmitCapture().emit,
    );
    expect(seen).toHaveLength(1);
  });

  it('整趟零进展（无文本无工具）→ 不续跑（打转护栏）', async () => {
    const chat = seedChat();
    const { seen } = installStream(() => {}, { status: 'budget_exhausted' });
    await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '长任务' } },
      makeEmitCapture().emit,
    );
    expect(seen).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// regenerate：截断重跑
// ---------------------------------------------------------------------------

describe('regenerate', () => {
  function seedConversation() {
    const chat = seedChat('已有标题');
    h.store.addMessage(chat.id, 'user', '第一个问题');
    const target = h.store.addMessage(chat.id, 'assistant', '第一个回答');
    h.store.addMessage(chat.id, 'user', '第二个问题');
    h.store.addMessage(chat.id, 'assistant', '第二个回答');
    return { chat, target };
  }

  it('目标消息不存在或不是 assistant → 404', async () => {
    const { chat } = seedConversation();
    installStream(() => {});
    const cap = makeEmitCapture();
    const res = await makeRouter().handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/messages/msg-not-exist/regenerate`,
        body: {},
      },
      cap.emit,
    );
    expect(res.status).toBe(404);
    expect((cap.events()[0].data as { message: string }).message).toContain('regenerate target');
  });

  it('fork 失败（未保留旧回复）→ 409 拒绝截断', async () => {
    const { chat, target } = seedConversation();
    const supervisor = makeSupervisor({
      forkSession: vi.fn(async () => ({ ok: false, reason: 'record 损坏' })),
    });
    h.supervisor = supervisor;
    h.streamImpl = async () => ({ status: 'completed' });

    const cap = makeEmitCapture();
    const res = await makeRouter().handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/messages/${target.id}/regenerate`,
        body: {},
      },
      cap.emit,
    );
    expect(res.status).toBe(409);
    expect((cap.events()[0].data as { message: string }).message).toContain('未能保留为分支');
    // 历史未被截断
    expect(h.store.listMessages(chat.id, 10)).toHaveLength(4);
  });

  it('fork 成功 → 截断目标及其后消息、切换 recordId、不重复插入用户消息', async () => {
    const { chat, target } = seedConversation();
    const { supervisor, seen } = installStream((opts) => opts.onText('重新生成的回答'));
    h.store.setChatRecordId(chat.id, 'rec-old');

    const cap = makeEmitCapture();
    const res = await makeRouter().handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/messages/${target.id}/regenerate`,
        body: {},
      },
      cap.emit,
    );
    expect(res.status).toBe(200);

    // fork 在截断之前调用，fork 点是触发用户消息的序号
    expect(supervisor.forkSession).toHaveBeenCalledWith(
      expect.objectContaining({ recordId: 'rec-old', beforeUserIndex: 0 }),
    );
    expect(h.store.getChatRecordId(chat.id)).toBe('fork-rec-1');

    // 目标 assistant + 其后两条被截断，只剩触发它的用户消息
    const remaining = h.store.listMessages(chat.id, 10);
    expect(remaining.map((m) => m.content)).toContain('第一个问题');
    expect(remaining.map((m) => m.content)).not.toContain('第二个问题');

    // 没有新的 user_message 帧（不像 send 会同步插入用户消息）
    expect(cap.byType('user_message')).toHaveLength(0);

    // 发给 sidecar 的历史以触发用户消息结尾，且只出现一次
    const contents = seen[0].messages.map((m: { content: string }) => m.content);
    expect(contents.filter((c: string) => c === '第一个问题')).toHaveLength(1);
    expect(contents.at(-1)).toBe('第一个问题');
  });

  it('sidecar 关闭时直接 503，历史一字不动（截断不得先于回合可用性检查）', async () => {
    const { chat, target } = seedConversation();
    installStream((opts) => opts.onText('新回答'));
    h.supervisor = null; // installStream 挂的 supervisor 摘掉
    const cap = makeEmitCapture();
    const res = await makeRouter().handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/messages/${target.id}/regenerate`,
        body: {},
      },
      cap.emit,
    );
    // rerun 回合只能跑在 sidecar 上；旧行为先截断再 503，旧回复被删且
    // 没有新回复、record 也不存在——非破坏性承诺破窗。现在门在前面。
    expect(res.status).toBe(503);
    expect(h.store.listMessages(chat.id, 10)).toHaveLength(4);
  });
});

// ---------------------------------------------------------------------------
// resume：续跑被中断的 turn（W7-1）
// ---------------------------------------------------------------------------

describe('resume', () => {
  it('无中断签名 → 409 没有可继续的中断回复', async () => {
    const chat = seedChat();
    h.store.addMessage(chat.id, 'user', '问题');
    h.store.addMessage(chat.id, 'assistant', '完整回答'); // 已正常完结
    installStream(() => {});
    const cap = makeEmitCapture();
    const res = await makeRouter().handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/send`,
        body: { resume: true },
      },
      cap.emit,
    );
    expect(res.status).toBe(409);
    expect((cap.events()[0].data as { message: string }).message).toContain('没有可继续的中断回复');
  });

  it('中断签名成立（turn_active 残留 + 末尾是 user）→ resume:true + 空 messages', async () => {
    const chat = seedChat();
    h.store.addMessage(chat.id, 'user', '崩溃前的问题');
    h.store.setTurnActive(chat.id); // 模拟崩溃残留的标记
    const { seen } = installStream((opts) => opts.onText('续跑回答'));

    const res = await makeRouter().handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/send`,
        body: { resume: true },
      },
      makeEmitCapture().emit,
    );
    expect(res.status).toBe(200);
    expect(seen[0].resume).toBe(true);
    // record 是唯一权威：不喂历史
    expect(seen[0].messages).toEqual([]);
    // 不追加新用户消息
    expect(h.store.listMessages(chat.id, 10).filter((m) => m.role === 'user')).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// 提示词拼装：模式 / 工具策略 / 人设 / 项目 / 技能 / 附件
// ---------------------------------------------------------------------------

describe('流式回合的提示词与工具面', () => {
  it('plan 模式：写工具被过滤，只暴露只读工具', async () => {
    const chat = seedChat();
    const { seen } = installStream(() => {});
    await makeRouter().handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/send`,
        body: { message: '先出计划', mode: 'plan' },
      },
      makeEmitCapture().emit,
    );
    const toolNames = seen[0].tools.map((t: { name: string }) => t.name);
    expect(toolNames).toContain('local_read_file');
    expect(toolNames).not.toContain('local_write_file');
    // plan 模式的系统提示词包含模式前言
    expect(seen[0].systemPrompt).toContain('PLAN');
  });

  it('智能体工具策略（allowlist）真实收窄本轮工具面', async () => {
    const agent = h.store.createChatAgent({
      name: '只读助手',
      toolPolicy: { mode: 'allowlist', tools: ['local_read_file'] },
    });
    const chat = h.store.createChat('策略', agent.id, null);
    const { seen } = installStream(() => {});
    await makeRouter().handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/send`,
        body: { message: 'hi' },
      },
      makeEmitCapture().emit,
    );
    const toolNames = seen[0].tools.map((t: { name: string }) => t.name);
    expect(toolNames).toEqual(['local_read_file']);
    // 策略随 toolContext 下发供分发层复检
    expect(seen[0].toolContext.toolPolicy).toEqual({
      mode: 'allowlist',
      tools: ['local_read_file'],
    });
  });

  it('@提及带 rolePrompt 的智能体 → 系统提示词含人设前言', async () => {
    const agent = h.store.createChatAgent({ name: '审稿人', rolePrompt: '你是严格的审稿人' });
    const chat = seedChat();
    const { seen } = installStream(() => {});
    await makeRouter().handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/send`,
        body: { message: '看看这篇', mentionedAgentId: agent.id },
      },
      makeEmitCapture().emit,
    );
    expect(seen[0].systemPrompt).toContain('【当前角色】审稿人');
    expect(seen[0].systemPrompt).toContain('你是严格的审稿人');
    expect(seen[0].systemPrompt).toContain('你是 审稿人');
  });

  it('绑定智能体即使没有 rolePrompt，系统提示词自称也用智能体显示名', async () => {
    const agent = h.store.createChatAgent({ name: '电脑操作员' });
    const chat = h.store.createChat('对话', agent.id, null);
    const { seen } = installStream(() => {});
    await makeRouter().handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/send`,
        body: { message: '你是谁' },
      },
      makeEmitCapture().emit,
    );
    expect(seen[0].systemPrompt).toContain('你是 电脑操作员');
    expect(seen[0].systemPrompt).not.toContain('【当前角色】');
  });

  it('绑定项目的会话：系统提示词追加项目围栏；信任项目时注入规则文件', async () => {
    const registry = makeProjectRegistry([
      { id: 'proj-1', name: '演示项目', folderPath: '/tmp/proj-1', trusted: true },
    ]);
    h.loadProjectRuleFiles.mockReturnValue({ files: ['AGENTS.md'], content: '项目规则正文' });
    const chat = h.store.createChat('项目对话', 'agent-a', 'proj-1');
    const { seen } = installStream(() => {});
    await makeRouter({ toolRouter: makeToolRouter({ projectRegistry: registry }) }).handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/send`,
        body: { message: 'hi' },
      },
      makeEmitCapture().emit,
    );
    expect(seen[0].systemPrompt).toContain('【项目模式】');
    expect(seen[0].systemPrompt).toContain('/tmp/proj-1');
    expect(seen[0].systemPrompt).toContain('【项目规则】');
    expect(seen[0].systemPrompt).toContain('项目规则正文');
  });

  it('未信任的项目不注入规则文件内容', async () => {
    const registry = makeProjectRegistry([
      { id: 'proj-1', name: '演示项目', folderPath: '/tmp/proj-1', trusted: false },
    ]);
    h.loadProjectRuleFiles.mockReturnValue({ files: ['AGENTS.md'], content: '恶意规则' });
    const chat = h.store.createChat('项目对话', 'agent-a', 'proj-1');
    const { seen } = installStream(() => {});
    await makeRouter({ toolRouter: makeToolRouter({ projectRegistry: registry }) }).handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/send`,
        body: { message: 'hi' },
      },
      makeEmitCapture().emit,
    );
    expect(seen[0].systemPrompt).toContain('【项目模式】');
    expect(seen[0].systemPrompt).not.toContain('恶意规则');
  });

  it('行首 "/技能名" 显式触发：技能正文并入本轮用户消息', async () => {
    const chat = seedChat();
    h.loadSkills.mockResolvedValue([
      { name: 'myskill', dirName: '90-myskill', displayName: '', description: '', priority: 1, tags: [], layer: 'catalog', modelInvocable: true, skillsDir: '/x' },
    ]);
    h.findSkill.mockResolvedValue({
      name: 'myskill',
      dirName: '90-myskill',
      displayName: '',
      description: '技能描述',
      priority: 1,
      tags: [],
      layer: 'catalog',
      modelInvocable: true,
      content: '技能正文：按步骤处理',
      skillsDir: '/x',
    });
    const { seen } = installStream(() => {});
    await makeRouter().handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/send`,
        body: { message: '/myskill 帮我处理数据' },
      },
      makeEmitCapture().emit,
    );
    expect(h.findSkill).toHaveBeenCalledWith('myskill', expect.anything());
    const lastUser = seen[0].messages.at(-1);
    expect(lastUser.content).toContain('技能正文：按步骤处理');
    expect(lastUser.content).toContain('帮我处理数据');
    // 落库的用户消息保留用户输入原文（含触发前缀），清理后的文本只进模型上下文
    const persisted = h.store.listMessages(chat.id, 10).find((m) => m.role === 'user');
    expect(persisted?.content).toBe('/myskill 帮我处理数据');
  });

  it('图片附件：处理备注注入本轮用户消息', async () => {
    const chat = seedChat();
    h.processImageAttachments.mockReturnValue({
      images: [{ data: 'QUJD', mediaType: 'image/png' }],
      notes: ['已附加 1 张图片'],
    });
    const { seen } = installStream(() => {});
    await makeRouter().handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/send`,
        body: { message: '看图', images: [{ path: '/tmp/a.png', name: 'a.png' }] },
      },
      makeEmitCapture().emit,
    );
    const lastUser = seen[0].messages.at(-1);
    expect(lastUser.content).toContain('【附件图片】');
    expect(lastUser.content).toContain('已附加 1 张图片');
    expect(lastUser.images).toEqual([{ data: 'QUJD', mediaType: 'image/png' }]);
  });

  it('referencedChatIds：被引用对话的摘录作为 user 消息注入到本对话历史之前', async () => {
    const refChat = h.store.createChat('参考对话', 'agent-a', null);
    h.store.addMessage(refChat.id, 'user', '参考里的问题');
    h.store.addMessage(refChat.id, 'assistant', '参考里的回答');
    const chat = seedChat();
    const { seen } = installStream(() => {});
    await makeRouter().handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/send`,
        body: { message: '结合上面那段历史回答', referencedChatIds: [refChat.id] },
      },
      makeEmitCapture().emit,
    );
    // 引用摘录在最前，当前用户消息在最后
    expect(seen[0].messages).toHaveLength(2);
    expect(seen[0].messages[0].role).toBe('user');
    expect(seen[0].messages[0].content).toContain('【引用对话：参考对话】');
    expect(seen[0].messages[0].content).toContain('参考里的回答');
    expect(seen[0].messages.at(-1).content).toBe('结合上面那段历史回答');
  });

  it('referencedChatIds：引用自身或不存在的对话被跳过', async () => {
    const chat = seedChat();
    const { seen } = installStream(() => {});
    await makeRouter().handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/send`,
        body: { message: 'hi', referencedChatIds: [chat.id, 'ghost-chat'] },
      },
      makeEmitCapture().emit,
    );
    expect(seen[0].messages).toHaveLength(1);
  });

  it('payload.model 占位值（default/auto）回落到设置模型；显式值覆盖', async () => {
    const chat = seedChat();
    const { seen } = installStream(() => {});
    const router = makeRouter();
    await router.handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/send`,
        body: { message: 'hi', model: 'default' },
      },
      makeEmitCapture().emit,
    );
    expect(seen[0].model).toBe('unit-test-model');

    await router.handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/send`,
        body: { message: 'hi', model: 'gpt-x', reasoningEffort: 'high' },
      },
      makeEmitCapture().emit,
    );
    expect(seen[1].model).toBe('gpt-x');
    expect(seen[1].reasoningEffort).toBe('high');
  });
});

// ---------------------------------------------------------------------------
// 后台标题生成
// ---------------------------------------------------------------------------

describe('后台标题生成', () => {
  it('默认标题的首个真实回合结束后生成标题并广播 chat-title-updated', async () => {
    const chat = seedChat('新对话');
    installStream((opts) => opts.onText('回答'));
    const { broadcast, calls } = makeBroadcast();
    await makeRouter({ broadcast }).handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '量子计算是什么' } },
      makeEmitCapture().emit,
    );
    await vi.waitFor(() => {
      expect(h.generateChatTitle).toHaveBeenCalledWith('量子计算是什么', expect.anything());
    });
    await vi.waitFor(() => {
      expect(calls).toContainEqual({
        event: 'chat-title-updated',
        payload: { chatId: chat.id, title: '生成的标题' },
      });
    });
    expect(h.store.getChat(chat.id)?.title).toBe('生成的标题');
  });

  it('已有 assistant 历史的会话不改标题', async () => {
    const chat = seedChat('新对话');
    h.store.addMessage(chat.id, 'user', '旧问题');
    h.store.addMessage(chat.id, 'assistant', '旧回答');
    installStream(() => {});
    await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '新问题' } },
      makeEmitCapture().emit,
    );
    await new Promise((r) => setTimeout(r, 20));
    expect(h.generateChatTitle).not.toHaveBeenCalled();
  });

  it('regenerate 不触发标题生成', async () => {
    const chat = seedChat('新对话');
    h.store.addMessage(chat.id, 'user', '问题');
    const target = h.store.addMessage(chat.id, 'assistant', '回答');
    installStream(() => {});
    await makeRouter().handleStream(
      {
        method: 'POST',
        path: `/api/v2/chats/${chat.id}/messages/${target.id}/regenerate`,
        body: {},
      },
      makeEmitCapture().emit,
    );
    await new Promise((r) => setTimeout(r, 20));
    expect(h.generateChatTitle).not.toHaveBeenCalled();
  });

  it('用户在生成期间手改了标题 → 不覆盖、不广播', async () => {
    const chat = seedChat('新对话');
    installStream(() => {});
    h.generateChatTitle.mockImplementation(async () => {
      // 模拟用户在标题生成期间手动改名
      h.store.updateChat(chat.id, { title: '用户改的标题' });
      return { title: '生成的标题', usedFallback: false };
    });
    const { broadcast, calls } = makeBroadcast();
    await makeRouter({ broadcast }).handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: 'hi' } },
      makeEmitCapture().emit,
    );
    await vi.waitFor(() => {
      expect(h.generateChatTitle).toHaveBeenCalled();
    });
    await new Promise((r) => setTimeout(r, 20));
    expect(h.store.getChat(chat.id)?.title).toBe('用户改的标题');
    expect(calls.filter((c) => c.event === 'chat-title-updated')).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// 后台追问建议（WorkBuddy 式 3 条下一轮输入）
// ---------------------------------------------------------------------------

describe('后台追问建议', () => {
  it('完成回合先广播启发式兜底，LLM 成功后再替换', async () => {
    const chat = seedChat();
    installStream((opts) => opts.onText('PPT 已生成'));
    let finishLlm: (value: {
      suggestions: string[];
      usedFallback: boolean;
    }) => void = () => {};
    h.generateSuggestedReplies.mockImplementation(
      () =>
        new Promise((resolve) => {
          finishLlm = resolve;
        }),
    );
    const { broadcast, calls } = makeBroadcast();
    await makeRouter({ broadcast }).handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '制作自我介绍ppt' } },
      makeEmitCapture().emit,
    );

    const assistant = h.store.listMessages(chat.id, 10).find((m) => m.role === 'assistant')!;
    expect(h.fallbackSuggestedReplies).toHaveBeenCalledWith('制作自我介绍ppt', 'PPT 已生成');
    expect(calls).toContainEqual({
      event: 'suggested-replies',
      payload: {
        chatId: chat.id,
        messageId: assistant.id,
        suggestions: ['兜底-1', '兜底-2', '兜底-3'],
      },
    });
    expect(JSON.parse(assistant.messageMetadata!)).toMatchObject({
      suggestedReplies: ['兜底-1', '兜底-2', '兜底-3'],
    });

    finishLlm({
      suggestions: ['llm-追问-1', 'llm-追问-2', 'llm-追问-3'],
      usedFallback: false,
    });
    await vi.waitFor(() => {
      expect(calls).toContainEqual({
        event: 'suggested-replies',
        payload: {
          chatId: chat.id,
          messageId: assistant.id,
          suggestions: ['llm-追问-1', 'llm-追问-2', 'llm-追问-3'],
        },
      });
    });
    expect(JSON.parse(h.store.getMessage(chat.id, assistant.id)!.messageMetadata!)).toMatchObject({
      suggestedReplies: ['llm-追问-1', 'llm-追问-2', 'llm-追问-3'],
    });
  });

  it('LLM 走兜底时不发第二次广播', async () => {
    h.generateSuggestedReplies.mockResolvedValue({
      suggestions: ['兜底-1', '兜底-2', '兜底-3'],
      usedFallback: true,
    });
    const chat = seedChat();
    installStream((opts) => opts.onText('回答'));
    const { broadcast, calls } = makeBroadcast();
    await makeRouter({ broadcast }).handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '你好' } },
      makeEmitCapture().emit,
    );
    await vi.waitFor(() => expect(h.generateSuggestedReplies).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(calls.filter((c) => c.event === 'suggested-replies')).toHaveLength(1);
  });

  it('取消 / 失败 / 空回复不生成建议', async () => {
    const cancelled = seedChat();
    installStream(() => {
      throw new DOMException('aborted', 'AbortError');
    });
    await makeRouter({ broadcast: makeBroadcast().broadcast }).handleStream(
      { method: 'POST', path: `/api/v2/chats/${cancelled.id}/send`, body: { message: 'hi' } },
      makeEmitCapture().emit,
    );
    expect(h.fallbackSuggestedReplies).not.toHaveBeenCalled();

    h.fallbackSuggestedReplies.mockClear();
    const failed = seedChat();
    installStream(() => {}, { status: 'failed', reason: 'HTTP 401' });
    await makeRouter({ broadcast: makeBroadcast().broadcast }).handleStream(
      { method: 'POST', path: `/api/v2/chats/${failed.id}/send`, body: { message: 'hi' } },
      makeEmitCapture().emit,
    );
    expect(h.fallbackSuggestedReplies).not.toHaveBeenCalled();

    h.fallbackSuggestedReplies.mockClear();
    const empty = seedChat();
    installStream(() => {});
    await makeRouter({ broadcast: makeBroadcast().broadcast }).handleStream(
      { method: 'POST', path: `/api/v2/chats/${empty.id}/send`, body: { message: 'hi' } },
      makeEmitCapture().emit,
    );
    expect(h.fallbackSuggestedReplies).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 回合产物文件列表（turn_files）
// ---------------------------------------------------------------------------

describe('回合产物文件列表', () => {
  /** 建一个绑定到真实临时项目目录的会话（扫描根 = 项目根）。 */
  async function seedProjectChat() {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'turn-files-router-'));
    const projectRegistry = makeProjectRegistry([
      { id: 'proj-1', name: '演示项目', folderPath: dir, trusted: true },
    ]);
    const chat = h.store.createChat('新对话', 'agent-a', 'proj-1');
    return { dir, chat, toolRouter: makeToolRouter({ projectRegistry }) };
  }

  it('回合内写到项目根的文件经 turn_files 事件下发（先于 message_id）并落 metadata', async () => {
    const { dir, chat, toolRouter } = await seedProjectChat();
    try {
      installStream(async (opts) => {
        opts.onToolStart({ id: 'call-1', tool: 'local_exec_shell', arguments: { command: 'gen ppt' } });
        // 脚本间接产物：不经写工具参数，只能靠工作区扫描发现。
        await fs.writeFile(path.join(dir, '自我介绍.pptx'), 'ppt-bytes');
        opts.onToolAction({
          id: 'call-1',
          tool: 'local_exec_shell',
          arguments: { command: 'gen ppt' },
          result: { success: true },
          success: true,
        });
        opts.onText('PPT 已生成');
      });
      const cap = makeEmitCapture();
      const res = await makeRouter({ toolRouter }).handleStream(
        { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '做个 PPT' } },
        cap.emit,
      );
      expect(res.status).toBe(200);

      const fileEvents = cap.byType('turn_files');
      expect(fileEvents).toHaveLength(1);
      const files = (fileEvents[0].data as { files: Array<{ path: string; kind: string; size: number }> }).files;
      expect(files).toEqual([
        { path: path.join(dir, '自我介绍.pptx'), kind: expect.stringMatching(/^(created|modified)$/), size: 9 },
      ]);

      // 顺序约定：turn_files 在 message_id 之前（前端按 message_id 归档本轮队列）。
      const types = cap
        .events()
        .map((c) => (typeof c.data === 'object' && c.data !== null ? (c.data as { type?: string }).type : null));
      expect(types.indexOf('turn_files')).toBeGreaterThanOrEqual(0);
      expect(types.indexOf('turn_files')).toBeLessThan(types.indexOf('message_id'));

      const assistant = h.store.listMessages(chat.id, 10)[0];
      const metadata = JSON.parse(assistant.messageMetadata!);
      expect(metadata.turnFiles).toHaveLength(1);
      expect(metadata.turnFiles[0].path).toBe(path.join(dir, '自我介绍.pptx'));
    } finally {
      await fs.rm(dir, { recursive: true, force: true });
    }
  });

  it('写工具落到项目根之外的路径经参数并集进入列表', async () => {
    const { dir, chat, toolRouter } = await seedProjectChat();
    const outside = await fs.mkdtemp(path.join(os.tmpdir(), 'turn-files-outside-'));
    try {
      const outsideFile = path.join(outside, 'report.md');
      installStream(async (opts) => {
        opts.onToolStart({ id: 'call-1', tool: 'local_write_file', arguments: { path: outsideFile } });
        await fs.writeFile(outsideFile, '# 报告');
        opts.onToolAction({
          id: 'call-1',
          tool: 'local_write_file',
          arguments: { path: outsideFile },
          result: { success: true },
          success: true,
        });
        opts.onText('写好了');
      });
      const cap = makeEmitCapture();
      await makeRouter({ toolRouter }).handleStream(
        { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '写报告' } },
        cap.emit,
      );

      const fileEvents = cap.byType('turn_files');
      expect(fileEvents).toHaveLength(1);
      const files = (fileEvents[0].data as { files: Array<{ path: string }> }).files;
      expect(files.map((f) => f.path)).toEqual([outsideFile]);
    } finally {
      await fs.rm(dir, { recursive: true, force: true });
      await fs.rm(outside, { recursive: true, force: true });
    }
  });

  it('回合没有产物时不发 turn_files，metadata 不带 turnFiles', async () => {
    const { dir, chat, toolRouter } = await seedProjectChat();
    try {
      installStream((opts) => {
        opts.onToolStart({ id: 'call-1', tool: 'local_read_file', arguments: { path: '/etc/hosts' } });
        opts.onToolAction({
          id: 'call-1',
          tool: 'local_read_file',
          arguments: { path: '/etc/hosts' },
          result: { success: true, content: 'hosts' },
          success: true,
        });
        opts.onText('读完了');
      });
      const cap = makeEmitCapture();
      await makeRouter({ toolRouter }).handleStream(
        { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '读一下' } },
        cap.emit,
      );

      expect(cap.byType('turn_files')).toHaveLength(0);
      const assistant = h.store.listMessages(chat.id, 10)[0];
      const metadata = JSON.parse(assistant.messageMetadata!);
      expect(metadata.turnFiles).toBeUndefined();
    } finally {
      await fs.rm(dir, { recursive: true, force: true });
    }
  });

  it('未绑定项目的会话没有扫描根，不产事件', async () => {
    const chat = seedChat();
    installStream((opts) => opts.onText('纯聊天'));
    const cap = makeEmitCapture();
    await makeRouter().handleStream(
      { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: 'hi' } },
      cap.emit,
    );
    expect(cap.byType('turn_files')).toHaveLength(0);
  });

  it('未绑定项目的会话：exec cwd 浅扫描 + 命令文本路径字面量捕获脚本产物', async () => {
    // 无项目对话里 exec 的 cwd 与脚本 save 路径都不在任何递归根里——
    // 工作区扫描覆盖不到，靠 exec 线索兜底（真实场景：脚本在 home 写 PPT）。
    const chat = seedChat();
    const work = await fs.mkdtemp(path.join(os.tmpdir(), 'turn-files-unbound-'));
    try {
      const ppt = path.join(work, '自我介绍_张三.pptx');
      installStream(async (opts) => {
        const command = `python3 -c "from pptx import Presentation; prs.save('${ppt}')"`;
        opts.onToolStart({ id: 'call-1', tool: 'local_exec_shell', arguments: { command, cwd: work } });
        await fs.writeFile(ppt, 'ppt-bytes');
        opts.onToolAction({
          id: 'call-1',
          tool: 'local_exec_shell',
          arguments: { command, cwd: work },
          result: { success: true },
          success: true,
        });
        opts.onText('PPT 已生成');
      });
      const cap = makeEmitCapture();
      await makeRouter().handleStream(
        { method: 'POST', path: `/api/v2/chats/${chat.id}/send`, body: { message: '创建自我介绍ppt' } },
        cap.emit,
      );

      const fileEvents = cap.byType('turn_files');
      expect(fileEvents).toHaveLength(1);
      const files = (fileEvents[0].data as { files: Array<{ path: string }> }).files;
      expect(files.map((f) => f.path)).toEqual([ppt]);
    } finally {
      await fs.rm(work, { recursive: true, force: true });
    }
  });
});
