/**
 * SystemPromptBuilder for the local desktop agent.
 *
 * Assembles a system prompt by loading skill modules from
 * `src/local-backend/skills/<dir>/SKILL.md` and concatenating their bodies
 * in directory-name order. Skills can be filtered by runtime conditions
 * (e.g. `tool:<tool_name>`) and trimmed by character budget when too
 * large (lowest-priority skills are dropped first).
 *
 * This is the TS counterpart of
 * 上游 Python 服务的 SystemPromptBuilder。
 */

import path from 'node:path';
import { loadSkills, getSkillsDir, type SkillModule } from './skill-loader.js';
import { isSkillPinned } from './agent-capability.js';
import { getBrand } from '../brand.js';

const FALLBACK_PROMPT = (() => {
  const brand = getBrand();
  return [
    '# 角色',
    '',
    `你是 ${brand.agentName}，${brand.tagline}，运行在用户自己的 Windows / macOS / Linux 机器上。`,
    '通过工具直接操作用户的本地环境（shell、文件、当前产品安装的场景工具等）。用中文回复。',
    '不要凭空编造任何工具返回值。',
  ].join('\n');
})();

const DEFAULT_CHAR_BUDGET = 60_000;

export interface BuildPromptOptions {
  /** Runtime conditions e.g. `tool:<tool_name>`, `has-tools`. */
  conditions?: Iterable<string>;
  /** Per-tool names available this turn; auto-derives `tool:<name>` conditions. */
  toolNames?: Iterable<string>;
  /** Optional persona preamble (`AgentProfile.role_prompt`). Rendered first. */
  personaPreamble?: string | null;
  /** Optional per-chat custom system prompt; rendered after persona. */
  chatSystemPrompt?: string | null;
  /** Optional realityCheck suffix (e.g. "本轮可用工具…"). Rendered last. */
  realityCheckSuffix?: string;
  /** Character budget (rough proxy for tokens). Defaults to {@link DEFAULT_CHAR_BUDGET}. */
  charBudget?: number;
  /** Inject `has-tools` automatically when toolNames is non-empty. Default true. */
  autoHasTools?: boolean;
  /**
   * 用户通过 "/mcp__srv__tool" 显式指定的 MCP 工具。渲染为末尾指令模块，
   * 配合首轮 tool_choice='required' 保证真的发起调用。
   */
  forcedMcpTool?: ForcedMcpTool;
  /** Override skills directory (mainly for tests; bypasses built-in + user dirs). */
  skillsDir?: string;
  /** Ignore all loading conditions and load all skills. */
  ignoreConditions?: boolean;
  /** Skill names to always drop, even under `ignoreConditions`. */
  excludeSkillNames?: Iterable<string>;
  /**
   * A6 分层披露:true 时只注入 eager 层技能正文;catalog 层由 sidecar 以
   * 「目录 + skill 工具按需加载」提供。CoreLoop 路径恒为 true。
   */
  eagerOnly?: boolean;
  /**
   * 智能体勾选的技能别名（`skillIds`）。命中的技能正文无条件进系统提示词:
   * 既绕过触发条件,也绕过 `eagerOnly` 的 catalog 层过滤。`excludeSkillNames`
   * 仍然优先——模式级排除（plan 模式的执行类技能）不因勾选而失效。
   */
  pinnedSkillNames?: Iterable<string>;
}

export interface BuiltPrompt {
  prompt: string;
  modules: SkillModule[];
  droppedModules: string[];
  conditions: string[];
}

/** 显式指定的 MCP 工具（"/" 选择器 → cleanUserMessage → 此处）。 */
export interface ForcedMcpTool {
  /** 完整一等工具名，如 mcp__demo_local__add。 */
  token: string;
  toolName: string;
  serverName: string;
  description: string;
  /** false = 服务未启用/未连接或工具不存在——渲染"不可用"指令而非强制调用。 */
  available: boolean;
}

/**
 * Derive condition strings from the set of tool names available this turn.
 *
 *   ['some_pack_tool', 'local_exec_shell']
 *   →  ['tool:some_pack_tool', 'tool:local_exec_shell']
 */
export function conditionsFromTools(
  toolNames: Iterable<string>,
  autoHasTools = true,
): Set<string> {
  const out = new Set<string>();
  let hasAny = false;
  for (const name of toolNames) {
    if (!name) continue;
    out.add(`tool:${name}`);
    hasAny = true;
  }
  if (autoHasTools && hasAny) out.add('has-tools');
  return out;
}

/**
 * Assemble the final system prompt:
 *
 *   [persona preamble]?       (agent role / DSL)
 *   [chat-level prompt]?      (per-chat custom override)
 *   [skill 1 body]
 *   ...
 *   [skill N body]
 *   [reality check]?          (turn-specific tool list / pollution warning)
 */
