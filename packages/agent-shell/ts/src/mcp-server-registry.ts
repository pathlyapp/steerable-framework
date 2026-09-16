/**
 * MCP 外部服务注册表。
 *
 * 用户在「设置 → MCP 服务」里导入/登记的外部 MCP server（stdio 型），
 * 持久化在 userData/agent-mcp-servers.json。已启用服务的工具会经
 * ToolRouter 以 `mcp__<serverKey>__<toolName>` 一等工具身份暴露给模型。
 *
 * 存储通过 {@link McpServerKvStore} 接口注入：main.ts 用 electron-store
 * 实现，单测用内存实现——本模块不 import electron，保持纯 Node 可测。
 */

import { randomUUID } from 'node:crypto';
import { mcpExecutor, type McpServerConfig } from './mcp-executor.js';

export interface McpServerEntry {
  id: string;
  /** 用户可见名称，也用于生成工具前缀（如 "filesystem"）。 */
  name: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  cwd?: string;
  enabled: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface CreateMcpServerInput {
  name: string;
  command: string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
  enabled?: boolean;
}

/** 最小 KV 存储接口，避免本模块直接依赖 electron-store。 */
export interface McpServerKvStore {
  get(key: 'mcpServers'): McpServerEntry[] | undefined;
  set(key: 'mcpServers', value: McpServerEntry[]): void;
}

export interface CachedToolList {
  tools: Array<{ name: string; description: string; inputSchema?: unknown }>;
  fetchedAt: string;
  error: string | null;
}

export interface ImportResult {
  added: McpServerEntry[];
  /** 因重名或缺少 command 被跳过的服务名。 */
  skipped: string[];
}

/**
 * 一个可调用 MCP 工具的扁平视图（跨服务），供 "/" 选择器与消息触发解析用。
 * `token` 即模型侧的一等工具名 `mcp__<serverKey>__<toolName>`（命名规则与
 * tool-router.ts 的动态工具保持一致）。
 */
export interface McpToolEntry {
  token: string;
  toolName: string;
  serverKey: string;
  serverName: string;
  description: string;
  serverId: string;
}

const STORE_KEY = 'mcpServers';

/** 工具名只允许 [a-z0-9-]；中文名会被剥光，退化为 srv-<id前6位>。 */
export function sanitizeServerKey(name: string, id: string): string {
  const cleaned = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return cleaned || `srv-${id.slice(0, 6)}`;
}

export class McpServerRegistry {
  /** serverId → 最近一次 listTools 的结果（内存缓存，重启后由后台刷新重建）。 */
  private readonly toolCache = new Map<string, CachedToolList>();

  constructor(private readonly store: McpServerKvStore) {}

  list(): McpServerEntry[] {
    return this.store.get(STORE_KEY) ?? [];
  }

  get(idOrName: string): McpServerEntry | null {
    const needle = idOrName.trim().toLowerCase();
    return (
      this.list().find(
        (s) => s.id === idOrName || s.name.toLowerCase() === needle,
      ) ?? null
    );
  }

  create(input: CreateMcpServerInput): McpServerEntry {
    const name = input.name.trim();
    const command = input.command.trim();
    if (!name) throw new Error('服务名称不能为空');
    if (!command) throw new Error('启动命令不能为空');
    if (this.get(name)) throw new Error(`已存在同名服务「${name}」`);

    const now = new Date().toISOString();
    const entry: McpServerEntry = {
      id: randomUUID(),
      name,
      command,
      args: (input.args ?? []).map((a) => String(a)),
      env: { ...(input.env ?? {}) },
      cwd: input.cwd?.trim() || undefined,
      enabled: input.enabled ?? true,
      createdAt: now,
      updatedAt: now,
    };
    this.store.set(STORE_KEY, [...this.list(), entry]);
    return entry;
  }

  update(id: string, updates: Partial<CreateMcpServerInput>): McpServerEntry {
    const servers = this.list();
    const idx = servers.findIndex((s) => s.id === id);
    if (idx === -1) throw new Error('服务不存在');
    const current = servers[idx];
    const nextName = updates.name !== undefined ? updates.name.trim() : current.name;
    if (!nextName) throw new Error('服务名称不能为空');
    const nameClash = servers.some(
      (s) => s.id !== id && s.name.toLowerCase() === nextName.toLowerCase(),
    );
    if (nameClash) throw new Error(`已存在同名服务「${nextName}」`);

    const next: McpServerEntry = {
      ...current,
      name: nextName,
      command:
        updates.command !== undefined ? updates.command.trim() : current.command,
      args: updates.args !== undefined ? updates.args.map((a) => String(a)) : current.args,
      env: updates.env !== undefined ? { ...updates.env } : current.env,
      cwd: updates.cwd !== undefined ? updates.cwd.trim() || undefined : current.cwd,
      enabled: updates.enabled !== undefined ? updates.enabled : current.enabled,
      updatedAt: new Date().toISOString(),
    };
    if (!next.command) throw new Error('启动命令不能为空');
    servers[idx] = next;
    this.store.set(STORE_KEY, servers);
    // 配置变了，旧工具缓存不可信
    this.toolCache.delete(id);
    return next;
  }

  delete(id: string): boolean {
    const servers = this.list();
    const next = servers.filter((s) => s.id !== id);
    if (next.length === servers.length) return false;
    this.store.set(STORE_KEY, next);
    this.toolCache.delete(id);
    return true;
  }

