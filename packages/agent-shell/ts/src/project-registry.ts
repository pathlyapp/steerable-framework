/**
 * 项目注册表（项目模式）。
 *
 * 项目 = 名字 + 绑定的本地文件夹。chat 通过 `chat_sessions.project_id` 绑定
 * 项目；绑定后该对话的文件读写与命令执行被硬限制在项目文件夹内（见
 * tool-router.ts 的 ToolExecContext.projectRoot 与 local-executor.ts 的
 * 路径围栏）。
 *
 * 持久化在 userData/agent-projects.json。存储通过 {@link ProjectKvStore}
 * 接口注入：main.ts 用 electron-store 实现，单测用内存实现——本模块不
 * import electron，保持纯 Node 可测（与 mcp-server-registry.ts 同一模式）。
 */

import { randomUUID } from 'node:crypto';

export interface ProjectRecord {
  id: string;
  /** 用户可见名称（侧边栏分组标题）。 */
  name: string;
  /** 绑定的项目文件夹（绝对路径）。 */
  folderPath: string;
  /**
   * W6-5 项目信任门控：项目目录里的 `AGENTS.md` / `CLAUDE.md` 等规则文件
   * 是「项目作者写给 agent 的指令」——打开一个恶意仓库时，一段精心构造的
   * 规则文件就能劫持 agent。因此项目级规则只在 `trusted: true` 时才加载进
   * 模型上下文；默认不信任（安全缺省），用户显式授权后才加载，且可随时撤销。
   * 旧记录没有此字段，读取时按 `false` 处理（见 {@link ProjectRegistry.isTrusted}）。
   */
  trusted?: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface CreateProjectInput {
  name: string;
  folderPath: string;
}

/** 最小 KV 存储接口，避免本模块直接依赖 electron-store。 */
export interface ProjectKvStore {
  get(key: 'projects'): ProjectRecord[] | undefined;
  set(key: 'projects', value: ProjectRecord[]): void;
}

const STORE_KEY = 'projects';

export class ProjectRegistry {
  constructor(private readonly store: ProjectKvStore) {}

  list(): ProjectRecord[] {
    // 按创建时间升序：侧边栏项目组顺序稳定，早建的在前。
    return [...(this.store.get(STORE_KEY) ?? [])].sort((a, b) =>
      a.createdAt.localeCompare(b.createdAt),
    );
  }

  get(idOrName: string): ProjectRecord | null {
    const needle = idOrName.trim().toLowerCase();
    return (
      this.list().find(
        (p) => p.id === idOrName || p.name.toLowerCase() === needle,
      ) ?? null
    );
  }

  create(input: CreateProjectInput): ProjectRecord {
    const name = input.name.trim();
    const folderPath = input.folderPath.trim();
    if (!name) throw new Error('项目名称不能为空');
    if (!folderPath) throw new Error('项目文件夹不能为空');
    if (this.get(name)) throw new Error(`已存在同名项目「${name}」`);

    const now = new Date().toISOString();
    const entry: ProjectRecord = {
      id: randomUUID(),
      name,
      folderPath,
      createdAt: now,
      updatedAt: now,
    };
    this.store.set(STORE_KEY, [...this.list(), entry]);
    return entry;
  }

  update(
    id: string,
    updates: Partial<CreateProjectInput>,
  ): ProjectRecord {
    const projects = this.list();
    const idx = projects.findIndex((p) => p.id === id);
    if (idx === -1) throw new Error('项目不存在');
    const current = projects[idx];
    const nextName = updates.name !== undefined ? updates.name.trim() : current.name;
    if (!nextName) throw new Error('项目名称不能为空');
    const nameClash = projects.some(
      (p) => p.id !== id && p.name.toLowerCase() === nextName.toLowerCase(),
    );
    if (nameClash) throw new Error(`已存在同名项目「${nextName}」`);
    const nextFolder =
      updates.folderPath !== undefined
        ? updates.folderPath.trim()
        : current.folderPath;
    if (!nextFolder) throw new Error('项目文件夹不能为空');

    const next: ProjectRecord = {
      ...current,
      name: nextName,
      folderPath: nextFolder,
      updatedAt: new Date().toISOString(),
    };
    projects[idx] = next;
    this.store.set(STORE_KEY, projects);
    return next;
  }

  delete(id: string): boolean {
    const projects = this.list();
    const next = projects.filter((p) => p.id !== id);
    if (next.length === projects.length) return false;
    this.store.set(STORE_KEY, next);
    return true;
  }

  /**
   * W6-5: read the trust flag, defaulting absent (legacy) records to
   * `false` — fail-closed, so a project is never trusted unless the user
   * explicitly said so.
   */
  isTrusted(id: string): boolean {
    return this.get(id)?.trusted === true;
  }

  /** W6-5: grant or revoke trust. Returns the updated record. */
  setTrusted(id: string, trusted: boolean): ProjectRecord {
    const projects = this.list();
    const idx = projects.findIndex((p) => p.id === id);
    if (idx === -1) throw new Error('项目不存在');
    const next: ProjectRecord = {
      ...projects[idx],
      trusted,
      updatedAt: new Date().toISOString(),
    };
    projects[idx] = next;
    this.store.set(STORE_KEY, projects);
    return next;
  }
}
