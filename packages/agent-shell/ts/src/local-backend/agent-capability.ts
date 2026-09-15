/**
 * 智能体能力面解析（技能 + 工具准入）。
 *
 * 智能体在「智能体管理」页配置三件事，本模块把它们解析成本轮可执行的形态：
 *
 *   - `skillIds`            勾选的技能 → 正文无条件常驻注入（钉进 eager 层）
 *   - `allowExternalSkills` false 时把勾选集变成硬白名单
 *   - `toolPolicy`          模型可见工具的 allowlist / denylist
 *
 * 全是纯函数（不碰 electron / sqlite / sidecar），因为同一份解析结果要喂给
 * 三个互相独立的执行点，任何一处漂移都会让「限制」变成假象：
 *
 *   1. 系统提示词注入   —— prompt-builder 的 `excludeSkillNames` / `pinnedSkillNames`
 *   2. sidecar 技能目录 —— `SkillTurnContext.exclude`
 *   3. 工具面           —— 每轮 `turnTools`、`tool_search` 结果、反向通道分发复检
 *
 * `@提及` 多个智能体的回合按「最宽松」合并：提及额外专家是为了叠加能力，
 * 不应该反而让对话失去工具或技能（与 personaPreamble 的「同时具备以下 N 个
 * 角色的能力」一致）。
 */

export type AgentToolPolicyMode = 'all' | 'allowlist' | 'denylist';

export interface AgentToolPolicy {
  mode: AgentToolPolicyMode;
  tools: string[];
}

/** 一个智能体的技能/工具配置（{@link ChatAgentRecord} 的相关子集）。 */
export interface AgentCapabilityInput {
  skillIds: string[];
  allowExternalSkills: boolean;
  loadAllSkills: boolean;
  toolPolicy: AgentToolPolicy;
}

/** 本轮生效的能力面（可能是多个被提及智能体的合并结果）。 */
export interface AgentCapability {
  /** 正文无条件注入的技能别名（dirName / name / displayName 皆可）。 */
  pinnedSkills: string[];
  /** false = 只允许 {@link pinnedSkills}（硬白名单）。 */
  allowExternalSkills: boolean;
  /** true = 无视技能触发条件全量加载。 */
  loadAllSkills: boolean;
  toolPolicy: AgentToolPolicy;
}

/** 无智能体绑定（或未配置）的回合：不钉技能、不限制。 */
export const UNRESTRICTED_CAPABILITY: AgentCapability = {
  pinnedSkills: [],
  allowExternalSkills: true,
  loadAllSkills: false,
  toolPolicy: { mode: 'all', tools: [] },
};

/**
 * 归一化外部传入（API payload / 旧库行）的工具策略。
 *
 * 空 `tools` 一律降级为 `all`：denylist 空集本就等于不限制，而「allowlist
 * 空集」会让智能体一个工具都调不到——那不是任何人想要的配置，把它当作未
 * 配置处理，避免用户在 UI 上手滑就把智能体废掉。
 *
 * @param raw 任意来源的候选值。
 * @returns 合法的 {@link AgentToolPolicy}；无法识别时为 `{ mode: 'all', tools: [] }`。
 */
export function normalizeToolPolicy(raw: unknown): AgentToolPolicy {
  const source = (raw ?? {}) as { mode?: unknown; tools?: unknown };
  const tools = Array.isArray(source.tools)
    ? Array.from(
        new Set(
          source.tools
            .map((item) => String(item).trim())
            .filter((item) => item.length > 0),
        ),
      )
    : [];
  const mode: AgentToolPolicyMode =
    source.mode === 'allowlist' || source.mode === 'denylist' ? source.mode : 'all';
  if (mode === 'all' || tools.length === 0) return { mode: 'all', tools: [] };
  return { mode, tools };
}

/**
 * 单个工具是否被策略放行。
 *
 * @param policy 归一化后的策略。
 * @param toolName 一等工具名（含 `mcp__<server>__<tool>` 全名）。
 * @returns 放行为 true。
 */
export function isToolAllowed(policy: AgentToolPolicy, toolName: string): boolean {
  switch (policy.mode) {
    case 'all':
      return true;
    case 'allowlist':
      return policy.tools.includes(toolName);
    case 'denylist':
      return !policy.tools.includes(toolName);
  }
}

/**
 * 按策略过滤一批工具 schema（保持入参顺序）。
 *
 * @param schemas 待过滤的 schema 列表。
 * @param policy 归一化后的策略。
 * @returns 放行的 schema 子集；`mode: 'all'` 时原样返回。
 */
export function filterToolsByPolicy<T extends { name: string }>(
  schemas: readonly T[],
  policy: AgentToolPolicy,
): T[] {
  if (policy.mode === 'all') return [...schemas];
  return schemas.filter((schema) => isToolAllowed(policy, schema.name));
}