  /**
   * 解析 Claude Desktop 风格的配置并批量导入：
   *
   * ```json
   * { "mcpServers": { "filesystem": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/"] } } }
   * ```
   *
   * 也兼容裸对象（不带 mcpServers 包裹）。重名或缺 command 的条目跳过。
   */
  importClaudeConfig(raw: string | Record<string, unknown>): ImportResult {
    let parsed: unknown = raw;
    if (typeof raw === 'string') {
      parsed = JSON.parse(raw); // JSON 语法错误直接抛给调用方
    }
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      throw new Error('配置必须是 JSON 对象');
    }
    const container =
      'mcpServers' in parsed
        ? (parsed as Record<string, unknown>).mcpServers
        : parsed;
    if (typeof container !== 'object' || container === null || Array.isArray(container)) {
      throw new Error('mcpServers 必须是对象（服务名 → 配置）');
    }

    const result: ImportResult = { added: [], skipped: [] };
    for (const [name, cfgRaw] of Object.entries(container as Record<string, unknown>)) {
      const cfg = (cfgRaw ?? {}) as Record<string, unknown>;
      const command = typeof cfg.command === 'string' ? cfg.command.trim() : '';
      if (!command || this.get(name)) {
        result.skipped.push(name);
        continue;
      }
      const env: Record<string, string> = {};
      if (cfg.env && typeof cfg.env === 'object' && !Array.isArray(cfg.env)) {
        for (const [k, v] of Object.entries(cfg.env as Record<string, unknown>)) {
          env[k] = String(v);
        }
      }
      const entry = this.create({
        name: name.trim(),
        command,
        args: Array.isArray(cfg.args) ? cfg.args.map((a) => String(a)) : [],
        env,
        cwd: typeof cfg.cwd === 'string' ? cfg.cwd : undefined,
        enabled: true,
      });
      result.added.push(entry);
    }
    return result;
  }

  /** 工具名前缀（同一名称多实例撞名时追加 id 后缀保证唯一）。 */
  serverKey(entry: McpServerEntry): string {
    const base = sanitizeServerKey(entry.name, entry.id);
    const clash = this.list().some(
      (s) => s.id !== entry.id && sanitizeServerKey(s.name, s.id) === base,
    );
    return clash ? `${base}-${entry.id.slice(0, 4)}` : base;
  }

  toExecutorConfig(entry: McpServerEntry): McpServerConfig {
    return {
      command: entry.command,
      args: entry.args,
      env: entry.env,
      cwd: entry.cwd,
    };
  }

  getCachedTools(serverId: string): CachedToolList | null {
    return this.toolCache.get(serverId) ?? null;
  }

  /**
   * 所有「已启用 + 工具缓存非空」服务的工具扁平清单（"/" 选择器数据源）。
   */
  listEnabledToolEntries(): McpToolEntry[] {
    const out: McpToolEntry[] = [];
    for (const server of this.list()) {
      if (!server.enabled) continue;
      const cached = this.getCachedTools(server.id);
      if (!cached || cached.tools.length === 0) continue;
      const serverKey = this.serverKey(server);
      for (const tool of cached.tools) {
        out.push({
          token: `mcp__${serverKey}__${tool.name}`,
          toolName: tool.name,
          serverKey,
          serverName: server.name,
          description: tool.description ?? '',
          serverId: server.id,
        });
      }
    }
    return out;
  }

  /**
   * 按完整 token（大小写不敏感）反查工具；命中时返回注册表里的规范 token
   * （用户手敲可能大小写不符，tool_call 必须用规范名）。未启用 / 无缓存 /
   * 不存在均返回 null。
   */
  findToolByToken(token: string): McpToolEntry | null {
    const needle = token.trim().toLowerCase();
    if (!needle.startsWith('mcp__')) return null;
    return (
      this.listEnabledToolEntries().find((e) => e.token.toLowerCase() === needle) ?? null
    );
  }

  /** 连接服务并刷新工具缓存；失败时缓存错误信息（tools 为空）。 */
  async refreshTools(serverId: string): Promise<CachedToolList> {
    const entry = this.list().find((s) => s.id === serverId);
    if (!entry) throw new Error('服务不存在');
    const result = await mcpExecutor.listTools(this.toExecutorConfig(entry));
    const cached: CachedToolList = result.success
      ? {
          tools: (result.tools ?? []).map((t) => ({
            name: t.name,
            description: t.description,
            inputSchema: t.inputSchema,
          })),
          fetchedAt: new Date().toISOString(),
          error: null,
        }
      : { tools: [], fetchedAt: new Date().toISOString(), error: result.error ?? '未知错误' };
    this.toolCache.set(serverId, cached);
    return cached;
  }

  /** 后台刷新所有已启用服务的工具列表（启动时调用，不阻塞、不抛错）。 */
  async refreshAllEnabled(): Promise<void> {
    const jobs = this.list()
      .filter((s) => s.enabled)
      .map((s) =>
        this.refreshTools(s.id).catch((err) => {
          this.toolCache.set(s.id, {
            tools: [],
            fetchedAt: new Date().toISOString(),
            error: err instanceof Error ? err.message : String(err),
          });
        }),
      );
    await Promise.allSettled(jobs);
  }
}