export async function buildSystemPrompt(options: BuildPromptOptions = {}): Promise<BuiltPrompt> {
  const conditions = new Set<string>(options.conditions ?? []);
  if (options.toolNames) {
    for (const c of conditionsFromTools(options.toolNames, options.autoHasTools ?? true)) {
      conditions.add(c);
    }
  }

  let modules = await loadSkills({
    conditions,
    ignoreConditions: options.ignoreConditions,
    excludeSkillNames: options.excludeSkillNames,
    skillsDir: options.skillsDir,
  });
  const droppedModules: string[] = [];

  // 智能体勾选的技能:再取一次全量(无视条件)捞出勾选项并并入。追加在条件
  // 命中的模块之后——recency 更强,且这些是用户明确为该智能体选的。
  const pinnedAliases = Array.from(options.pinnedSkillNames ?? []);
  const pinnedDirNames = new Set<string>();
  if (pinnedAliases.length > 0) {
    const all = await loadSkills({
      ignoreConditions: true,
      excludeSkillNames: options.excludeSkillNames,
      skillsDir: options.skillsDir,
    });
    const present = new Set(modules.map((m) => m.dirName));
    for (const module of all) {
      if (!isSkillPinned(module, pinnedAliases)) continue;
      pinnedDirNames.add(module.dirName);
      if (!present.has(module.dirName)) modules.push(module);
    }
  }

  // A6 分层披露:catalog 层技能的正文不再进系统提示词——sidecar 注入目录
  // 清单,模型调 `skill` 工具按需加载(用户显式 "/技能名" 走 router 的一次性
  // 消息注入,也不经过这里)。勾选的技能是例外:用户为这个智能体点名了,正文
  // 直接常驻,不必等模型自己去目录里找。
  if (options.eagerOnly) {
    modules = modules.filter(
      (m) => m.layer === 'eager' || pinnedDirNames.has(m.dirName),
    );
  }

  if (modules.length === 0) {
    return {
      prompt: assembleFinal({
        personaPreamble: options.personaPreamble ?? '',
        chatSystemPrompt: options.chatSystemPrompt ?? '',
        body: FALLBACK_PROMPT,
        realityCheckSuffix: options.realityCheckSuffix ?? '',
      }),
      modules,
      droppedModules,
      conditions: Array.from(conditions),
    };
  }

  const charBudget = options.charBudget ?? DEFAULT_CHAR_BUDGET;
  modules = applyBudget(modules, charBudget, droppedModules);

  // 用户显式指定的 MCP 工具（"/mcp__srv__tool" 触发）：合成一个指令模块放到
  // 最末尾（recency 最强）。在 applyBudget 之后追加，天然免疫预算裁剪。与
  // tool-router 的动态工具命名 mcp__<serverKey>__<toolName> 一致，首轮
  // tool_choice='required' 由 router 的回合路由负责施加。
  if (options.forcedMcpTool) {
    modules.push(buildForcedMcpToolModule(options.forcedMcpTool));
  }

  // 品牌占位符（{agentName} 等）由产品注入的品牌渲染——shell 技能正文
  // 保持产品中立（3.2 中性化，见 skills/00-identity）。
  const body = modules.map((m) => renderSkillContent(m, brandSkillVars())).join('\n\n');

  return {
    prompt: assembleFinal({
      personaPreamble: options.personaPreamble ?? '',
      chatSystemPrompt: options.chatSystemPrompt ?? '',
      body,
      realityCheckSuffix: options.realityCheckSuffix ?? '',
    }),
    modules,
    droppedModules,
    conditions: Array.from(conditions),
  };
}

/** 显式指定 MCP 工具的末尾指令模块（合成 SkillModule，不来自 skill-loader）。 */
function buildForcedMcpToolModule(t: ForcedMcpTool): SkillModule {
  const content = t.available
    ? [
        `# ⚡ 本轮指定 MCP 工具（最高优先级）`,
        ``,
        `用户通过 \`/${t.token}\` 显式指定了 MCP 工具 \`${t.toolName}\`` +
          `${t.serverName ? `（来自服务「${t.serverName}」）` : ''}` +
          `${t.description ? `，功能：${t.description}` : ''}。本轮规则：`,
        `1. 第一步必须调用工具 \`${t.token}\`（发起 tool_call），参数根据用户问题构造；`,
        `2. 回答必须以该工具本次返回的数据为准——不得编造结果，也不得用历史/缓存数据冒充本次调用；`,
        `3. 若工具调用失败，如实报告错误信息，不要假装执行成功；`,
        `4. 用户问题中超出该工具能力的部分，可用其他工具或知识补充，但要明确区分来源。`,
      ].join('\n')
    : [
        `# ⚡ 本轮指定 MCP 工具（当前不可用）`,
        ``,
        `用户通过 \`/${t.token}\` 显式指定的 MCP 工具当前不可用（服务未启用、未连接成功或工具不存在）。本轮规则：`,
        `1. **不要**假装调用该工具，也不要编造它的返回结果；`,
        `2. 直接告知用户该工具不可用，建议其在「设置 → MCP 服务」中检查该服务的启用状态并重试「测试连接」；`,
        `3. 其余可回答的部分用中文正常回答。`,
      ].join('\n');
  return {
    name: `forced-mcp-tool`,
    displayName: '',
    description: t.description,
    priority: 999,
    tags: [],
    conditions: [],
    match: 'any',
    layer: 'eager',
    modelInvocable: true,
    content,
    dirName: `forced-mcp:${t.token}`,
    skillsDir: '',
  };
}

