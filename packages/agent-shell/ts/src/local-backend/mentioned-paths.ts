/**
 * 对话正文里提到的文件路径的存在性解析（「点击打开正文里的路径」用）。
 *
 * 渲染层从助手回答的行内代码里挑出「看着像路径」的字面量，批量送到这里
 * 落地成绝对路径并 stat 验证。只有真实存在的条目会回给渲染层——路径是
 * 模型写出来的自然语言片段，猜错在所难免，靠 stat 证伪而不是靠解析可靠。
 *
 * 相对路径的基准目录由调用方给（绑定项目的对话用项目根，否则用 home），
 * 与 exec 工具缺省 cwd 的回落规则一致，这样模型写 `./out.bin` 时指向的
 * 就是它当时真正落盘的位置。
 */

import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

export interface ResolvedMentionedPath {
  /** 渲染层送来的原始字面量（用于回填到对应的行内代码节点）。 */
  candidate: string;
  /** 落地后的绝对路径（点击打开直接用）。 */
  path: string;
  /** 目录会用文件管理器打开，渲染层据此换图标。 */
  isDirectory: boolean;
}

/** 单次请求的候选上限与单条长度上限：渲染层是不可信输入，挡住刷 stat。 */
const MAX_CANDIDATES = 64;
const MAX_CANDIDATE_LENGTH = 512;

/**
 * 批量解析候选路径，只返回真实存在的条目。`baseDir` 是相对路径的基准
 * 目录；`homeDir` 仅测试注入用，默认 os.homedir()。
 */
export async function resolveMentionedPaths(options: {
  candidates: readonly string[];
  baseDir: string;
  homeDir?: string;
}): Promise<ResolvedMentionedPath[]> {
  const homeDir = options.homeDir ?? os.homedir();
  const out: ResolvedMentionedPath[] = [];
  const seen = new Set<string>();

  for (const raw of options.candidates.slice(0, MAX_CANDIDATES)) {
    if (typeof raw !== 'string') continue;
    const candidate = raw.trim();
    if (!candidate || candidate.length > MAX_CANDIDATE_LENGTH) continue;
    // 换行/NUL 不可能是单个路径字面量，且会污染后续 stat。
    if (/[\n\r\0]/.test(candidate)) continue;
    if (seen.has(candidate)) continue;
    seen.add(candidate);

    const full = toAbsolute(candidate, options.baseDir, homeDir);
    if (!full) continue;
    let stat;
    try {
      stat = await fs.stat(full);
    } catch {
      // 不存在/无权限：模型写错或文件已被删，静默丢弃。
      continue;
    }
    if (!stat.isFile() && !stat.isDirectory()) continue;
    out.push({ candidate, path: full, isDirectory: stat.isDirectory() });
  }
  return out;
}

/** `~` 展开 → 绝对路径原样 → 其余按 baseDir 解析。 */
function toAbsolute(candidate: string, baseDir: string, homeDir: string): string | null {
  if (candidate === '~') return homeDir;
  if (candidate.startsWith('~/') || candidate.startsWith('~\\')) {
    return path.resolve(path.join(homeDir, candidate.slice(2)));
  }
  if (path.isAbsolute(candidate)) return path.resolve(candidate);
  if (!baseDir) return null;
  return path.resolve(baseDir, candidate);
}
