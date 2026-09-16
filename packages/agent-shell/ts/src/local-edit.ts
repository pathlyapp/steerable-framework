/**
 * 结构化文件编辑（W6-1）的桌面侧薄客户端。
 *
 * 定位/替换/统一 diff 算法已下沉到框架，单一真源是 Python 的
 * `steerable_sidecar/file_edit.py`（headless 与 ACP 工作区工具共用同一实现），
 * 通过 sidecar 的 `workspace.apply_edits` RPC 调用。本模块只做线协议转换与
 * 错误映射；文件读写、版本令牌、原子落盘、同文件串行队列仍在 local-executor。
 */

import { getSidecarSupervisor } from './sidecar/handle.js';
import { SidecarMethodError } from './sidecar/errors.js';

export interface EditOp {
  /** 定位锚点（要被替换掉的原文）。 */
  oldText: string;
  /** 替换后的新文。 */
  newText: string;
}

export type MatchLevel = 'exact' | 'trim' | 'unicode';

/** 一次命中在原文中的位置与跨度（供工具卡/测试断言用）。 */
export interface EditMatch {
  level: MatchLevel;
  startLine: number;
  oldLineCount: number;
}

export interface ApplyEditsResult {
  /** 应用全部编辑后的新文件内容。 */
  content: string;
  /** 统一 diff（unified diff，3 行上下文），供工具卡渲染。 */
  diff: string;
  matches: EditMatch[];
}

export class EditError extends Error {
  constructor(
    message: string,
    readonly code:
      | 'empty_old'
      | 'not_found'
      | 'ambiguous'
      | 'overlap'
      | 'no_edits',
  ) {
    super(message);
    this.name = 'EditError';
  }
}

/** 把 sidecar 的 `workspace.apply_edits` 响应映射为桌面结果形状。 */
export function buildApplyEditsResult(result: {
  content: string;
  diff: string;
  matches: Array<{ level: MatchLevel; startLine: number; oldLineCount: number }>;
}): ApplyEditsResult {
  return { content: result.content, diff: result.diff, matches: result.matches };
}

/**
 * 在 sidecar（Python 真源）里对 `content` 应用 `edits`。只在回合内可达——
 * 编辑工具经 reverse `tool.invoke` 触发，此刻 sidecar 必在运行；sidecar 不可用
 * 时抛出 EditError 之外的错误，由 local-executor 的兜底分支转成失败结果。
 */
export async function applyEdits(
  content: string,
  edits: EditOp[],
  filePath = 'file',
): Promise<ApplyEditsResult> {
  const supervisor = getSidecarSupervisor();
  if (!supervisor) {
    throw new Error('sidecar 不可用——结构化编辑算法在框架侧，需 sidecar 运行。');
  }
  try {
    const result = await supervisor.applyEdits({ content, edits, filePath });
    return buildApplyEditsResult(result);
  } catch (error) {
    if (error instanceof SidecarMethodError && error.kind === 'edit_failed') {
      const code = (error.data as { code?: string } | undefined)?.code;
      throw new EditError(error.message, (code as EditError['code']) ?? 'not_found');
    }
    throw error;
  }
}
