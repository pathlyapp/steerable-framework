import { useCallback, useEffect, useState } from 'react';
import {
  LuLoaderCircle,
  LuPencil,
  LuPlug,
  LuPlus,
  LuRefreshCw,
  LuToggleLeft,
  LuToggleRight,
  LuTrash2,
} from 'react-icons/lu';
import { getElectronBridge, isElectron } from '@/lib/electron-bridge';

/**
 * McpSettingsPanel — MCP 服务管理面板（JSON 导入 / 手动增改 / 启停 / 测试 /
 * 删除）。
 *
 * 渲染在 `/settings?section=mcp` 独立页（AgentLayout 右侧内容区）。
 *
 * 数据与状态完全自管理：挂载时拉一次列表，操作后刷新。后端走
 * local-backend REST（`/api/v2/mcp/servers*`），注册表持久化在 main 进程的
 * electron-store（`agent-mcp-servers.json`），无专用 IPC channel。
 */

/** GET /api/v2/mcp/servers 返回的服务条目（含工具缓存状态）。 */
export interface McpServerInfo {
  id: string;
  name: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  cwd?: string;
  enabled: boolean;
  serverKey: string;
  toolCount: number;
  toolsPreview: string[];
  lastError: string | null;
  lastFetchedAt: string | null;
}

