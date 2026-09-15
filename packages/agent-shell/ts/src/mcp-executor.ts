import { execSync } from 'child_process';
import { createRequire } from 'module';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

// In ESM there is no `__filename` global; use the module URL to seed a local
// CommonJS require() so we can `require.resolve()` MCP server packages that
// ship as CJS (npx -y <pkg> targets, e.g. apple-notes-mcp).
const localRequire = createRequire(import.meta.url);

const IDLE_TIMEOUT_MS = 5 * 60 * 1000;

/**
 * 单次 MCP 请求（initialize 握手 / listTools / callTool）的超时。
 * SDK 默认 60s 太短：`npx -y <pkg>` 首次运行要从 npm registry 下载包，
 * 国内网络或走代理时 60s 内完不成握手，报 "MCP error -32001: Request
 * timed out"（2026-07-31 用户实测遇到）。放宽到 3 分钟覆盖冷启动下载。
 */
const MCP_REQUEST_TIMEOUT_MS = 3 * 60 * 1000;

/**
 * 给超时类错误追加可操作的排查提示。独立成纯函数便于单测。
 */
export function withMcpTroubleshootHint(message: string): string {
  if (!/timed?\s*out|超时|-32001/i.test(message)) return message;
  return (
    `${message}。若为首次启动该服务：npx/uvx 需要现场下载包，可能较慢——` +
    `可先在终端手动执行一次相同命令预热缓存，或为 npm 配置国内镜像源 ` +
    `(npm config set registry https://registry.npmmirror.com)，之后重试"测试连接"。`
  );
}

const COMMAND_HINTS: Record<string, string> = {
  npx: '请安装 Node.js (https://nodejs.org)',
  node: '请安装 Node.js (https://nodejs.org)',
  uvx: '请安装 uv (https://docs.astral.sh/uv)',
  uv: '请安装 uv (https://docs.astral.sh/uv)',
  python: '请安装 Python (https://python.org)',
  python3: '请安装 Python (https://python.org)',
};

