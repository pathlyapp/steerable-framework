/**
 * LocalStore 会话（chat_sessions）集成测试：真实 SQLite（临时目录库文件）。
 *
 * 覆盖：建/读/改/删、listChats 分页与排序（含 clampInt 对坏输入的回退）、
 * 空会话清理三件套、项目降级（clearProjectAssignment）、以及挂在
 * settings_kv 上的 chat_record / turn_active 两个标记。
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  cleanupTestStores,
  createTestStore,
  loadStorageModule,
} from './local-store-testkit.js';

await loadStorageModule();

afterEach(() => {
  cleanupTestStores();
  vi.useRealTimers();
});

describe('LocalStore / 会话创建与读取', () => {
  it('createChat 默认值：新对话、绑定品牌默认智能体、无项目', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat();
    expect(chat.title).toBe('新对话');
    // 中性 shell 品牌的 defaultAgentId 是 local-assistant。
    expect(chat.agentId).toBe('local-assistant');
    expect(chat.projectId).toBeNull();
    expect(chat.userId).toBe('local');
    expect(chat.isPinned).toBe(false);
    expect(chat.systemPrompt).toBeNull();
    expect(chat.pinnedRefs).toBeNull();
    expect(chat.id).toMatch(/^[0-9a-f]{8}-[0-9a-f-]{27}$/);
    expect(chat.createdAt).toBe(chat.updatedAt);
  });

  it('createChat 自定义标题 / 智能体 / 项目，读回一致', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('排查日志', 'all-round-assistant', 'proj-1');
    expect(chat.title).toBe('排查日志');
    expect(chat.agentId).toBe('all-round-assistant');
    expect(chat.projectId).toBe('proj-1');
    expect(await store.getChat(chat.id)).toEqual(chat);
  });

  it('getChat 对不存在的 id 返回 null', async () => {
    const { store, db } = await createTestStore();
    expect(await store.getChat('ghost')).toBeNull();
  });

  it('createChatWithId 幂等：同 id 重复补建返回原行，不覆盖标题', async () => {
    const { store, db } = await createTestStore();
    const first = await store.createChatWithId('chat-fixed', '首次标题');
    const second = await store.createChatWithId('chat-fixed', '后来的标题');
    expect(second.id).toBe('chat-fixed');
    expect(second.title).toBe('首次标题');
    expect(second.createdAt).toBe(first.createdAt);
    expect((await store.listChats()).total).toBe(1);
  });
});

describe('LocalStore / listChats 分页与排序', () => {
  it('空库返回空列表与 total 0', async () => {
    const { store, db } = await createTestStore();
    expect(await store.listChats()).toEqual({ chats: [], total: 0 });
  });

  it('按页切分，total 是全量计数', async () => {
    const { store, db } = await createTestStore();
    for (let i = 0; i < 3; i += 1) await store.createChat(`会话${i}`);
    const page1 = await store.listChats(1, 2);
    const page2 = await store.listChats(2, 2);
    expect(page1.chats).toHaveLength(2);
    expect(page2.chats).toHaveLength(1);
    expect(page1.total).toBe(3);
    expect(page2.total).toBe(3);
    // 两页不重叠且并集是全量。
    const ids = [...page1.chats, ...page2.chats].map((c) => c.id);
    expect(new Set(ids).size).toBe(3);
  });

  it('置顶优先，其次按 updated_at 新→旧', async () => {
    vi.useFakeTimers();
    const { store, db } = await createTestStore();
    vi.setSystemTime('2026-01-01T00:00:01.000Z');
    const oldest = await store.createChat('最旧');
    vi.setSystemTime('2026-01-01T00:00:02.000Z');
    const pinned = await store.createChat('置顶');
    vi.setSystemTime('2026-01-01T00:00:03.000Z');
    const newest = await store.createChat('最新');
    await store.updateChat(pinned.id, { isPinned: true });

    const { chats } = await store.listChats();
    // datetime(updated_at) 截断到秒，逐秒推进保证次序确定。
    expect(chats.map((c) => c.id)).toEqual([pinned.id, newest.id, oldest.id]);
  });

  it('坏分页输入被 clampInt 回退：NaN 页码回第 1 页，NaN/超限 limit 回默认区间', async () => {
    const { store, db } = await createTestStore();
    for (let i = 0; i < 2; i += 1) await store.createChat(`会话${i}`);
    // NaN（如 ?page=abc 经 Number() 解析而来）不应产出 NaN OFFSET/LIMIT。
    expect((await store.listChats(Number.NaN, Number.NaN)).chats).toHaveLength(2);
    // limit 下限 1：0 被夹到 1。
    expect((await store.listChats(1, 0)).chats).toHaveLength(1);
    // limit 上限 200：超限不炸，正常返回。
    expect((await store.listChats(1, 9999)).chats).toHaveLength(2);
  });
});

describe('LocalStore / updateChat', () => {
  it('部分更新只动传入字段；undefined 不抹掉现有值', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('原标题', 'agent-a', 'proj-1');
    const updated = await store.updateChat(chat.id, { title: '新标题', systemPrompt: undefined });
    expect(updated?.title).toBe('新标题');
    expect(updated?.agentId).toBe('agent-a');
    expect(updated?.projectId).toBe('proj-1');
    expect(updated?.systemPrompt).toBeNull();
  });

  it('projectId 显式传 null 才是移出项目', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('项目会话', null, 'proj-1');
    expect((await store.updateChat(chat.id, {}))?.projectId).toBe('proj-1');
    expect((await store.updateChat(chat.id, { projectId: null }))?.projectId).toBeNull();
  });

  it('isPinned / pinnedRefs / systemPrompt 往返', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    const refs = [{ kind: 'file', path: '/tmp/a.ts' }];
    const updated = await store.updateChat(chat.id, {
      isPinned: true,
      pinnedRefs: refs,
      systemPrompt: '你是审查员',
    });
    expect(updated?.isPinned).toBe(true);
    expect(updated?.pinnedRefs).toEqual(refs);
    expect(updated?.systemPrompt).toBe('你是审查员');
  });

  it('updated_at 随更新刷新', async () => {
    vi.useFakeTimers();
    const { store, db } = await createTestStore();
    vi.setSystemTime('2026-01-01T00:00:00.000Z');
    const chat = await store.createChat('x');
    vi.setSystemTime('2026-01-01T00:00:05.000Z');
    const updated = await store.updateChat(chat.id, { title: 'y' });
    expect(updated?.updatedAt).toBe('2026-01-01T00:00:05.000Z');
    expect(updated?.createdAt).toBe('2026-01-01T00:00:00.000Z');
  });

  it('更新不存在的会话返回 null', async () => {
    const { store, db } = await createTestStore();
    expect(await store.updateChat('ghost', { title: 'y' })).toBeNull();
  });
});

describe('LocalStore / 删除与空会话清理', () => {
  it('deleteChat 存在删 true，再删 false', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    expect(await store.deleteChat(chat.id)).toBe(true);
    expect(await store.deleteChat(chat.id)).toBe(false);
    expect(await store.getChat(chat.id)).toBeNull();
  });

  it('deleteChat 级联删除消息 / 任务 / trace（外键 ON DELETE CASCADE 生效）', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    await store.addMessage(chat.id, 'user', '你好');
    await store.createTask({ chatId: chat.id, task: '后台任务' });
    await store.saveTrace({ id: 'trace-1', chatId: chat.id, startedAtMs: 1, status: 'ok', payload: {} });

    expect(await store.deleteChat(chat.id)).toBe(true);
    // 级联必须直查底层表——list* 对不存在的 chat 本来就返回空。
    const count = (table: string) =>
      (db.prepare(`SELECT COUNT(*) AS n FROM ${table} WHERE chat_id = ?`).get(chat.id) as { n: number }).n;
    expect(count('chat_messages')).toBe(0);
    expect(count('tasks')).toBe(0);
    expect(count('harness_traces')).toBe(0);
  });

  it('deleteChat 顺带清掉 chat_record / turn_active 两个 settings_kv 标记', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    await store.setChatRecordId(chat.id, 'record-1');
    await store.setTurnActive(chat.id);
    await store.deleteChat(chat.id);
    expect(await store.getChatRecordId(chat.id)).toBeNull();
    expect(await store.getTurnActive(chat.id)).toBeNull();
  });

  it('chatHasMessages / deleteChatIfEmpty：有消息的会话受保护', async () => {
    const { store, db } = await createTestStore();
    const empty = await store.createChat('空');
    const busy = await store.createChat('有内容');
    expect(await store.chatHasMessages(busy.id)).toBe(false);
    await store.addMessage(busy.id, 'user', 'hi');
    expect(await store.chatHasMessages(busy.id)).toBe(true);

    expect(await store.deleteChatIfEmpty(busy.id)).toBe(false);
    expect(await store.getChat(busy.id)).not.toBeNull();
    expect(await store.deleteChatIfEmpty(empty.id)).toBe(true);
    expect(await store.deleteChatIfEmpty('ghost')).toBe(false);
  });

  it('deleteEmptyChats 清掉全部空会话，exceptChatId 保留打开中的作曲框', async () => {
    const { store, db } = await createTestStore();
    const a = await store.createChat('a');
    const keep = await store.createChat('keep');
    const busy = await store.createChat('busy');
    await store.addMessage(busy.id, 'user', 'hi');

    const removed = await store.deleteEmptyChats(keep.id);
    expect(new Set(removed)).toEqual(new Set([a.id]));
    expect(await store.getChat(keep.id)).not.toBeNull();
    expect(await store.getChat(busy.id)).not.toBeNull();

    // 不传 except：keep 也被清掉，只剩 busy。
    expect(await store.deleteEmptyChats()).toEqual([keep.id]);
    expect((await store.listChats()).chats.map((c) => c.id)).toEqual([busy.id]);
  });
});

describe('LocalStore / 项目绑定与降级', () => {
  it('clearProjectAssignment 只降级指定项目的会话并返回行数', async () => {
    const { store, db } = await createTestStore();
    const a = await store.createChat('a', null, 'proj-1');
    const b = await store.createChat('b', null, 'proj-1');
    const other = await store.createChat('c', null, 'proj-2');

    expect(await store.clearProjectAssignment('proj-1')).toBe(2);
    expect((await store.getChat(a.id))?.projectId).toBeNull();
    expect((await store.getChat(b.id))?.projectId).toBeNull();
    expect((await store.getChat(other.id))?.projectId).toBe('proj-2');
    // 会话本身不删。
    expect((await store.listChats()).total).toBe(3);
    // 再清一次是 0 行（幂等）。
    expect(await store.clearProjectAssignment('proj-1')).toBe(0);
  });
});

describe('LocalStore / chat_record 与 turn_active 标记', () => {
  it('setChatRecordId / getChatRecordId 往返与覆盖', async () => {
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    expect(await store.getChatRecordId(chat.id)).toBeNull();
    await store.setChatRecordId(chat.id, 'record-1');
    expect(await store.getChatRecordId(chat.id)).toBe('record-1');
    await store.setChatRecordId(chat.id, 'record-2');
    expect(await store.getChatRecordId(chat.id)).toBe('record-2');
  });

  it('turn_active：set 后可读 startedAt，clear 后归 null', async () => {
    vi.useFakeTimers();
    const { store, db } = await createTestStore();
    const chat = await store.createChat('x');
    expect(await store.getTurnActive(chat.id)).toBeNull();
    vi.setSystemTime('2026-01-01T08:00:00.000Z');
    await store.setTurnActive(chat.id);
    expect(await store.getTurnActive(chat.id)).toEqual({ startedAt: '2026-01-01T08:00:00.000Z' });
    await store.clearTurnActive(chat.id);
    expect(await store.getTurnActive(chat.id)).toBeNull();
  });

  it('turn_active 存了坏 JSON 时读为 null（崩溃标记不容错就会误报）', async () => {
    const { store, db } = await createTestStore();
    db
      .prepare(`INSERT INTO settings_kv (tenant_id, user_id, key, value)
                VALUES ('local', 'local', ?, ?)`)
      .run('turn_active:chat-bad', 'not-json');
    expect(await store.getTurnActive('chat-bad')).toBeNull();
    // startedAt 不是字符串同样归 null。
    db
      .prepare(`UPDATE settings_kv SET value = ? WHERE key = ?`)
      .run('{"startedAt": 123}', 'turn_active:chat-bad');
    expect(await store.getTurnActive('chat-bad')).toBeNull();
  });
});
