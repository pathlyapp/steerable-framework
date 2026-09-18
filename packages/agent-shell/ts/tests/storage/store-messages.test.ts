/**
 * LocalStore 消息（chat_messages）集成测试：真实 SQLite。
 *
 * 覆盖：addMessage 往返与对会话 updated_at 的触碰、listMessages 的
 * 倒序/截断/clamp、同毫秒写入的 rowid 决胜、getMessage 的 chat 隔离、
 * deleteMessagesFrom 的「从这条起往后全删」语义、replaceChatMessages 的
 * 分支投影替换、以及外键约束（孤儿消息拒绝写入）。
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

describe('LocalStore / 消息写入与读取', () => {
  it('addMessage 往返：字段齐全，metadata 缺省为 null', async () => {
    const { store } = await createTestStore();
    const chat = await store.createChat('x');
    const msg = await store.addMessage(chat.id, 'user', '你好');
    expect(msg.id).toMatch(/^[0-9a-f]{8}-[0-9a-f-]{27}$/);
    expect(msg.chatId).toBe(chat.id);
    expect(msg.role).toBe('user');
    expect(msg.content).toBe('你好');
    expect(msg.messageMetadata).toBeNull();
    expect(await store.getMessage(chat.id, msg.id)).toEqual(msg);
  });

  it('addMessage 透传 messageMetadata 字符串（不做 JSON 解析）', async () => {
    const { store } = await createTestStore();
    const chat = await store.createChat('x');
    const meta = JSON.stringify({ completionStatus: 'completed', model: 'm' });
    const msg = await store.addMessage(chat.id, 'assistant', '答', meta);
    expect(msg.messageMetadata).toBe(meta);
  });

  it('patchMessageMetadata 浅合并 JSON，损坏元数据从空对象重写', async () => {
    const { store } = await createTestStore();
    const chat = await store.createChat('x');
    const msg = await store.addMessage(
      chat.id,
      'assistant',
      '答',
      JSON.stringify({ completionStatus: 'completed' }),
    );
    const patched = await store.patchMessageMetadata(chat.id, msg.id, {
      suggestedReplies: ['a', 'b', 'c'],
    });
    expect(JSON.parse(patched!.messageMetadata!)).toEqual({
      completionStatus: 'completed',
      suggestedReplies: ['a', 'b', 'c'],
    });
    expect(await store.patchMessageMetadata(chat.id, 'ghost', { x: 1 })).toBeNull();

    const broken = await store.addMessage(chat.id, 'assistant', '坏', 'not-json');
    const rewritten = await store.patchMessageMetadata(chat.id, broken.id, { suggestedReplies: ['x'] });
    expect(JSON.parse(rewritten!.messageMetadata!)).toEqual({ suggestedReplies: ['x'] });
  });

  it('addMessage 刷新所属会话的 updated_at', async () => {
    vi.useFakeTimers();
    const { store } = await createTestStore();
    vi.setSystemTime('2026-01-01T00:00:00.000Z');
    const chat = await store.createChat('x');
    vi.setSystemTime('2026-01-01T00:00:09.000Z');
    await store.addMessage(chat.id, 'user', 'hi');
    expect((await store.getChat(chat.id))?.updatedAt).toBe('2026-01-01T00:00:09.000Z');
  });

  it('向不存在的会话写消息被外键约束拒绝', async () => {
    const { store } = await createTestStore();
    // foreign_keys = ON 是构造时显式打开的；孤儿消息必须炸在写入侧。
    await expect(store.addMessage('ghost', 'user', 'hi')).rejects.toThrow(/FOREIGN KEY/);
  });

  it('getMessage 按 chat 隔离：同 id 在别的会话不可见', async () => {
    const { store } = await createTestStore();
    const a = await store.createChat('a');
    const b = await store.createChat('b');
    const msg = await store.addMessage(a.id, 'user', 'hi');
    expect(await store.getMessage(b.id, msg.id)).toBeNull();
    expect(await store.getMessage(a.id, 'ghost')).toBeNull();
  });
});

describe('LocalStore / listMessages 排序与截断', () => {
  it('新→旧返回（MESSAGE_ORDER_DESC），调用方自行反转成 transcript 序', async () => {
    vi.useFakeTimers();
    const { store } = await createTestStore();
    const chat = await store.createChat('x');
    const ids: string[] = [];
    for (let i = 0; i < 3; i += 1) {
      vi.setSystemTime(`2026-01-01T00:00:0${i}.000Z`);
      ids.push((await store.addMessage(chat.id, 'user', `m${i}`)).id);
    }
    const listed = await store.listMessages(chat.id);
    expect(listed.map((m) => m.id)).toEqual([ids[2], ids[1], ids[0]]);
    // 反转为 transcript 序后等于写入序。
    expect([...listed].reverse().map((m) => m.id)).toEqual(ids);
  });

  it('同毫秒连写靠 rowid 决胜：读出顺序依然稳定', async () => {
    vi.useFakeTimers();
    const { store } = await createTestStore();
    const chat = await store.createChat('x');
    // 钉死时间：三条消息 created_at 完全相同（模拟 mid-turn steer 的
    // 亚毫秒写入），次序只能由 rowid DESC 决定。
    vi.setSystemTime('2026-01-01T00:00:00.000Z');
    const ids = await Promise.all(
      [0, 1, 2].map(async (i) => (await store.addMessage(chat.id, 'user', `m${i}`)).id),
    );
    expect((await store.listMessages(chat.id)).map((m) => m.id)).toEqual([ids[2], ids[1], ids[0]]);
  });

  it('limit 截断与 clamp：NaN 回默认 200，0 夹到 1', async () => {
    const { store } = await createTestStore();
    const chat = await store.createChat('x');
    for (let i = 0; i < 3; i += 1) await store.addMessage(chat.id, 'user', `m${i}`);
    expect(await store.listMessages(chat.id, 2)).toHaveLength(2);
    expect(await store.listMessages(chat.id, Number.NaN)).toHaveLength(3);
    expect(await store.listMessages(chat.id, 0)).toHaveLength(1);
    expect(await store.listMessages('ghost')).toEqual([]);
  });
});

describe('LocalStore / deleteMessagesFrom（regenerate 截断）', () => {
  it('删掉目标消息及其后的全部消息，之前的保留', async () => {
    vi.useFakeTimers();
    const { store } = await createTestStore();
    const chat = await store.createChat('x');
    const ids: string[] = [];
    for (let i = 0; i < 5; i += 1) {
      vi.setSystemTime(`2026-01-01T00:00:0${i}.000Z`);
      ids.push((await store.addMessage(chat.id, i % 2 ? 'assistant' : 'user', `m${i}`)).id);
    }
    // 从第 3 条（ids[2]）起删：被重新生成的回复及其后全部截掉。
    expect(await store.deleteMessagesFrom(chat.id, ids[2])).toBe(3);
    expect((await store.listMessages(chat.id)).map((m) => m.id)).toEqual([ids[1], ids[0]]);
  });

  it('未知 messageId 不删任何行；不波及其他会话', async () => {
    const { store } = await createTestStore();
    const a = await store.createChat('a');
    const b = await store.createChat('b');
    const ma = await store.addMessage(a.id, 'user', 'a1');
    await store.addMessage(b.id, 'user', 'b1');
    // 子查询对未知 id 返回 NULL，整条件不成立。
    expect(await store.deleteMessagesFrom(a.id, 'ghost')).toBe(0);
    // b 会话里的同 id 消息不构成匹配（子查询带 chat_id 条件）。
    expect(await store.deleteMessagesFrom(b.id, ma.id)).toBe(0);
    expect(await store.listMessages(a.id)).toHaveLength(1);
    expect(await store.listMessages(b.id)).toHaveLength(1);
  });
});

describe('LocalStore / replaceChatMessages（分支投影）', () => {
  it('整体替换消息列表，合成时间戳保持输入序，会话 updated_at 刷新', async () => {
    vi.useFakeTimers();
    const { store } = await createTestStore();
    vi.setSystemTime('2026-01-01T00:00:00.000Z');
    const chat = await store.createChat('x');
    await store.addMessage(chat.id, 'user', '旧消息1');
    await store.addMessage(chat.id, 'assistant', '旧消息2');

    vi.setSystemTime('2026-01-01T01:00:00.000Z');
    const base = Date.now();
    await store.replaceChatMessages(chat.id, [
      { role: 'user', content: '投影1' },
      { role: 'assistant', content: '投影2' },
      { role: 'user', content: '投影3' },
    ]);

    const listed = await store.listMessages(chat.id);
    expect(listed.map((m) => m.content)).toEqual(['投影3', '投影2', '投影1']);
    // 合成时间戳 base+i：反转后顺序即输入序，且逐一递增 1ms。
    const asc = [...listed].reverse();
    expect(asc.map((m) => m.createdAt)).toEqual([0, 1, 2].map((i) => new Date(base + i).toISOString()));
    // metadata 一律置 NULL（投影不带 per-message 元数据）。
    expect(asc.every((m) => m.messageMetadata === null)).toBe(true);
    // updated_at = base + length。
    expect((await store.getChat(chat.id))?.updatedAt).toBe(new Date(base + 3).toISOString());
  });

  it('空数组投影 = 清空消息', async () => {
    const { store } = await createTestStore();
    const chat = await store.createChat('x');
    await store.addMessage(chat.id, 'user', 'hi');
    await store.replaceChatMessages(chat.id, []);
    expect(await store.listMessages(chat.id)).toEqual([]);
    expect(await store.chatHasMessages(chat.id)).toBe(false);
  });
});
