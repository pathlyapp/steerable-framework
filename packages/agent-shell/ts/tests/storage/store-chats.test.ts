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

const { LocalStore } = await loadStorageModule();

afterEach(() => {
  cleanupTestStores();
  vi.useRealTimers();
});

describe('LocalStore / 会话创建与读取', () => {
  it('createChat 默认值：新对话、绑定品牌默认智能体、无项目', () => {
    const { store } = createTestStore(LocalStore);
    const chat = store.createChat();
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

  it('createChat 自定义标题 / 智能体 / 项目，读回一致', () => {
    const { store } = createTestStore(LocalStore);
    const chat = store.createChat('排查日志', 'all-round-assistant', 'proj-1');
    expect(chat.title).toBe('排查日志');
    expect(chat.agentId).toBe('all-round-assistant');
    expect(chat.projectId).toBe('proj-1');
    expect(store.getChat(chat.id)).toEqual(chat);
  });

  it('getChat 对不存在的 id 返回 null', () => {
    const { store } = createTestStore(LocalStore);
    expect(store.getChat('ghost')).toBeNull();
  });

  it('createChatWithId 幂等：同 id 重复补建返回原行，不覆盖标题', () => {
    const { store } = createTestStore(LocalStore);
    const first = store.createChatWithId('chat-fixed', '首次标题');
    const second = store.createChatWithId('chat-fixed', '后来的标题');
    expect(second.id).toBe('chat-fixed');
    expect(second.title).toBe('首次标题');
    expect(second.createdAt).toBe(first.createdAt);
    expect(store.listChats().total).toBe(1);
  });
});

describe('LocalStore / listChats 分页与排序', () => {
  it('空库返回空列表与 total 0', () => {
    const { store } = createTestStore(LocalStore);
    expect(store.listChats()).toEqual({ chats: [], total: 0 });
  });

  it('按页切分，total 是全量计数', () => {
    const { store } = createTestStore(LocalStore);
    for (let i = 0; i < 3; i += 1) store.createChat(`会话${i}`);
    const page1 = store.listChats(1, 2);
    const page2 = store.listChats(2, 2);
    expect(page1.chats).toHaveLength(2);
    expect(page2.chats).toHaveLength(1);
    expect(page1.total).toBe(3);
    expect(page2.total).toBe(3);
    // 两页不重叠且并集是全量。
    const ids = [...page1.chats, ...page2.chats].map((c) => c.id);
    expect(new Set(ids).size).toBe(3);
  });

  it('置顶优先，其次按 updated_at 新→旧', () => {
    vi.useFakeTimers();
    const { store } = createTestStore(LocalStore);
    vi.setSystemTime('2026-01-01T00:00:01.000Z');
    const oldest = store.createChat('最旧');
    vi.setSystemTime('2026-01-01T00:00:02.000Z');
    const pinned = store.createChat('置顶');
    vi.setSystemTime('2026-01-01T00:00:03.000Z');
    const newest = store.createChat('最新');
    store.updateChat(pinned.id, { isPinned: true });

    const { chats } = store.listChats();
    // datetime(updated_at) 截断到秒，逐秒推进保证次序确定。
    expect(chats.map((c) => c.id)).toEqual([pinned.id, newest.id, oldest.id]);
  });

  it('坏分页输入被 clampInt 回退：NaN 页码回第 1 页，NaN/超限 limit 回默认区间', () => {
    const { store } = createTestStore(LocalStore);
    for (let i = 0; i < 2; i += 1) store.createChat(`会话${i}`);
    // NaN（如 ?page=abc 经 Number() 解析而来）不应产出 NaN OFFSET/LIMIT。
    expect(store.listChats(Number.NaN, Number.NaN).chats).toHaveLength(2);
    // limit 下限 1：0 被夹到 1。
    expect(store.listChats(1, 0).chats).toHaveLength(1);
    // limit 上限 200：超限不炸，正常返回。
    expect(store.listChats(1, 9999).chats).toHaveLength(2);
  });
});

describe('LocalStore / updateChat', () => {
  it('部分更新只动传入字段；undefined 不抹掉现有值', () => {
    const { store } = createTestStore(LocalStore);
    const chat = store.createChat('原标题', 'agent-a', 'proj-1');
    const updated = store.updateChat(chat.id, { title: '新标题', systemPrompt: undefined });
    expect(updated?.title).toBe('新标题');
    expect(updated?.agentId).toBe('agent-a');
    expect(updated?.projectId).toBe('proj-1');
    expect(updated?.systemPrompt).toBeNull();
  });

  it('projectId 显式传 null 才是移出项目', () => {
    const { store } = createTestStore(LocalStore);
    const chat = store.createChat('项目会话', null, 'proj-1');
    expect(store.updateChat(chat.id, {})?.projectId).toBe('proj-1');
    expect(store.updateChat(chat.id, { projectId: null })?.projectId).toBeNull();
  });

  it('isPinned / pinnedRefs / systemPrompt 往返', () => {
    const { store } = createTestStore(LocalStore);
    const chat = store.createChat('x');
    const refs = [{ kind: 'file', path: '/tmp/a.ts' }];
    const updated = store.updateChat(chat.id, {
      isPinned: true,
      pinnedRefs: refs,
      systemPrompt: '你是审查员',
    });
    expect(updated?.isPinned).toBe(true);
    expect(updated?.pinnedRefs).toEqual(refs);
    expect(updated?.systemPrompt).toBe('你是审查员');
  });

  it('updated_at 随更新刷新', () => {
    vi.useFakeTimers();
    const { store } = createTestStore(LocalStore);
    vi.setSystemTime('2026-01-01T00:00:00.000Z');
    const chat = store.createChat('x');
    vi.setSystemTime('2026-01-01T00:00:05.000Z');
    const updated = store.updateChat(chat.id, { title: 'y' });
    expect(updated?.updatedAt).toBe('2026-01-01T00:00:05.000Z');
    expect(updated?.createdAt).toBe('2026-01-01T00:00:00.000Z');
  });

  it('更新不存在的会话返回 null', () => {
    const { store } = createTestStore(LocalStore);
    expect(store.updateChat('ghost', { title: 'y' })).toBeNull();
  });
});

describe('LocalStore / 删除与空会话清理', () => {
  it('deleteChat 存在删 true，再删 false', () => {
    const { store } = createTestStore(LocalStore);
    const chat = store.createChat('x');
    expect(store.deleteChat(chat.id)).toBe(true);
    expect(store.deleteChat(chat.id)).toBe(false);
    expect(store.getChat(chat.id)).toBeNull();
  });

  it('deleteChat 级联删除消息 / 任务 / trace（外键 ON DELETE CASCADE 生效）', () => {
    const { store } = createTestStore(LocalStore);
    const chat = store.createChat('x');
    store.addMessage(chat.id, 'user', '你好');
    store.createTask({ chatId: chat.id, task: '后台任务' });
    store.saveTrace({ id: 'trace-1', chatId: chat.id, startedAtMs: 1, status: 'ok', payload: {} });

    expect(store.deleteChat(chat.id)).toBe(true);
    // 级联必须直查底层表——list* 对不存在的 chat 本来就返回空。
    const db = store.getPackDb();
    const count = (table: string) =>
      (db.prepare(`SELECT COUNT(*) AS n FROM ${table} WHERE chat_id = ?`).get(chat.id) as { n: number }).n;
    expect(count('chat_messages')).toBe(0);
    expect(count('tasks')).toBe(0);
    expect(count('harness_traces')).toBe(0);
  });

  it('deleteChat 顺带清掉 chat_record / turn_active 两个 settings_kv 标记', () => {
    const { store } = createTestStore(LocalStore);
    const chat = store.createChat('x');
    store.setChatRecordId(chat.id, 'record-1');
    store.setTurnActive(chat.id);
    store.deleteChat(chat.id);
    expect(store.getChatRecordId(chat.id)).toBeNull();
    expect(store.getTurnActive(chat.id)).toBeNull();
  });

  it('chatHasMessages / deleteChatIfEmpty：有消息的会话受保护', () => {
    const { store } = createTestStore(LocalStore);
    const empty = store.createChat('空');
    const busy = store.createChat('有内容');
    expect(store.chatHasMessages(busy.id)).toBe(false);
    store.addMessage(busy.id, 'user', 'hi');
    expect(store.chatHasMessages(busy.id)).toBe(true);

    expect(store.deleteChatIfEmpty(busy.id)).toBe(false);
    expect(store.getChat(busy.id)).not.toBeNull();
    expect(store.deleteChatIfEmpty(empty.id)).toBe(true);
    expect(store.deleteChatIfEmpty('ghost')).toBe(false);
  });

  it('deleteEmptyChats 清掉全部空会话，exceptChatId 保留打开中的作曲框', () => {
    const { store } = createTestStore(LocalStore);
    const a = store.createChat('a');
    const keep = store.createChat('keep');
    const busy = store.createChat('busy');
    store.addMessage(busy.id, 'user', 'hi');

    const removed = store.deleteEmptyChats(keep.id);
    expect(new Set(removed)).toEqual(new Set([a.id]));
    expect(store.getChat(keep.id)).not.toBeNull();
    expect(store.getChat(busy.id)).not.toBeNull();

    // 不传 except：keep 也被清掉，只剩 busy。
    expect(store.deleteEmptyChats()).toEqual([keep.id]);
    expect(store.listChats().chats.map((c) => c.id)).toEqual([busy.id]);
  });
});

describe('LocalStore / 项目绑定与降级', () => {
  it('clearProjectAssignment 只降级指定项目的会话并返回行数', () => {
    const { store } = createTestStore(LocalStore);
    const a = store.createChat('a', null, 'proj-1');
    const b = store.createChat('b', null, 'proj-1');
    const other = store.createChat('c', null, 'proj-2');

    expect(store.clearProjectAssignment('proj-1')).toBe(2);
    expect(store.getChat(a.id)?.projectId).toBeNull();
    expect(store.getChat(b.id)?.projectId).toBeNull();
    expect(store.getChat(other.id)?.projectId).toBe('proj-2');
    // 会话本身不删。
    expect(store.listChats().total).toBe(3);
    // 再清一次是 0 行（幂等）。
    expect(store.clearProjectAssignment('proj-1')).toBe(0);
  });
});

describe('LocalStore / chat_record 与 turn_active 标记', () => {
  it('setChatRecordId / getChatRecordId 往返与覆盖', () => {
    const { store } = createTestStore(LocalStore);
    const chat = store.createChat('x');
    expect(store.getChatRecordId(chat.id)).toBeNull();
    store.setChatRecordId(chat.id, 'record-1');
    expect(store.getChatRecordId(chat.id)).toBe('record-1');
    store.setChatRecordId(chat.id, 'record-2');
    expect(store.getChatRecordId(chat.id)).toBe('record-2');
  });

  it('turn_active：set 后可读 startedAt，clear 后归 null', () => {
    vi.useFakeTimers();
    const { store } = createTestStore(LocalStore);
    const chat = store.createChat('x');
    expect(store.getTurnActive(chat.id)).toBeNull();
    vi.setSystemTime('2026-01-01T08:00:00.000Z');
    store.setTurnActive(chat.id);
    expect(store.getTurnActive(chat.id)).toEqual({ startedAt: '2026-01-01T08:00:00.000Z' });
    store.clearTurnActive(chat.id);
    expect(store.getTurnActive(chat.id)).toBeNull();
  });

  it('turn_active 存了坏 JSON 时读为 null（崩溃标记不容错就会误报）', () => {
    const { store } = createTestStore(LocalStore);
    store
      .getPackDb()
      .prepare(`INSERT INTO settings_kv (key, value) VALUES (?, ?)`)
      .run('turn_active:chat-bad', 'not-json');
    expect(store.getTurnActive('chat-bad')).toBeNull();
    // startedAt 不是字符串同样归 null。
    store
      .getPackDb()
      .prepare(`UPDATE settings_kv SET value = ? WHERE key = ?`)
      .run('{"startedAt": 123}', 'turn_active:chat-bad');
    expect(store.getTurnActive('chat-bad')).toBeNull();
  });
});
