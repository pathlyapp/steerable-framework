/**
 * 用户消息里的 "/" 显式触发解析（本地技能 + MCP 工具）。
 *
 * 从 router.ts 的 cleanUserMessage 抽出的纯函数，便于单测。支持两类触发：
 *
 *   /skillName 其余问题…        → forcedSkillName（指定本地技能）
 *   /mcp__srv__tool 其余问题…   → forcedMcpToolToken（指定 MCP 工具）
 *
 * 行首 token 信任选择器（技能不校验存在性；MCP token 经 resolver 规范大小写）。
 * 句中 token 必须精确命中一个已安装技能或已注册 MCP 工具才生效，避免把
 * 路径（/mnt/c）、URL、分数（1 /2）里的斜杠误判成触发。
 */

export interface MessageTriggers {
  cleanText: string;
  skillName?: string;
  mcpToolToken?: string;
}

export interface TriggerResolvers {
  /** 名字是否对应一个已安装技能（大小写不敏感，含 displayName）。 */
  isKnownSkill(name: string): boolean;
  /**
   * 把用户输入的 mcp token 规范成注册表里的精确 token（tool_call 必须用
   * 规范名，用户手敲可能大小写不符）；未启用 / 不存在返回 null。
   */
  resolveMcpToolToken(token: string): string | null;
}

const MCP_TOKEN_PATTERN = /^mcp__/i;

export function parseUserMessageTriggers(
  content: string,
  resolvers: TriggerResolvers,
): MessageTriggers {
  const trimmed = content.trim();

  if (trimmed.startsWith('/')) {
    const spaceIndex = trimmed.indexOf(' ');
    const rawToken =
      spaceIndex !== -1 ? trimmed.slice(1, spaceIndex).trim() : trimmed.slice(1).trim();
    const remainText = spaceIndex !== -1 ? trimmed.slice(spaceIndex + 1).trim() : '';

    // MCP 工具触发优先于技能：token 命名空间 mcp__* 与技能名天然隔离。
    if (MCP_TOKEN_PATTERN.test(rawToken)) {
      const token = resolvers.resolveMcpToolToken(rawToken) ?? rawToken;
      const cleanText = remainText
        ? `【指定MCP工具: ${token}】${remainText}`
        : `【指定MCP工具: ${token}】请调用该工具并展示其返回结果。`;
      return { cleanText, mcpToolToken: token };
    }

    if (spaceIndex !== -1) {
      const cleanText = `【指定本地技能: ${rawToken}】${remainText}`;
      return { cleanText, skillName: rawToken };
    }
    const cleanText = `【指定本地技能: ${rawToken}】请启用并展示技能「${rawToken}」的作用。`;
    return { cleanText, skillName: rawToken };
  }

  const match = trimmed.match(/(?:^|\s)\/([^\s/]+)/);
  if (match && typeof match.index === 'number' && match[1]) {
    const matchIndex = match.index;
    const matchedText = match[0];
    const typed = match[1];

    if (MCP_TOKEN_PATTERN.test(typed)) {
      const token = resolvers.resolveMcpToolToken(typed);
      if (token) {
        return buildMidMessageResult(
          trimmed,
          matchIndex,
          matchedText,
          typed,
          (remainText) =>
            remainText
              ? `【指定MCP工具: ${token}】${remainText}`
              : `【指定MCP工具: ${token}】请调用该工具并展示其返回结果。`,
          { mcpToolToken: token },
        );
      }
    } else {
      const candidate = typed.toLowerCase();
      if (resolvers.isKnownSkill(candidate)) {
        return buildMidMessageResult(
          trimmed,
          matchIndex,
          matchedText,
          typed,
          (remainText) =>
            remainText
              ? `【指定本地技能: ${candidate}】${remainText}`
              : `【指定本地技能: ${candidate}】请启用并展示技能「${candidate}」的作用。`,
          { skillName: candidate },
        );
      }
    }
  }
  return { cleanText: trimmed };
}

/** 句中 token：从原文里抠掉 token 后构造 cleanText，并合并触发字段。 */
function buildMidMessageResult(
  trimmed: string,
  matchIndex: number,
  matchedText: string,
  typedToken: string,
  buildCleanText: (remainText: string) => string,
  trigger: { skillName?: string; mcpToolToken?: string },
): MessageTriggers {
  const tokenStart = matchIndex + matchedText.indexOf('/');
  const tokenEnd = tokenStart + 1 + typedToken.length;
  const remainText = (trimmed.slice(0, tokenStart) + trimmed.slice(tokenEnd))
    .replace(/\s{2,}/g, ' ')
    .trim();
  return { cleanText: buildCleanText(remainText), ...trigger };
}
