/**
 * Slash sources — the local skills + MCP tools behind every `/` token.
 *
 * One source of truth for two surfaces that must agree on "is `/foo` a real
 * tool?":
 *   - `ChatInput` — the `/` suggestion menu and the composer's tool chips.
 *   - `Markdown` — the same chips replayed inside sent message bubbles.
 *
 * The catalog is cached at module scope and shared by every subscriber, so a
 * chat with 50 rendered messages still costs one fetch. `refreshSlashSources`
 * re-reads it (MCP servers and skills can be added/removed in settings while
 * a chat is open) and pushes the result to all live subscribers.
 */

import { useCallback, useEffect, useState } from 'react';
import { getPackHiddenSlashSkills } from '../packs/registry';
import { getElectronBridge, isElectron } from './electron-bridge';

export type SkillItem = {
  /** Skill directory name (e.g. `70-plan-mode`); absent on hand-built items. */
  id?: string;
  name: string;
  displayName?: string;
  description: string;
  isBuiltin?: boolean;
};

export type McpToolItem = {
  /** First-class tool name `mcp__<serverKey>__<toolName>`, inserted verbatim. */
  token: string;
  toolName: string;
  serverKey: string;
  serverName: string;
  description: string;
};

export interface SlashSources {
  skills: SkillItem[];
  mcpTools: McpToolItem[];
}

/**
 * 系统内置的身份/约束/指导类技能：由引擎按条件自动注入系统提示词，不是用户
 * 手动指定执行的业务技能，因此不进 "/" 菜单，也不渲染成工具卡片。技能名与
 * 目录名都登记，两种写法都能命中。
 */
const HIDDEN_SLASH_SKILLS = new Set([
  // 助手身份人设底座
  '00-identity',
  'identity',
  // 计划模式约束（由输入框底部的 Agent/Plan 开关专控）
  '70-plan-mode',
  'plan-mode',
  // 工具调用规范
  '80-tool-usage',
  'tool-usage',
  // 零容忍光说不做
  '81-anti-deferred',
  'anti-deferred',
  'anti-deferred-execution',
  // 零容忍捏造与过时缓存
  '82-data-grounding',
  'data-grounding',
  // 本地命令/文件读写规范
  '85-local-exec',
  'local-exec',
  // 主动编程补位规范
  '86-proactive-coding',
  'proactive-coding',
  // 场景包的隐藏技能由包渲染层声明，
  // 见 getPackHiddenSlashSkills（0.4 起不再硬编码在这里）。
]);

/** True for skills the engine injects on its own; they never reach the user. */
export function isHiddenSlashSkill(skill: SkillItem): boolean {
  const packHidden = getPackHiddenSlashSkills();
  return (
    HIDDEN_SLASH_SKILLS.has(skill.name) ||
    (skill.id !== undefined && HIDDEN_SLASH_SKILLS.has(skill.id)) ||
    packHidden.has(skill.name) ||
    (skill.id !== undefined && packHidden.has(skill.id))
  );
}

/**
 * `/token` occurrences: line start or after whitespace, no inner slash. Keeps
 * paths (`/mnt/c`), URLs and fractions out — same rule the backend's
 * `parseUserMessageTriggers` uses to decide what counts as a trigger.
 */
export const SLASH_TOKEN_PATTERN = /(?:^|\s)(\/[^\s/]+)/g;

export type SlashToolRef = { type: 'skill' | 'mcp'; id: string; label: string };

/**
 * Resolve a bare `/` token name to the tool it names, or null when nothing
 * matches. `mcp__*` resolves even without a catalog entry: the namespace is
 * unambiguous and the server may be temporarily disconnected.
 */
export function resolveSlashTool(
  name: string,
  skills: SkillItem[] = [],
  mcpTools: McpToolItem[] = [],
): SlashToolRef | null {
  const key = name.toLowerCase();

  const mcp = mcpTools.find((t) => t.token.toLowerCase() === key);
  if (mcp) return { type: 'mcp', id: mcp.token, label: name };
  if (key.startsWith('mcp__')) return { type: 'mcp', id: name, label: name };

  const skill = skills.find(
    (s) =>
      s.name.toLowerCase() === key ||
      (s.displayName !== undefined && s.displayName.toLowerCase() === key),
  );
  if (skill && !isHiddenSlashSkill(skill)) {
    return { type: 'skill', id: skill.name, label: name };
  }
  return null;
}

const EMPTY: SlashSources = { skills: [], mcpTools: [] };

let cached: SlashSources = EMPTY;
let inflight: Promise<SlashSources> | null = null;
const listeners = new Set<(sources: SlashSources) => void>();

async function load(): Promise<SlashSources> {
  const bridge = getElectronBridge();
  if (!bridge) return cached;

  let skills = cached.skills;
  let mcpTools = cached.mcpTools;

  try {
    const res = await bridge.localBackend.request<{ skills: SkillItem[] }>({
      method: 'GET',
      path: '/api/v2/chat-agents/skills',
    });
    skills = (res.skills || []).filter((s) => !isHiddenSlashSkill(s));
  } catch (err) {
    console.error('slash-sources failed to fetch skills:', err);
  }

  try {
    const res = await bridge.localBackend.request<{ mcpTools: McpToolItem[] }>({
      method: 'GET',
      path: '/api/v2/chat-agents/mcp-tools',
    });
    mcpTools = res.mcpTools || [];
  } catch (err) {
    // MCP 服务不可用时静默降级——技能候选不受影响
    console.error('slash-sources failed to fetch MCP tools:', err);
  }

  cached = { skills, mcpTools };
  for (const listener of listeners) listener(cached);
  return cached;
}

/** Re-read the catalog. Concurrent callers share one in-flight request. */
export function refreshSlashSources(): Promise<SlashSources> {
  if (!isElectron()) return Promise.resolve(cached);
  if (!inflight) {
    inflight = load().finally(() => {
      inflight = null;
    });
  }
  return inflight;
}

/**
 * Subscribe to the shared catalog. Fetches once on first mount; call
 * `refresh` when the `/` menu opens so a just-connected MCP server shows up.
 */
export function useSlashSources(): SlashSources & {
  refresh: () => Promise<SlashSources>;
} {
  const [sources, setSources] = useState<SlashSources>(cached);

  useEffect(() => {
    listeners.add(setSources);
    void refreshSlashSources();
    return () => {
      listeners.delete(setSources);
    };
  }, []);

  const refresh = useCallback(() => refreshSlashSources(), []);

  return { skills: sources.skills, mcpTools: sources.mcpTools, refresh };
}