function commandExists(cmd: string): boolean {
  try {
    const which = process.platform === 'win32' ? 'where' : 'which';
    execSync(`${which} ${cmd}`, { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}

function tryResolveBundled(config: McpServerConfig): McpServerConfig | null {
  if (config.command !== 'npx') return null;
  const args = config.args ?? [];
  const yIdx = args.indexOf('-y');
  if (yIdx === -1 || yIdx + 1 >= args.length) return null;
  const pkgName = args[yIdx + 1];
  const extraArgs = args.slice(yIdx + 2);

  try {
    const entryPath = localRequire.resolve(pkgName);
    return {
      command: process.execPath,
      args: [entryPath, ...extraArgs],
      env: { ...config.env, ELECTRON_RUN_AS_NODE: '1' },
      cwd: config.cwd,
    };
  } catch {
    return null;
  }
}

export interface McpServerConfig {
  command: string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
}

interface ToolSchema {
  name: string;
  inputSchema?: { properties?: Record<string, { type?: string }> };
}

interface PoolEntry {
  client: Client;
  transport: StdioClientTransport;
  lastUsed: number;
  key: string;
  toolSchemas: Map<string, ToolSchema>;
  /**
   * Count of calls currently in flight on this connection. `evictIdle()`
   * skips any entry with `inflight > 0` — without this, `lastUsed` was
   * stamped when a call *started*, so a single tool call running longer
   * than `IDLE_TIMEOUT_MS` (e.g. a slow MCP server or a long-running task)
   * looked "idle" to the 60s sweep and got `closeEntry()`'d out from under
   * the in-flight request, killing it with a connection-closed error.
   */
  inflight: number;
}

function configKey(cfg: McpServerConfig): string {
  return `${cfg.command}::${(cfg.args ?? []).join(' ')}`;
}

/**
 * Pure eviction predicate, split out so it's unit-testable without spinning
 * up a real MCP child process (`McpExecutor` isn't otherwise exported —
 * everything else about it talks to a real stdio transport).
 */
export function shouldEvictMcpConnection(
  entry: { lastUsed: number; inflight: number },
  now: number,
  idleTimeoutMs: number,
): boolean {
  if (entry.inflight > 0) return false;
  return now - entry.lastUsed > idleTimeoutMs;
}

function coerceArgs(args: Record<string, unknown>, schema: ToolSchema | undefined): Record<string, unknown> {
  if (!schema?.inputSchema?.properties) return args;
  const props = schema.inputSchema.properties;
  const out: Record<string, unknown> = { ...args };
  for (const [key, value] of Object.entries(args)) {
    const expected = props[key]?.type;
    if (expected === 'string' && typeof value !== 'string') {
      out[key] = String(value);
    } else if (expected === 'integer' && typeof value === 'string') {
      const n = parseInt(value, 10);
      if (!isNaN(n)) out[key] = n;
    } else if (expected === 'number' && typeof value === 'string') {
      const n = parseFloat(value);
      if (!isNaN(n)) out[key] = n;
    }
  }
  return out;
}

class McpExecutor {
  private pool = new Map<string, PoolEntry>();
  private cleanupTimer: ReturnType<typeof setInterval> | null = null;

  constructor() {
    this.cleanupTimer = setInterval(() => this.evictIdle(), 60_000);
  }

  async executeTool(
    config: McpServerConfig,
    toolName: string,
    toolArgs: Record<string, unknown>
  ): Promise<{ success: boolean; text?: string; error?: string }> {
    let entry: PoolEntry;
    try {
      entry = await this.getOrCreate(config);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      return { success: false, error: withMcpTroubleshootHint(`MCP server startup failed: ${msg}`) };
    }

    entry.inflight += 1;
    try {
      const coerced = coerceArgs(toolArgs, entry.toolSchemas.get(toolName));
      const result = await entry.client.callTool({ name: toolName, arguments: coerced }, undefined, {
        timeout: MCP_REQUEST_TIMEOUT_MS,
      });
      const textParts: string[] = [];
      if (Array.isArray(result.content)) {
        for (const item of result.content) {
          if (typeof item === 'object' && item !== null && 'text' in item) {
            textParts.push(String((item as { text: unknown }).text));
          }
        }
      }
      const text = textParts.join('\n') || JSON.stringify(result.content);
      if (result.isError) return { success: false, error: text };
      return { success: true, text };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      const key = configKey(config);
      await this.closeEntry(key);
      return { success: false, error: withMcpTroubleshootHint(`Tool execution failed: ${msg}`) };
    } finally {
      entry.inflight -= 1;
      entry.lastUsed = Date.now();
    }
  }

  async listTools(
    config: McpServerConfig
  ): Promise<{ success: boolean; tools?: { name: string; description: string; inputSchema?: unknown }[]; error?: string }> {
    let entry: PoolEntry;
    try {
      entry = await this.getOrCreate(config);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      return { success: false, error: withMcpTroubleshootHint(`MCP server startup failed: ${msg}`) };
    }

    entry.inflight += 1;
    try {
      const listing = await entry.client.listTools(undefined, { timeout: MCP_REQUEST_TIMEOUT_MS });
      return {
        success: true,
        tools: listing.tools.map(t => ({ name: t.name, description: t.description ?? '', inputSchema: t.inputSchema })),
      };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      const key = configKey(config);
      await this.closeEntry(key);
      return { success: false, error: withMcpTroubleshootHint(`List tools failed: ${msg}`) };
    } finally {
      entry.inflight -= 1;
      entry.lastUsed = Date.now();
    }
  }

  private async getOrCreate(config: McpServerConfig): Promise<PoolEntry> {
    const key = configKey(config);
    const existing = this.pool.get(key);
    if (existing) return existing;

    const resolved = tryResolveBundled(config) ?? config;
    if (!commandExists(resolved.command)) {
      const hint = COMMAND_HINTS[config.command] || `请确保 ${config.command} 已安装并在 PATH 中`;
      throw new Error(`命令 "${config.command}" 不可用。${hint}`);
    }

    const transport = new StdioClientTransport({
      command: resolved.command,
      args: resolved.args,
      env: { ...process.env, ...(resolved.env ?? {}) } as Record<string, string>,
      cwd: resolved.cwd ?? undefined,
      stderr: 'pipe',
    });
    const client = new Client({ name: 'agent-shell', version: '1.0.0' }, { capabilities: {} });
    await client.connect(transport, { timeout: MCP_REQUEST_TIMEOUT_MS });

    const toolSchemas = new Map<string, ToolSchema>();
    try {
      const listing = await client.listTools(undefined, { timeout: MCP_REQUEST_TIMEOUT_MS });
      for (const t of listing.tools) {
        toolSchemas.set(t.name, {
          name: t.name,
          inputSchema: t.inputSchema as ToolSchema['inputSchema'],
        });
      }
    } catch {
      // no-op
    }

    const entry: PoolEntry = { client, transport, lastUsed: Date.now(), key, toolSchemas, inflight: 0 };
    this.pool.set(key, entry);
    transport.onclose = () => {
      this.pool.delete(key);
    };
    return entry;
  }

  private async closeEntry(key: string): Promise<void> {
    const entry = this.pool.get(key);
    if (!entry) return;
    this.pool.delete(key);
    try {
      await entry.client.close();
    } catch {
      // ignore
    }
  }

  /** 关闭连接池中的所有连接（应用退出 / 测试收尾时调用）。 */
  async shutdownAll(): Promise<void> {
    const keys = [...this.pool.keys()];
    await Promise.all(keys.map((key) => this.closeEntry(key)));
  }

  private evictIdle(): void {
    const now = Date.now();
    for (const [key, entry] of this.pool) {
      if (shouldEvictMcpConnection(entry, now, IDLE_TIMEOUT_MS)) {
        this.closeEntry(key).catch(() => {});
      }
    }
  }
}

export const mcpExecutor = new McpExecutor();