/**
 * 合并两个工具策略，取「任一放行即放行」。
 *
 * allowlist A 与 denylist D 混合时结果是 `denylist(D \ A)`：被拒的只剩
 * 「D 里拒了、A 里也没放行」的工具。
 */
function mergeToolPolicies(a: AgentToolPolicy, b: AgentToolPolicy): AgentToolPolicy {
  if (a.mode === 'all' || b.mode === 'all') return { mode: 'all', tools: [] };
  if (a.mode === 'allowlist' && b.mode === 'allowlist') {
    return normalizeToolPolicy({
      mode: 'allowlist',
      tools: [...a.tools, ...b.tools],
    });
  }
  if (a.mode === 'denylist' && b.mode === 'denylist') {
    const both = a.tools.filter((tool) => b.tools.includes(tool));
    return normalizeToolPolicy({ mode: 'denylist', tools: both });
  }
  const [allow, deny] = a.mode === 'allowlist' ? [a, b] : [b, a];
  const stillDenied = deny.tools.filter((tool) => !allow.tools.includes(tool));
  return normalizeToolPolicy({ mode: 'denylist', tools: stillDenied });
}

/**
 * 合并本轮涉及的全部智能体的能力面，取最宽松。
 *
 * @param agents 按提及顺序排列的智能体配置；空数组表示无绑定。
 * @returns 本轮生效的能力面；空数组时为 {@link UNRESTRICTED_CAPABILITY}。
 */
export function mergeAgentCapabilities(
  agents: readonly AgentCapabilityInput[],
): AgentCapability {
  if (agents.length === 0) return UNRESTRICTED_CAPABILITY;
  const pinned = new Set<string>();
  let allowExternalSkills = false;
  let loadAllSkills = false;
  // 折叠起点取最严（allowlist 空集经归一化即 `all`，所以这里显式从第一个
  // 智能体的策略起步，而不是造一个「空允许集」的中间值）。
  let toolPolicy = normalizeToolPolicy(agents[0].toolPolicy);
  for (const agent of agents) {
    for (const id of agent.skillIds) {
      const trimmed = id.trim();
      if (trimmed) pinned.add(trimmed);
    }
    if (agent.allowExternalSkills) allowExternalSkills = true;
    if (agent.loadAllSkills) loadAllSkills = true;
    toolPolicy = mergeToolPolicies(toolPolicy, normalizeToolPolicy(agent.toolPolicy));
  }
  return {
    pinnedSkills: Array.from(pinned),
    allowExternalSkills,
    loadAllSkills,
    toolPolicy,
  };
}

/** 技能的可匹配别名（与 `findSkill` 接受的 "/" 触发别名一致）。 */
export interface SkillIdentity {
  name: string;
  dirName: string;
  displayName?: string;
}

/** 技能在 `exclude` / `skillIds` 里的规范写法。 */
function skillKey(skill: SkillIdentity): string {
  return skill.dirName || skill.name;
}

function matchesAlias(skill: SkillIdentity, alias: string): boolean {
  const key = alias.toLowerCase().trim();
  if (!key) return false;
  return (
    skill.name.toLowerCase() === key ||
    skill.dirName.toLowerCase() === key ||
    (!!skill.displayName && skill.displayName.toLowerCase() === key)
  );
}

/** 技能是否在勾选集里。 */
export function isSkillPinned(
  skill: SkillIdentity,
  pinnedSkills: readonly string[],
): boolean {
  return pinnedSkills.some((alias) => matchesAlias(skill, alias));
}

/**
 * 解析本轮的技能排除清单——注入、sidecar 目录、`/技能名` 显式触发三处共用
 * 同一份结果，所以「限制」不会在某一条路径上漏掉。
 *
 * `allowExternalSkills` 为 true（默认）时只有模式级排除生效，未勾选的技能
 * 照旧按触发条件或目录按需加载；为 false 时未勾选的技能全部进排除清单。
 *
 * @param capability 本轮生效的能力面。
 * @param allSkills 全部已安装技能（无视条件加载的结果）。
 * @param modeExcludes 模式级排除（如 plan 模式排掉执行类技能），始终生效。
 * @returns 去重后的技能排除清单。
 */
export function resolveSkillExcludes(
  capability: AgentCapability,
  allSkills: readonly SkillIdentity[],
  modeExcludes: readonly string[],
): string[] {
  const excludes = new Set(modeExcludes);
  if (!capability.allowExternalSkills) {
    for (const skill of allSkills) {
      if (!isSkillPinned(skill, capability.pinnedSkills)) {
        excludes.add(skillKey(skill));
      }
    }
  }
  return Array.from(excludes);
}
