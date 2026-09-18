/**
 * LocalStore 智能体（chat_agents）集成测试：真实 SQLite。
 *
 * 覆盖：构造时的内置种子（电脑操作员 / 智能助手）、listChatAgents 的
 * 归档过滤与排序、createChatAgent 的默认值与全字段往返、
 * updateChatAgent 的 undefined 过滤与显式置空、archiveChatAgent。
 */
import { afterEach, describe, expect, it } from 'vitest';

import {
  cleanupTestStores,
  createTestStore,
  loadStorageModule,
} from './local-store-testkit.js';

const { LocalStore } = await loadStorageModule();

afterEach(() => {
  cleanupTestStores();
});

describe('LocalStore / 内置智能体种子', () => {
  it('新库构造后种子出电脑操作员与智能助手', () => {
    const { store } = createTestStore(LocalStore);
    const local = store.getChatAgent('local-assistant');
    expect(local).toMatchObject({
      name: '电脑操作员',
      rolePrompt: '你是 **电脑操作员**，本地离线助手，回答时清晰、可执行。',
      isBuiltin: true,
      isArchived: false,
      loadAllSkills: false,
      sortOrder: 0,
    });
    const allRound = store.getChatAgent('all-round-assistant');
    expect(allRound).toMatchObject({
      name: '智能助手',
      isBuiltin: true,
      isArchived: false,
      // 智能助手的定位就是无条件全量加载技能。
      loadAllSkills: true,
      sortOrder: 2,
    });
    // 无场景包种子的中性 shell：列表就是这两个内置，按 sort_order 升序。
    expect(store.listChatAgents().map((a) => a.id)).toEqual([
      'local-assistant',
      'all-round-assistant',
    ]);
  });
});

describe('LocalStore / createChatAgent', () => {
  it('最小输入（仅 name）落库为全套默认值', () => {
    const { store } = createTestStore(LocalStore);
    const agent = store.createChatAgent({ name: '最小智能体' });
    expect(agent).toMatchObject({
      name: '最小智能体',
      slug: null,
      icon: null,
      color: null,
      description: null,
      rolePrompt: null,
      forbiddenPrompt: null,
      skillIds: [],
      toolPolicy: { mode: 'all', tools: [] },
      allowExternalSkills: false,
      loadAllSkills: false,
      isBuiltin: false,
      isArchived: false,
      sortOrder: 0,
    });
    expect(agent.id).toMatch(/^[0-9a-f]{8}-[0-9a-f-]{27}$/);
    expect(agent.createdAt).toBe(agent.updatedAt);
  });

  it('全字段往返：skillIds / toolPolicy / 提示词等 JSON 列读回一致', () => {
    const { store } = createTestStore(LocalStore);
    const agent = store.createChatAgent({
      id: 'custom-agent',
      slug: 'custom',
      name: '全字段',
      icon: 'Wrench',
      color: '#000000',
      description: '描述',
      rolePrompt: '角色提示',
      forbiddenPrompt: '禁止事项',
      skillIds: ['skill-a', 'skill-b'],
      toolPolicy: { mode: 'allowlist', tools: ['local_exec_shell'] },
      allowExternalSkills: true,
      loadAllSkills: true,
      sortOrder: 5,
    });
    expect(store.getChatAgent('custom-agent')).toEqual(agent);
    expect(agent).toMatchObject({
      slug: 'custom',
      skillIds: ['skill-a', 'skill-b'],
      toolPolicy: { mode: 'allowlist', tools: ['local_exec_shell'] },
      allowExternalSkills: true,
      loadAllSkills: true,
      sortOrder: 5,
    });
  });
});

describe('LocalStore / updateChatAgent 与归档', () => {
  it('部分更新只动传入字段；undefined 不抹掉现有值', () => {
    const { store } = createTestStore(LocalStore);
    const agent = store.createChatAgent({
      name: '旧名',
      icon: 'Star',
      skillIds: ['s1'],
      sortOrder: 3,
    });
    const updated = store.updateChatAgent(agent.id, { name: '新名', icon: undefined });
    expect(updated?.name).toBe('新名');
    expect(updated?.icon).toBe('Star');
    expect(updated?.skillIds).toEqual(['s1']);
    expect(updated?.sortOrder).toBe(3);
  });

  it('可空列显式传 null 被清空（与 undefined 的「不动」语义相对）', () => {
    const { store } = createTestStore(LocalStore);
    const agent = store.createChatAgent({ name: 'x', icon: 'Star', rolePrompt: '提示' });
    const updated = store.updateChatAgent(agent.id, { icon: null, rolePrompt: null });
    expect(updated?.icon).toBeNull();
    expect(updated?.rolePrompt).toBeNull();
  });

  it('更新 / 归档不存在的智能体返回 null / false', () => {
    const { store } = createTestStore(LocalStore);
    expect(store.updateChatAgent('ghost', { name: 'y' })).toBeNull();
    expect(store.archiveChatAgent('ghost')).toBe(false);
  });

  it('archiveChatAgent 后默认列表隐藏，includeArchived 可见', () => {
    const { store } = createTestStore(LocalStore);
    const agent = store.createChatAgent({ name: '待归档' });
    expect(store.archiveChatAgent(agent.id)).toBe(true);
    expect(store.getChatAgent(agent.id)?.isArchived).toBe(true);
    expect(store.listChatAgents().map((a) => a.id)).not.toContain(agent.id);
    expect(store.listChatAgents(true).map((a) => a.id)).toContain(agent.id);
  });
});

describe('LocalStore / listChatAgents 排序', () => {
  it('按 sort_order 升序，内置种子被自定义小序号挤到后面', () => {
    const { store } = createTestStore(LocalStore);
    const first = store.createChatAgent({ name: '排最前', sortOrder: -1 });
    const last = store.createChatAgent({ name: '排最后', sortOrder: 99 });
    const ids = store.listChatAgents().map((a) => a.id);
    expect(ids[0]).toBe(first.id);
    expect(ids.at(-1)).toBe(last.id);
    // 内置两个仍在中间且相对顺序不变。
    expect(ids.indexOf('local-assistant')).toBeLessThan(ids.indexOf('all-round-assistant'));
  });
});
