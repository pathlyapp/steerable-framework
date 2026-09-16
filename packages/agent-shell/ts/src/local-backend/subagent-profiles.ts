/**
 * 内置 subagent_type 画像集（CC `.claude/agents` parity）。
 *
 * 桌面不再向 sidecar 传裸 `subagent: true`——命名画像让每个委派带
 * 工具域 / 轮次上限 / 并发性 / 系统提示，模型按画像 description 选择
 * 委派对象（画像名与用途经工具 schema 广告给模型）。
 *
 * 设计约束：
 * - 画像的 toolFilter 引用的是桌面模型可见工具面（tool-router）里的
 *   真实工具名；收窄即权限边界（explore 够不到写工具是构造性的）。
 * - per-profile model 刻意不设：跟随父模型；模型路由留给设置面。
 * - 画像系统提示是种进子 loop 的第一条 system 消息（子代理看不到
 *   主对话的系统提示，画像提示就是它的全部角色设定）。
 */

export interface BuiltinSubagentProfile {
  toolFilter?: string[];
  maxRounds?: number;
  concurrent?: boolean;
  description: string;
  systemPrompt?: string;
}

/** 只读工具域：探索与调研画像共享的「摸不到写操作」基线。 */
const READ_ONLY_TOOLS = [
  'local_read_file',
  'local_list_scripts',
  'local_open_path',
  'web_search',
  'web_fetch',
  'mcp_list_tools',
];

export const BUILTIN_SUBAGENT_PROFILES: Record<string, BuiltinSubagentProfile> = {
  explore: {
    toolFilter: READ_ONLY_TOOLS,
    maxRounds: 12,
    concurrent: true,
    description:
      '只读代码侦察。搜索、读文件、定位「在哪里/是什么」类问题；不做任何修改，返回带路径:行号的发现。',
    systemPrompt: [
      '你是只读探索代理，任务是在代码库中定位信息并汇报发现。',
      '纪律：',
      '1) 你的工具域只有只读工具——不要尝试修改、创建、删除任何文件；',
      '2) 汇报必须具体：文件路径:行号、关键片段、明确结论；',
      '3) 找不到就明说找不到，并列出你查过的位置——禁止猜测。',
    ].join('\n'),
  },
  research: {
    toolFilter: [...READ_ONLY_TOOLS, 'local_write_file'],
    maxRounds: 16,
    concurrent: true,
    description:
      '深度调研。多轮 web 搜索/抓取并交叉验证，给出带来源 URL 的结论；可把报告写入文件。',
    systemPrompt: [
      '你是调研代理，任务是对一个问题做深度调研并给出可核验的结论。',
      '纪律：',
      '1) 每个事实性主张都必须来自真实抓取结果，并附来源 URL；',
      '2) 至少两个独立来源交叉验证关键结论，来源冲突时并列呈现；',
      '3) 需要交付长报告时用 local_write_file 落盘并在汇报中给出路径；',
      '4) 查不到就明说，禁止编造来源或数据。',
    ].join('\n'),
  },
  coder: {
    // 不设 toolFilter：全工具域。写操作串行（concurrent: false），避免两个
    // 实现代理同时改同一批文件。
    maxRounds: 16,
    concurrent: false,
    description:
      '全工具实现代理。改代码、跑命令、自验结果；适合自包含的实现/修复任务。',
    systemPrompt: [
      '你是实现代理，接到的是自包含的实现任务。',
      '纪律：',
      '1) 直接动手：真实调用工具改代码/跑命令，不要用文本描述代替执行；',
      '2) 完成后必须验证：跑相关测试或构建，把真实输出作为依据；',
      '3) 汇报改动清单 + 验证结果 + 遗留风险，禁止编造工具返回。',
    ].join('\n'),
  },
};

/**
 * chat.stream 的 `subagent` 参数：内置画像集。调用方（普通回合与
 * task-service 后台任务）共享同一份——任务回合的子代理画像与主对话
 * 一致，编排语义不随入口分叉。
 */
export function builtinSubagentParam(): { profiles: Record<string, BuiltinSubagentProfile> } {
  return { profiles: BUILTIN_SUBAGENT_PROFILES };
}
