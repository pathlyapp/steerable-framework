/**
 * W6-7a 项目规则文件加载（AGENTS.md / CLAUDE.md 生态约定）。
 *
 * 从绑定的项目目录向上遍历，收集每一层的规则文件，拼成一段注入模型上下文。
 * 三个对照框架都有这一机制（pi 的 `ResourceLoader` 向上遍历
 * `AGENTS.override.md`/`AGENTS.md`/`CLAUDE.md`；codex 把 AGENTS.md 当作
 * WorldState 的一节），我们此前完全没有。
 *
 * 安全前提（W6-5）：这些文件是「项目作者写给 agent 的指令」，属于不可信输入。
 * 本模块只做发现并返回内容；**是否注入由调用方按项目信任状态决定**——未信任
 * 的项目一律不调用本模块。遍历与注入都有硬上限，单个文件与总量都截断。
 *
 * 「热重载」按回合粒度天然成立：每个回合重新读取磁盘，规则文件保存即生效，
 * 不需要文件 watcher。
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

/** 每层目录按此顺序查找（override 优先于通用约定）。 */
const RULE_FILE_NAMES = ['AGENTS.override.md', 'AGENTS.md', 'CLAUDE.md'] as const;

/** 最多向上遍历的层数（含起始目录）——支持 monorepo 把规则放在父目录。 */
const MAX_LEVELS = 6;
/** 最多注入的规则文件个数。 */
const MAX_RULE_FILES = 8;
/** 单个规则文件的最大字符数（超出截断）。 */
const MAX_FILE_CHARS = 8_000;
/** 注入内容总字符上限（W4-7 注入内容必须有界）。 */
const MAX_TOTAL_CHARS = 20_000;

export interface ProjectRules {
  /** 实际发现的规则文件绝对路径，根→叶顺序（越靠后对当前项目越具体）。 */
  files: string[];
  /** 拼接好的注入文本（含每个文件的来源标注），已按上限截断。 */
  content: string;
  /** 是否有内容因上限被截断。 */
  truncated: boolean;
}

const EMPTY: ProjectRules = { files: [], content: '', truncated: false };

/**
 * 收集并拼接项目规则。`folderPath` 为绑定的项目根目录。
 *
 * 向上遍历在「用户 home 目录」处封顶（若项目不在 home 下则按 MAX_LEVELS
 * 封顶）——不扫 `/`、`/etc` 这类与项目无关的祖先目录。
 */
export function loadProjectRuleFiles(folderPath: string): ProjectRules {
  if (!folderPath || !folderPath.trim()) return EMPTY;
  const start = path.resolve(folderPath);
  if (!fs.existsSync(start)) return EMPTY;

  // 构造 根→…→起始目录 的目录链（根在前，越往后越贴近项目）。
  const home = os.homedir();
  const chain: string[] = [];
  let dir = start;
  const seen = new Set<string>();
  while (!seen.has(dir) && chain.length < MAX_LEVELS) {
    seen.add(dir);
    chain.unshift(dir);
    if (dir === home) break; // 不越过 home 向上
    const parent = path.dirname(dir);
    if (parent === dir) break; // 已到文件系统根
    dir = parent;
  }

  // 根→叶收集规则文件；每层内 override 优先。
  const files: string[] = [];
  for (const d of chain) {
    for (const name of RULE_FILE_NAMES) {
      const p = path.join(d, name);
      try {
        if (fs.statSync(p).isFile()) files.push(p);
      } catch {
        // 不存在或不可读——跳过该文件。
      }
      if (files.length >= MAX_RULE_FILES) break;
    }
    if (files.length >= MAX_RULE_FILES) break;
  }
  if (files.length === 0) return EMPTY;

  // 拼接并施加总量上限。
  const sections: string[] = [];
  let total = 0;
  let truncated = false;
  for (const file of files) {
    let body: string;
    try {
      body = fs.readFileSync(file, 'utf-8');
    } catch {
      continue;
    }
    if (body.length > MAX_FILE_CHARS) {
      body = `${body.slice(0, MAX_FILE_CHARS)}\n…（规则文件过长，已截断）`;
      truncated = true;
    }
    const section = `## 项目规则（来自 \`${file}\`）\n\n${body.trim()}`;
    if (total + section.length > MAX_TOTAL_CHARS) {
      truncated = true;
      break;
    }
    sections.push(section);
    total += section.length;
  }

  return { files, content: sections.join('\n\n'), truncated };
}
