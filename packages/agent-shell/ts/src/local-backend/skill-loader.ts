/**
 * Skill loader — desktop thin client over the framework's skill parser.
 *
 * SKILL.md parsing (frontmatter, `layer` derivation, condition matching,
 * `{scripts}` resolution, user-root-overrides-builtin) is single-sourced in
 * Python (`steerable_agent_runtime.skills`) and reached via the sidecar
 * `skills.list` RPC. This module only resolves the skill roots (builtin,
 * workspace `skills/` dirs, userData), calls the RPC, and returns the wire
 * shape (which already matches `SkillModule`). The product concerns stay on
 * top: eager-layer budget trimming (prompt-builder) and the import/uninstall
 * file management (router / skill-install).
 *
 * The sidecar shares the filesystem with this host, so the roots below are
 * readable by both. The sidecar is the only chat path and auto-restarts, so
 * it is available both in-turn (prompt assembly) and for the management UI.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { getAppRootDir, getUserDataDir } from '../runtime.js';
import { getSidecarSupervisor, whenSidecarSupervisor } from '../sidecar/handle.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_SKILLS_DIR = path.resolve(__dirname, 'skills');

/** builtin = 随应用分发; user = 设置页导入; workspace = 项目/工作区 `skills/`. */
export type SkillOrigin = 'builtin' | 'user' | 'workspace';

export function getUserSkillsDir(): string {
  const dir = path.join(getUserDataDir(), 'skills');
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
  return dir;
}

/**
 * 场景包技能根（3.2）：产品构建把激活包的技能拷到产品产物的
 * `pack-skills/` 目录，产品组装根经 setPackSkillsDir 注入。包技能随产品
 * 分发，origin 归 'builtin'。未注入（纯 shell）时该根不参与。
 */
let packSkillsDir: string | null = null;

export function setPackSkillsDir(dir: string): void {
  packSkillsDir = dir;
}

/**
 * Extra skill roots beyond builtin + userData (project folders, cwd, app
 * root). Hosts register this at boot; tests leave it unset so roots stay
 * [builtin, user].
 */
let workspaceSkillRootsProvider: (() => Iterable<string>) | null = null;

export function setWorkspaceSkillRootsProvider(
  provider: (() => Iterable<string>) | null,
): void {
  workspaceSkillRootsProvider = provider;
}

/** Bind the default workspace roots: each project's `skills/`, cwd, app root. */
export function bindWorkspaceSkillRoots(registry: {
  list: () => Array<{ folderPath: string }>;
}): void {
  setWorkspaceSkillRootsProvider(() => [
    ...registry.list().map((p) => path.join(p.folderPath, 'skills')),
    path.join(process.cwd(), 'skills'),
    path.join(getAppRootDir(), 'skills'),
  ]);
}

function samePath(a: string, b: string): boolean {
  return path.resolve(a) === path.resolve(b);
}

function uniqueExisting(dirs: Iterable<string>): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const dir of dirs) {
    const resolved = path.resolve(dir);
    if (seen.has(resolved)) continue;
    seen.add(resolved);
    if (!fs.existsSync(resolved) || !fs.statSync(resolved).isDirectory()) continue;
    out.push(resolved);
  }
  return out;
}

/**
 * Skill roots in override order: builtin, workspace extras, user last
 * (user wins on name clash, same as the framework provider merge).
 */
export function listSkillRoots(skillsDir?: string): string[] {
  if (skillsDir) return [path.resolve(skillsDir)];
  const builtin = path.resolve(DEFAULT_SKILLS_DIR);
  const user = path.resolve(getUserSkillsDir());
  const pack = packSkillsDir && fs.existsSync(packSkillsDir) ? [path.resolve(packSkillsDir)] : [];
  const extras = uniqueExisting(workspaceSkillRootsProvider?.() ?? []).filter(
    (dir) => !samePath(dir, builtin) && !samePath(dir, user) && !pack.some((p) => samePath(dir, p)),
  );
  return [builtin, ...pack, ...extras, user];
}

