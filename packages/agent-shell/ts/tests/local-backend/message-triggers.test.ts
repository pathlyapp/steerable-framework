import { describe, expect, it } from 'vitest';
import {
  parseUserMessageTriggers,
  type TriggerResolvers,
} from '../../src/local-backend/message-triggers.js';

// 测试夹具：两个已知技能 + 一个已注册 MCP 工具（注意注册 token 含大写，
// 用来验证 resolveMcpToolToken 的规范化作用）。
const KNOWN_SKILLS = ['read-workspace', '智能测井处理链'];
const REGISTERED_MCP_TOKEN = 'mcp__demo_local__add';

const resolvers: TriggerResolvers = {
  isKnownSkill: (name) => KNOWN_SKILLS.includes(name),
  resolveMcpToolToken: (token) =>
    token.toLowerCase() === REGISTERED_MCP_TOKEN.toLowerCase() ? REGISTERED_MCP_TOKEN : null,
};

describe('message-triggers / 行首技能触发（原有行为保持不变）', () => {
  it('行首 "/skill 剩余问题" → 技能触发 + 剩余文本', () => {
    const r = parseUserMessageTriggers('/read-workspace 看看工区里有啥', resolvers);
    expect(r.skillName).toBe('read-workspace');
    expect(r.mcpToolToken).toBeUndefined();
    expect(r.cleanText).toBe('【指定本地技能: read-workspace】看看工区里有啥');
  });

  it('行首只有 "/skill" 时补演示提示语', () => {
    const r = parseUserMessageTriggers('/read-workspace', resolvers);
    expect(r.skillName).toBe('read-workspace');
    expect(r.cleanText).toContain('请启用并展示技能');
  });
});

describe('message-triggers / 行首 MCP 工具触发', () => {
  it('"/mcp__demo_local__add 算 1+2" → MCP 触发，token 经 resolver 规范化', () => {
    const r = parseUserMessageTriggers('/MCP__DEMO_LOCAL__ADD 算 1+2', resolvers);
    expect(r.mcpToolToken).toBe(REGISTERED_MCP_TOKEN); // 大小写被规范成注册名
    expect(r.skillName).toBeUndefined();
    expect(r.cleanText).toBe(`【指定MCP工具: ${REGISTERED_MCP_TOKEN}】算 1+2`);
  });

  it('只有 token 没有问题文本时补"请调用该工具"', () => {
    const r = parseUserMessageTriggers('/mcp__demo_local__add', resolvers);
    expect(r.mcpToolToken).toBe(REGISTERED_MCP_TOKEN);
    expect(r.cleanText).toContain('请调用该工具并展示其返回结果');
  });

  it('未注册的 mcp__ token 仍按 MCP 触发处理（由下游渲染"不可用"指令）', () => {
    const r = parseUserMessageTriggers('/mcp__ghost__nope 你好', resolvers);
    expect(r.mcpToolToken).toBe('mcp__ghost__nope'); // 保留用户输入原样
    expect(r.skillName).toBeUndefined();
  });
});

describe('message-triggers / 句中触发', () => {
  it('句中已知技能 token 生效并抠除', () => {
    const r = parseUserMessageTriggers('帮我 /read-workspace 然后总结一下', resolvers);
    expect(r.skillName).toBe('read-workspace');
    expect(r.cleanText).toBe('【指定本地技能: read-workspace】帮我 然后总结一下');
  });

  it('句中已注册 MCP token 生效并抠除（大小写规范化）', () => {
    const r = parseUserMessageTriggers('用 /mcp__demo_local__ADD 算一下', resolvers);
    expect(r.mcpToolToken).toBe(REGISTERED_MCP_TOKEN);
    expect(r.cleanText).toBe(`【指定MCP工具: ${REGISTERED_MCP_TOKEN}】用 算一下`);
  });

  it('句中未知技能/未注册 MCP token 不生效（防路径误伤）', () => {
    const r1 = parseUserMessageTriggers('看一下 /unknown-skill 的内容', resolvers);
    expect(r1.skillName).toBeUndefined();
    expect(r1.mcpToolToken).toBeUndefined();

    const r2 = parseUserMessageTriggers('执行 /mcp__ghost__nope 试试', resolvers);
    expect(r2.mcpToolToken).toBeUndefined();
    expect(r2.cleanText).toBe('执行 /mcp__ghost__nope 试试');
  });

  it('路径与 URL 里的斜杠永远不触发', () => {
    for (const text of ['打开 C:/Users/test 目录', '访问 https://a.com/mcp__demo_local__add']) {
      const r = parseUserMessageTriggers(text, resolvers);
      expect(r.skillName).toBeUndefined();
      expect(r.mcpToolToken).toBeUndefined();
      expect(r.cleanText).toBe(text);
    }
  });
});