/**
 * 品牌占位符变量：技能正文里的 `{agentName}` / `{brandDisplayName}` /
 * `{brandTagline}` 渲染为当前产品注入的品牌（brand.ts；未注入 = shell
 * 中性默认）。eager 系统提示词路径统一注入；包技能同理可用。
 */
export function brandSkillVars(): Record<string, string> {
  const brand = getBrand();
  return {
    agentName: brand.agentName,
    brandDisplayName: brand.displayName,
    brandTagline: brand.tagline,
  };
}

/** 技能正文渲染:把 `{scripts}` 及额外占位符解析为绝对路径。 */
export function renderSkillContent(
  m: SkillModule,
  vars?: Record<string, string>,
): string {
  const scriptsPath = path.join(m.skillsDir, m.dirName, 'scripts');
  // Replace {scripts}/ or {scripts}\ with the correct path to ensure uniform slashes
  let content = m.content;
  content = content.replace(/\{scripts\}[\/\\]/g, scriptsPath + path.sep);
  content = content.replace(/\{scripts\}/g, scriptsPath);
  if (vars) {
    for (const [key, value] of Object.entries(vars)) {
      content = content.replaceAll(`{${key}}`, value);
    }
  }
  return content;
}

/** 用户显式指定技能的优先级指令头，渲染在该技能内容之前。 */
export function buildForcedSkillHeader(m: SkillModule): string {
  const label = m.displayName || m.name;
  return [
    `# ⚡ 本轮指定技能（最高优先级）`,
    ``,
    `用户通过 \`/${label}\` 显式指定了本技能。本轮规则：`,
    `1. 必须优先、严格地遵循本技能的流程、约束与工具选择来完成任务；`,
    `2. 本技能与其他内嵌技能冲突时，**一律以本技能为准**；`,
    `3. 除非本技能明确要求，**不要**改用其他工具来完成本技能已覆盖的任务；`,
    `4. 只有本技能未覆盖的辅助步骤，才允许使用其他工具补充。`,
  ].join('\n');
}

/**
 * "/技能名" 触发的一次性注入文本(A6):指令头 + 技能正文,由 router 并入
 * 本轮最后一条用户消息——不再塞系统提示词尾部(系统提示词保持稳定,是
 * prompt cache 的净收益;历史消息里也不会残留技能正文)。
 */
export function buildForcedSkillMessage(
  m: SkillModule,
  vars?: Record<string, string>,
): string {
  return `${buildForcedSkillHeader(m)}\n\n${renderSkillContent(m, vars)}`;
}

function applyBudget(
  modules: SkillModule[],
  charBudget: number,
  droppedModules: string[],
): SkillModule[] {
  const total = modules.reduce((sum, m) => sum + m.content.length, 0);
  if (total <= charBudget) return modules;

  const byPriorityAsc = [...modules].sort((a, b) => a.priority - b.priority);
  const dropped = new Set<string>();
  let excess = total - charBudget;
  for (const m of byPriorityAsc) {
    if (excess <= 0) break;
    dropped.add(m.name);
    excess -= m.content.length;
    droppedModules.push(m.name);
  }
  return modules.filter((m) => !dropped.has(m.name));
}

function assembleFinal(parts: {
  personaPreamble: string;
  chatSystemPrompt: string;
  body: string;
  realityCheckSuffix: string;
}): string {
  const sections: string[] = [];
  if (parts.personaPreamble.trim()) sections.push(parts.personaPreamble.trim());
  if (parts.chatSystemPrompt.trim()) {
    sections.push(`# 本次对话补充设定\n\n${parts.chatSystemPrompt.trim()}`);
  }
  sections.push(parts.body.trim());
  if (parts.realityCheckSuffix.trim()) sections.push(parts.realityCheckSuffix.trim());
  return sections.join('\n\n');
}