export function classifySkillOrigin(skillsDir: string): SkillOrigin {
  const resolved = path.resolve(skillsDir);
  if (samePath(resolved, getUserSkillsDir())) return 'user';
  if (samePath(resolved, DEFAULT_SKILLS_DIR)) return 'builtin';
  if (packSkillsDir && samePath(resolved, packSkillsDir)) return 'builtin';
  return 'workspace';
}

export type SkillLayer = 'eager' | 'catalog';

export interface SkillModule {
  name: string;
  /** 可读展示名（可以是中文，如「数据处理链」）。没配置时为空串。 */
  displayName: string;
  description: string;
  priority: number;
  tags: string[];
  conditions: string[];
  match: 'any' | 'all';
  /**
   * 分层披露:eager = 正文常驻系统提示词;catalog = 只进目录,模型调
   * `skill` 工具按需加载正文。由框架解析器按 frontmatter `layer` 或
   * priority 阈值派生。
   */
  layer: SkillLayer;
  /**
   * false = 只能由用户通过 `/name` 触发(生态兼容:Claude/codex 的
   * `disable-model-invocation: true`),不进 catalog、skill 工具拒绝加载。
   */
  modelInvocable: boolean;
  content: string;
  dirName: string;
  skillsDir: string;
}

export interface LoadSkillsOptions {
  conditions?: Iterable<string>;
  /** Override skills root (mainly for tests); bypasses built-in + user dirs. */
  skillsDir?: string;
  /** Kept for call-site compatibility; the RPC re-parses fresh each call. */
  reload?: boolean;
  ignoreConditions?: boolean;
  /**
   * Skill names (or dir names) to always drop, even when `ignoreConditions`
   * is set. Used for mode-scoped skills (e.g. `plan-mode`) that must never
   * leak into a turn they don't belong to.
   */
  excludeSkillNames?: Iterable<string>;
}

/**
 * Load skill modules matching the active runtime conditions, parsed by the
 * framework. When the sidecar is still booting (renderer requests race app
 * start), waits on the registered boot promise instead of degrading
 * immediately. Returns [] when the sidecar is unavailable, boot failed, the
 * wait times out, or the RPC fails (mirrors the old "missing dir → []"
 * robustness; the prompt then falls back to its built-in minimal prompt).
 */
export async function loadSkills(options: LoadSkillsOptions = {}): Promise<SkillModule[]> {
  const supervisor = getSidecarSupervisor() ?? (await whenSidecarSupervisor());
  if (!supervisor) {
    console.warn('[skill-loader] sidecar unavailable; no skills loaded');
    return [];
  }
  try {
    return await supervisor.listSkills({
      roots: listSkillRoots(options.skillsDir),
      conditions: options.conditions ? Array.from(options.conditions) : undefined,
      exclude: options.excludeSkillNames ? Array.from(options.excludeSkillNames) : undefined,
      ignoreConditions: options.ignoreConditions,
    });
  } catch (err) {
    console.warn('[skill-loader] skills.list failed', err);
    return [];
  }
}

export function getSkillsDir(): string {
  return DEFAULT_SKILLS_DIR;
}

/**
 * Resolve one skill by the aliases the "/" trigger accepts (name / dirName /
 * displayName, case-insensitive), bypassing conditions — an explicit user
 * trigger forces the skill. `exclude` keeps mode-scoped drops authoritative
 * (e.g. execution skills stay out of plan mode even when typed explicitly).
 */
export async function findSkill(
  name: string,
  options: { exclude?: Iterable<string>; skillsDir?: string } = {},
): Promise<SkillModule | null> {
  const key = name.toLowerCase().trim();
  if (!key) return null;
  const all = await loadSkills({
    ignoreConditions: true,
    skillsDir: options.skillsDir,
    excludeSkillNames: options.exclude,
  });
  const hit = all.find(
    (m) =>
      m.name.toLowerCase() === key ||
      m.dirName.toLowerCase() === key ||
      (m.displayName !== '' && m.displayName.toLowerCase() === key),
  );
  return hit ?? null;
}