export function McpSettingsPanel() {
  const [mcpServers, setMcpServers] = useState<McpServerInfo[]>([]);
  const [mcpLoading, setMcpLoading] = useState(false);
  const [mcpImportJson, setMcpImportJson] = useState('');
  const [mcpImporting, setMcpImporting] = useState(false);
  const [mcpImportStatus, setMcpImportStatus] = useState<string | null>(null);
  const [mcpFormOpen, setMcpFormOpen] = useState(false);
  const [mcpEditingId, setMcpEditingId] = useState<string | null>(null);
  const [mcpFormName, setMcpFormName] = useState('');
  const [mcpFormCommand, setMcpFormCommand] = useState('');
  const [mcpFormArgs, setMcpFormArgs] = useState('');
  const [mcpFormEnv, setMcpFormEnv] = useState('');
  const [mcpFormCwd, setMcpFormCwd] = useState('');
  const [mcpSaving, setMcpSaving] = useState(false);
  const [mcpTestingId, setMcpTestingId] = useState<string | null>(null);
  const [mcpDeletingId, setMcpDeletingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fetchMcpServers = useCallback(async () => {
    if (!isElectron()) return;
    setMcpLoading(true);
    setError(null);
    try {
      const res = await getElectronBridge()!.localBackend.request<{ servers: McpServerInfo[] }>({
        method: 'GET',
        path: '/api/v2/mcp/servers',
      });
      setMcpServers(res.servers || []);
    } catch (err) {
      console.error('获取 MCP 服务列表失败:', err);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setMcpLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchMcpServers();
  }, [fetchMcpServers]);

  const resetMcpForm = () => {
    setMcpEditingId(null);
    setMcpFormName('');
    setMcpFormCommand('');
    setMcpFormArgs('');
    setMcpFormEnv('');
    setMcpFormCwd('');
  };

  const parseEnvLines = (raw: string): Record<string, string> => {
    const env: Record<string, string> = {};
    for (const line of raw.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
      const eq = trimmed.indexOf('=');
      if (eq <= 0) continue;
      env[trimmed.slice(0, eq).trim()] = trimmed.slice(eq + 1).trim();
    }
    return env;
  };

  const handleSaveMcpServer = async () => {
    if (!isElectron()) return;
    setMcpSaving(true);
    setError(null);
    try {
      const body = {
        name: mcpFormName.trim(),
        command: mcpFormCommand.trim(),
        args: mcpFormArgs.split('\n').map((s) => s.trim()).filter(Boolean),
        env: parseEnvLines(mcpFormEnv),
        cwd: mcpFormCwd.trim() || undefined,
      };
      if (mcpEditingId) {
        await getElectronBridge()!.localBackend.request({
          method: 'PUT',
          path: `/api/v2/mcp/servers/${encodeURIComponent(mcpEditingId)}`,
          body,
        });
      } else {
        await getElectronBridge()!.localBackend.request({
          method: 'POST',
          path: '/api/v2/mcp/servers',
          body,
        });
      }
      resetMcpForm();
      setMcpFormOpen(false);
      await fetchMcpServers();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setMcpSaving(false);
    }
  };

  const handleEditMcpServer = (server: McpServerInfo) => {
    setMcpEditingId(server.id);
    setMcpFormName(server.name);
    setMcpFormCommand(server.command);
    setMcpFormArgs((server.args || []).join('\n'));
    setMcpFormEnv(
      Object.entries(server.env || {})
        .map(([k, v]) => `${k}=${v}`)
        .join('\n'),
    );
    setMcpFormCwd(server.cwd || '');
    setMcpFormOpen(true);
  };

  const handleToggleMcpServer = async (server: McpServerInfo) => {
    setError(null);
    try {
      await getElectronBridge()!.localBackend.request({
        method: 'PUT',
        path: `/api/v2/mcp/servers/${encodeURIComponent(server.id)}`,
        body: { enabled: !server.enabled },
      });
      await fetchMcpServers();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const handleDeleteMcpServer = async (server: McpServerInfo) => {
    setMcpDeletingId(server.id);
    setError(null);
    try {
      await getElectronBridge()!.localBackend.request({
        method: 'DELETE',
        path: `/api/v2/mcp/servers/${encodeURIComponent(server.id)}`,
      });
      await fetchMcpServers();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setMcpDeletingId(null);
    }
  };

  const handleTestMcpServer = async (server: McpServerInfo) => {
    setMcpTestingId(server.id);
    setError(null);
    try {
      const res = await getElectronBridge()!.localBackend.request<{
        success: boolean;
        toolCount: number;
        tools: { name: string }[];
        error: string | null;
      }>({
        method: 'POST',
        path: `/api/v2/mcp/servers/${encodeURIComponent(server.id)}/test`,
      });
      if (!res.success && res.error) {
        setError(`「${server.name}」连接失败：${res.error}`);
      }
      await fetchMcpServers();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setMcpTestingId(null);
    }
  };

  const handleImportMcpJson = async () => {
    const json = mcpImportJson.trim();
    if (!json) return;
    setMcpImporting(true);
    setError(null);
    try {
      const res = await getElectronBridge()!.localBackend.request<{
        added: { name: string }[];
        skipped: string[];
      }>({
        method: 'POST',
        path: '/api/v2/mcp/servers/import',
        body: { json },
      });
      const parts: string[] = [];
      if (res.added.length > 0) parts.push(`已导入 ${res.added.length} 个：${res.added.map((s) => s.name).join('、')}`);
      if (res.skipped.length > 0) parts.push(`跳过 ${res.skipped.length} 个（重名或缺 command）：${res.skipped.join('、')}`);
      setMcpImportStatus(parts.join('；') || '没有可导入的服务');
      if (res.added.length > 0) setMcpImportJson('');
      await fetchMcpServers();
    } catch (err) {
      setMcpImportStatus(err instanceof Error ? err.message : String(err));
    } finally {
      setMcpImporting(false);
    }
  };

  return (
    <div className="space-y-3">
      {/* JSON 导入 */}
      <div className="bg-agent-muted/30 border border-agent-border/60 rounded-agent-md p-2.5 space-y-2">
        <h4 className="text-xs font-semibold text-agent-foreground flex items-center gap-1.5">
          <LuPlug className="h-3.5 w-3.5 text-agent-muted-foreground" />
          粘贴 JSON 导入 (Claude Desktop 格式)
        </h4>
        <textarea
          value={mcpImportJson}
          onChange={(e) => setMcpImportJson(e.target.value)}
          placeholder={'{\n  "mcpServers": {\n    "filesystem": {\n      "command": "npx",\n      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/"]\n    }\n  }\n}'}
          rows={5}
          disabled={mcpImporting}
          className="w-full rounded-agent-md border border-agent-border bg-agent-canvas px-3 py-2 font-mono text-[11px] text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
        />
        <div className="flex items-center justify-between gap-2">
          <p className="text-[10px] text-agent-muted-foreground">
            与 Claude Desktop / Cursor 的 mcpServers 配置格式一致，可整段粘贴。
          </p>
          <button
            type="button"
            onClick={handleImportMcpJson}
            disabled={mcpImporting || !mcpImportJson.trim()}
            className={`h-8 shrink-0 px-4 rounded-full text-xs font-medium transition-all ${
              mcpImporting || !mcpImportJson.trim()
                ? 'bg-agent-muted text-agent-muted-foreground cursor-not-allowed'
                : 'bg-agent-foreground text-agent-canvas hover:opacity-90'
            }`}
          >
            {mcpImporting ? <LuLoaderCircle className="h-3 w-3 animate-spin" /> : '导入'}
          </button>
        </div>
        {mcpImportStatus && (
          <p className="text-[10px] text-agent-muted-foreground bg-agent-muted/10 px-2 py-1 rounded border border-agent-border/20">
            {mcpImportStatus}
          </p>
        )}
      </div>

      {/* 手动添加 / 编辑表单 */}
      {mcpFormOpen ? (
        <div className="bg-agent-muted/30 border border-agent-border/60 rounded-agent-md p-2.5 space-y-2.5">
          <h4 className="text-xs font-semibold text-agent-foreground">
            {mcpEditingId ? '编辑 MCP 服务' : '手动添加 MCP 服务'}
          </h4>
          <div className="grid grid-cols-2 gap-2">
            <input
              type="text"
              value={mcpFormName}
              onChange={(e) => setMcpFormName(e.target.value)}
              placeholder="名称，如 filesystem"
              className="h-8 rounded-agent-md border border-agent-border bg-agent-canvas px-3 text-xs text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
            />
            <input
              type="text"
              value={mcpFormCommand}
              onChange={(e) => setMcpFormCommand(e.target.value)}
              placeholder="启动命令，如 npx / uvx / node"
              className="h-8 rounded-agent-md border border-agent-border bg-agent-canvas px-3 text-xs text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
            />
          </div>
          <textarea
            value={mcpFormArgs}
            onChange={(e) => setMcpFormArgs(e.target.value)}
            placeholder="参数（每行一个），如：&#10;-y&#10;@modelcontextprotocol/server-filesystem&#10;C:/"
            rows={3}
            className="w-full rounded-agent-md border border-agent-border bg-agent-canvas px-3 py-2 font-mono text-[11px] text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
          />
          <textarea
            value={mcpFormEnv}
            onChange={(e) => setMcpFormEnv(e.target.value)}
            placeholder="环境变量（每行一个 KEY=VALUE，可留空）"
            rows={2}
            className="w-full rounded-agent-md border border-agent-border bg-agent-canvas px-3 py-2 font-mono text-[11px] text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
          />
          <input
            type="text"
            value={mcpFormCwd}
            onChange={(e) => setMcpFormCwd(e.target.value)}
            placeholder="工作目录（可选）"
            className="h-8 w-full rounded-agent-md border border-agent-border bg-agent-canvas px-3 text-xs text-agent-foreground focus:outline-none focus:ring-2 focus:ring-agent-foreground/30"
          />
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => { resetMcpForm(); setMcpFormOpen(false); }}
              className="h-8 rounded-full px-4 text-xs font-medium text-agent-muted-foreground transition-colors hover:bg-agent-muted hover:text-agent-foreground"
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleSaveMcpServer}
              disabled={mcpSaving || !mcpFormName.trim() || !mcpFormCommand.trim()}
              className={`h-8 px-4 rounded-full text-xs font-medium transition-all ${
                mcpSaving || !mcpFormName.trim() || !mcpFormCommand.trim()
                  ? 'bg-agent-muted text-agent-muted-foreground cursor-not-allowed'
                  : 'bg-agent-foreground text-agent-canvas hover:opacity-90'
              }`}
            >
              {mcpSaving ? <LuLoaderCircle className="h-3 w-3 animate-spin" /> : '保存'}
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => { resetMcpForm(); setMcpFormOpen(true); }}
          className="flex items-center gap-1.5 text-xs font-medium text-agent-muted-foreground hover:text-agent-foreground transition-colors"
          data-testid="mcp-add-manual"
        >
          <LuPlus className="h-3.5 w-3.5" />
          手动添加服务
        </button>
      )}

      {/* 服务列表 */}
      <div className="space-y-2">
        <h4 className="text-xs font-semibold text-agent-muted-foreground uppercase tracking-wide">
          已注册服务
        </h4>
        {mcpLoading ? (
          <div className="flex items-center gap-2 py-4 text-xs text-agent-muted-foreground">
            <LuLoaderCircle className="h-3.5 w-3.5 animate-spin" />
            获取 MCP 服务...
          </div>
        ) : mcpServers.length === 0 ? (
          <div className="text-center py-6 text-xs text-agent-muted-foreground">
            暂无 MCP 服务，粘贴 JSON 或手动添加
          </div>
        ) : (
          <div className="divide-y divide-agent-border/40 pr-1">
            {mcpServers.map((server) => (
              <div key={server.id} className="py-2.5 flex items-start justify-between gap-3">
                <div className="space-y-1 min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <LuPlug className="h-3.5 w-3.5 text-agent-muted-foreground flex-shrink-0" />
                    <span className="text-xs font-medium text-agent-foreground truncate">
                      {server.name}
                    </span>
                    <span
                      className={`px-1 py-0.2 rounded text-[9px] font-medium flex-shrink-0 scale-95 origin-left ${
                        server.enabled
                          ? 'bg-green-500/10 text-green-600'
                          : 'bg-agent-muted text-agent-muted-foreground'
                      }`}
                    >
                      {server.enabled ? '已启用' : '已停用'}
                    </span>
                    {server.enabled && server.toolCount > 0 && (
                      <span className="px-1 py-0.2 rounded bg-agent-muted text-[9px] text-agent-muted-foreground font-medium flex-shrink-0 scale-95 origin-left">
                        {server.toolCount} 个工具
                      </span>
                    )}
                  </div>
                  <p className="text-[11px] text-agent-muted-foreground font-mono truncate">
                    {server.command} {(server.args || []).join(' ')}
                  </p>
                  {server.lastError && (
                    <p className="text-[10px] text-agent-destructive line-clamp-2">
                      连接失败：{server.lastError}
                    </p>
                  )}
                  {!server.lastError && server.toolsPreview.length > 0 && (
                    <p className="text-[10px] text-agent-muted-foreground/80 line-clamp-1">
                      工具：{server.toolsPreview.join('、')}{server.toolCount > server.toolsPreview.length ? ' …' : ''}
                    </p>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-0.5">
                  <button
                    type="button"
                    onClick={() => handleTestMcpServer(server)}
                    disabled={mcpTestingId === server.id}
                    className="text-agent-muted-foreground hover:text-agent-foreground p-1 rounded-full hover:bg-agent-muted transition-colors"
                    title="测试连接并刷新工具列表"
                  >
                    {mcpTestingId === server.id ? (
                      <LuLoaderCircle className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <LuRefreshCw className="h-3.5 w-3.5" />
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => handleEditMcpServer(server)}
                    className="text-agent-muted-foreground hover:text-agent-foreground p-1 rounded-full hover:bg-agent-muted transition-colors"
                    title="编辑"
                  >
                    <LuPencil className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    onClick={() => handleToggleMcpServer(server)}
                    className="text-agent-muted-foreground hover:text-agent-foreground p-1 rounded-full hover:bg-agent-muted transition-colors"
                    title={server.enabled ? '停用' : '启用'}
                  >
                    {server.enabled ? (
                      <LuToggleRight className="h-3.5 w-3.5" />
                    ) : (
                      <LuToggleLeft className="h-3.5 w-3.5" />
                    )}
                  </button>
                  <button
                    type="button"
                    onClick={() => handleDeleteMcpServer(server)}
                    disabled={mcpDeletingId === server.id}
                    className="text-agent-muted-foreground hover:text-agent-destructive p-1 rounded-full hover:bg-agent-muted transition-colors"
                    title="删除"
                  >
                    {mcpDeletingId === server.id ? (
                      <LuLoaderCircle className="h-3 w-3 animate-spin" />
                    ) : (
                      <LuTrash2 className="h-3.5 w-3.5" />
                    )}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      <p className="text-[10px] text-agent-muted-foreground">
        已启用服务的工具会以 mcp__服务名__工具名 的形式直接提供给助手调用；新导入或修改后点「测试连接」即可生效。
      </p>

      {error && (
        <div className="rounded-agent-md border border-agent-destructive/20 bg-agent-destructive/10 p-2.5 text-xs text-agent-destructive">
          {error}
        </div>
      )}
    </div>
  );
}
