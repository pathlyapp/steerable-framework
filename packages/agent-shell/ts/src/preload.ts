import { contextBridge, ipcRenderer } from 'electron';
import type {
  LocalExecRequest,
  LocalFileReadRequest,
  LocalFileWriteRequest,
  LocalOpenRequest,
  CommandSafetyConfigPayload,
} from './local-executor.js';
import type { CreateLocalScriptInput } from './local-script-registry.js';
import type {
  TerminalSession,
  TerminalSpawnOptions,
  TerminalExecResult,
} from './terminal-manager.js';
import type { SSEEvent } from '@steerable/agent-protocol';

type LocalBackendRequestInput = { method: string; path: string; body?: unknown };
type LocalBackendStreamEvent =
  | { type: 'data'; chunk: string; parsed?: SSEEvent }
  | { type: 'end'; status: number }
  | { type: 'error'; error: string };

// ---------------------------------------------------------------------------
// Demo bypass: pre-seed the renderer's localStorage with a long-lived JWT so
// the bundled web UI can talk to deeppath-api without going through the login
// flow. This is intentionally limited to the agent shell and only activates
// when AGENT_DEMO_TOKEN is set in the launch environment. Remove the env var
// (or leave it unset) for a normal authenticated experience.
// ---------------------------------------------------------------------------
const DEMO_TOKEN_KEY = 'deeppath_access_token';
const DEMO_USER_KEY = 'deeppath_user';
const demoToken = process.env.AGENT_DEMO_TOKEN;
const demoUserJson = process.env.AGENT_DEMO_USER;

function seedDemoCredentials(): void {
  if (!demoToken) return;
  try {
    if (window.localStorage.getItem(DEMO_TOKEN_KEY) !== demoToken) {
      window.localStorage.setItem(DEMO_TOKEN_KEY, demoToken);
    }
    if (demoUserJson && !window.localStorage.getItem(DEMO_USER_KEY)) {
      window.localStorage.setItem(DEMO_USER_KEY, demoUserJson);
    }
    const expires = new Date(Date.now() + 30 * 864e5).toUTCString();
    document.cookie = `${DEMO_TOKEN_KEY}=${encodeURIComponent(demoToken)}; expires=${expires}; path=/; SameSite=Lax`;
  } catch (err) {
    console.error('[agent-shell] failed to seed demo token', err);
  }
}

try {
  seedDemoCredentials();
} catch {
  // window/document may not be ready yet; retry on DOMContentLoaded.
}
window.addEventListener('DOMContentLoaded', seedDemoCredentials);

/**
 * 构建并暴露 window.electron API（2.3 pack-preload 组合）。
 *
 * 包贡献经参数合并进顶层命名空间（如某包给出 `{ <pack>, <pack>Mock }`
 * 这样的 invoke 型命名空间）；shell 本体不含任何
 * 包符号。由产品 preload 入口（products/<id>/preload.ts）或 dev 入口
 * （devtools/dev-preload.ts）调用。
 */
export function buildPreloadApi(packContributions: Record<string, unknown> = {}): void {
const electronAPI = {
  runtime: 'local' as const,
  platform: process.platform,
  onThemeChanged: (callback: (isDark: boolean) => void) => {
    ipcRenderer.on('theme-changed', (_event, isDark) => callback(isDark));
  },
  offThemeChanged: () => {
    ipcRenderer.removeAllListeners('theme-changed');
  },
  retryConnection: () => {
    ipcRenderer.send('retry-connection');
  },
  checkNetworkStatus: async (): Promise<{ online: boolean }> => {
    return await ipcRenderer.invoke('check-network-status');
  },
  onNetworkStatusChanged: (callback: (event: Electron.IpcRendererEvent, data: { online: boolean }) => void) => {
    ipcRenderer.on('network-status-changed', callback);
  },
  offNetworkStatusChanged: () => {
    ipcRenderer.removeAllListeners('network-status-changed');
  },
  showNotification: (payload: { title: string; body?: string; icon?: string; link?: string; tag?: string }) => {
    ipcRenderer.send('show-notification', payload);
  },
  onNotificationClicked: (callback: (data: { link: string }) => void) => {
    ipcRenderer.on('notification-clicked', (_event, data: { link: string }) => callback(data));
  },
  offNotificationClicked: () => {
    ipcRenderer.removeAllListeners('notification-clicked');
  },
  onMenuNewChat: (callback: () => void) => {
    ipcRenderer.on('menu:new-chat', () => callback());
  },
  offMenuNewChat: () => {
    ipcRenderer.removeAllListeners('menu:new-chat');
  },
  onMenuOpenTerminal: (callback: () => void) => {
    ipcRenderer.on('menu:open-terminal', () => callback());
  },
  offMenuOpenTerminal: () => {
    ipcRenderer.removeAllListeners('menu:open-terminal');
  },
  /**
   * AI 标题异步就绪。Backend 在每条 chat 的首条助手回复完成后 fire-and-forget
   * 跑 LLM 生成标题；完成后通过这个通道把 `{chatId, title}` 广播到所有 renderer。
   *
   * 返回 unsubscribe 函数——和 onMenuNewChat 不一样，这个通道有可能多处订阅
   * （主窗 + 未来的设置窗），所以用 add/remove 配对而不是 removeAllListeners。
   */
  onChatTitleUpdated: (
    callback: (payload: { chatId: string; title: string }) => void,
  ) => {
    const handler = (
      _event: Electron.IpcRendererEvent,
      payload: { chatId?: string; title?: string },
    ) => {
      if (typeof payload?.chatId === 'string' && typeof payload?.title === 'string') {
        callback({ chatId: payload.chatId, title: payload.title });
      }
    };
    ipcRenderer.on('chat-title-updated', handler);
    return () => {
      ipcRenderer.removeListener('chat-title-updated', handler);
    };
  },
  /**
   * 回合追问建议就绪。Backend 在助手回复完成后 fire-and-forget 生成 3 条
   * 下一轮用户输入；启发式兜底会立刻推一次，LLM 成功后再替换。返回 unsubscribe。
   */
  onSuggestedReplies: (
    callback: (payload: {
      chatId: string;
      messageId: string;
      suggestions: string[];
    }) => void,
  ) => {
    const handler = (
      _event: Electron.IpcRendererEvent,
      payload: { chatId?: string; messageId?: string; suggestions?: unknown },
    ) => {
      if (
        typeof payload?.chatId === 'string' &&
        typeof payload?.messageId === 'string' &&
        Array.isArray(payload.suggestions) &&
        payload.suggestions.every((item) => typeof item === 'string')
      ) {
        callback({
          chatId: payload.chatId,
          messageId: payload.messageId,
          suggestions: payload.suggestions,
        });
      }
    };
    ipcRenderer.on('suggested-replies', handler);
    return () => {
      ipcRenderer.removeListener('suggested-replies', handler);
    };
  },
  /**
   * 会话补建通知：Backend 在向本地不存在的 chatId 首次发送时按 URL 里的 id
   * 现场补建会话，并广播 `chat-created`（{chatId, agentId}）。返回 unsubscribe
   * （多处订阅用 add/remove 配对）。
   */
  onChatCreated: (
    callback: (payload: { chatId: string; agentId?: string | null }) => void,
  ) => {
    const handler = (
      _event: Electron.IpcRendererEvent,
      payload: { chatId?: string; agentId?: string | null },
    ) => {
      if (typeof payload?.chatId === 'string' && payload.chatId) {
        callback({
          chatId: payload.chatId,
          agentId: typeof payload.agentId === 'string' ? payload.agentId : null,
        });
      }
    };
    ipcRenderer.on('chat-created', handler);
    return () => {
      ipcRenderer.removeListener('chat-created', handler);
    };
  },
  /**
   * 场景包广播事件订阅（3.1 通用化）：按通道名订阅主进程广播，载荷
   * 原样透传（收窄校验在订阅方——包——一侧）。通道名约定带包前缀
   * （如 `<pack>:updated`）。返回解除订阅函数。
   */
  onPackEvent: (channel: string, callback: (payload: unknown) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, payload: unknown) => {
      callback(payload);
    };
    ipcRenderer.on(channel, handler);
    return () => {
      ipcRenderer.removeListener(channel, handler);
    };
  },
  /**
   * 4.6a 后台任务状态推送：任务到达终态（completed/failed）或 worktree
   * 合并/丢弃完成时，主进程广播 `task-updated`。载荷只带 chatId/taskId
   * ——renderer 收到后重新拉任务列表（GET /chats/:id/tasks），
   * 不把整条任务记录塞进 IPC（避免与 SQLite 双写漂移）。
   */
  onTaskUpdated: (
    callback: (payload: { chatId: string; taskId: string }) => void,
  ) => {
    const handler = (
      _event: Electron.IpcRendererEvent,
      payload: { chatId?: string; taskId?: string },
    ) => {
      if (typeof payload?.chatId === 'string' && typeof payload?.taskId === 'string') {
        callback({ chatId: payload.chatId, taskId: payload.taskId });
      }
    };
    ipcRenderer.on('task-updated', handler);
    return () => {
      ipcRenderer.removeListener('task-updated', handler);
    };
  },
  onTaskProcess: (
    callback: (payload: {
      chatId: string;
      taskId: string;
      timeline: unknown;
      live: boolean;
    }) => void,
  ) => {
    const handler = (
      _event: Electron.IpcRendererEvent,
      payload: { chatId?: string; taskId?: string; timeline?: unknown; live?: boolean },
    ) => {
      if (typeof payload?.chatId !== 'string' || typeof payload?.taskId !== 'string') return;
      callback({
        chatId: payload.chatId,
        taskId: payload.taskId,
        timeline: payload.timeline,
        live: payload.live === true,
      });
    };
    ipcRenderer.on('task-process', handler);
    return () => {
      ipcRenderer.removeListener('task-process', handler);
    };
  },
  /**
   * W4-1 审批代数：sidecar 的 ApprovalExecutor 经反向通道请示，主进程把
   * 请求广播到 renderer（approval:request），审批弹窗应答后走
   * approval:decide 回到主进程。订阅返回 unsubscribe；同一时刻只有一个
   * 弹窗监听者（AgentPage 挂载的 ApprovalModalHost）。
   */
  approval: {
    onRequest: (
      callback: (request: {
        requestId: string;
        toolName: string;
        arguments: Record<string, unknown>;
        mode: string;
        category: string;
        round: number;
      }) => void,
    ) => {
      const handler = (_event: Electron.IpcRendererEvent, payload: unknown) => {
        const p = payload as { requestId?: unknown; toolName?: unknown };
        if (typeof p?.requestId === 'string' && typeof p?.toolName === 'string') {
          callback(payload as Parameters<typeof callback>[0]);
        }
      };
      ipcRenderer.on('approval:request', handler);
      return () => {
        ipcRenderer.removeListener('approval:request', handler);
      };
    },
    decide: async (decision: {
      requestId: string;
      kind:
        | 'allow_once'
        | 'allow_for_session'
        | 'allow_always'
        | 'deny_once'
        | 'deny_for_session'
        | 'deny_always'
        | 'abort';
      reason?: string;
    }): Promise<void> => {
      await ipcRenderer.invoke('approval:decide', decision);
    },
  },
  /**
   * W8 结构化提问：sidecar 的 ask_user 工具经反向通道请示，主进程把
   * 请求广播到 renderer（ask-user:request），问题卡片应答后走
   * ask-user:answer 回到主进程。订阅返回 unsubscribe；同一时刻只有一个
   * 卡片监听者（AgentLayout 挂载的 AskUserModalHost）。
   */
  askUser: {
    onRequest: (
      callback: (request: {
        requestId: string;
        intro: string;
        questions: Array<Record<string, unknown>>;
      }) => void,
    ) => {
      const handler = (_event: Electron.IpcRendererEvent, payload: unknown) => {
        const p = payload as { requestId?: unknown; questions?: unknown };
        if (typeof p?.requestId === 'string' && Array.isArray(p?.questions)) {
          callback(payload as Parameters<typeof callback>[0]);
        }
      };
      ipcRenderer.on('ask-user:request', handler);
      return () => {
        ipcRenderer.removeListener('ask-user:request', handler);
      };
    },
    answer: async (reply: {
      requestId: string;
      answers: Record<string, string | string[]>;
    }): Promise<void> => {
      await ipcRenderer.invoke('ask-user:answer', reply);
    },
  },
  local: {
    selectDirectory: async (options?: { title?: string }) => {
      return await ipcRenderer.invoke('local:select-directory', options);
    },
    saveTextFile: async (options: {
      title?: string;
      defaultPath?: string;
      content: string;
    }) => {
      return await ipcRenderer.invoke('local:save-text-file', options);
    },
    captureScreenshot: async (rect?: {
      x: number;
      y: number;
      width: number;
      height: number;
    }) => {
      return await ipcRenderer.invoke(
        'local:capture-screenshot',
        rect ? { rect } : {},
      );
    },
    execShell: async (request: LocalExecRequest) => {
      return await ipcRenderer.invoke('local:exec-shell', request);
    },
    readFile: async (request: LocalFileReadRequest) => {
      return await ipcRenderer.invoke('local:read-file', request);
    },
    writeFile: async (request: LocalFileWriteRequest) => {
      return await ipcRenderer.invoke('local:write-file', request);
    },
    openPath: async (request: LocalOpenRequest) => {
      return await ipcRenderer.invoke('local:open-path', request);
    },
    listScripts: async () => {
      return await ipcRenderer.invoke('local:list-scripts');
    },
    addScript: async (input: CreateLocalScriptInput) => {
      return await ipcRenderer.invoke('local:add-script', input);
    },
    updateScript: async (id: string, updates: Partial<CreateLocalScriptInput>) => {
      return await ipcRenderer.invoke('local:update-script', { id, updates });
    },
    deleteScript: async (id: string) => {
      return await ipcRenderer.invoke('local:delete-script', id);
    },
    runScript: async (id: string) => {
      return await ipcRenderer.invoke('local:run-script', id);
    },
    getPlatformInfo: () => ({ platform: process.platform }),
    updateSafetyConfig: async (config: CommandSafetyConfigPayload) => {
      return await ipcRenderer.invoke('local:update-safety-config', config);
    },
  },
  /** 会话附件：把源文件路径 / base64 字节拷贝进会话空间（`attachments:save`）。 */
  attachments: {
    save: async (input: {
      chatId: string;
      files: Array<{ path?: string; name?: string; data?: string }>;
    }) => {
      return await ipcRenderer.invoke('attachments:save', input);
    },
  },
  localBackend: {
    request: async <T>(input: LocalBackendRequestInput): Promise<T> => {
      const result = await ipcRenderer.invoke('local-backend:request', input) as {
        ok: boolean;
        status: number;
        data?: T;
        error?: string;
      };
      if (!result.ok) {
        const err = new Error(result.error || 'Local backend request failed') as Error & { status?: number };
        err.status = result.status;
        throw err;
      }
      return result.data as T;
    },
    /**
     * 启动一条本地后端流。
     * 注意：Electron contextBridge 不支持把 ReadableStream 跨上下文传给 renderer
     * （structured clone 会把它剥成空对象，导致 web UI 拿到的 Response.body 永远不出数据）。
     * 这里只暴露 `startStream(input, onEvent)` 原语，由 web UI 端在 renderer 上下文
     * 内自己构造 ReadableStream 并喂入 onEvent 回调（callback 跨上下文是 proxied 的）。
     *
     * 返回 streamId 用于后续 cancelStream(id)。
     */
    startStream: async (
      input: LocalBackendRequestInput,
      onEvent: (payload: LocalBackendStreamEvent) => void
    ): Promise<string | null> => {
      const result = (await ipcRenderer.invoke('local-backend:stream', input)) as {
        ok: boolean;
        streamId?: string;
        status?: number;
        error?: string;
      };
      if (!result.ok || !result.streamId) {
        try {
          onEvent({ type: 'error', error: result.error || `start stream failed (${result.status ?? 500})` });
        } catch {
          // best-effort: callback 抛错不应影响 IPC 路径
        }
        return null;
      }
      const channel = `local-backend:stream:${result.streamId}`;
      const handler = (_event: unknown, payload: Parameters<typeof onEvent>[0]) => {
        try {
          onEvent(payload);
        } catch (err) {
          console.error('[preload] stream event callback threw', err);
        }
        if (payload.type === 'end' || payload.type === 'error') {
          ipcRenderer.removeListener(channel, handler);
        }
      };
      ipcRenderer.on(channel, handler);
      return result.streamId;
    },
    cancelStream: (streamId: string): void => {
      ipcRenderer.send(`local-backend:stream:${streamId}:cancel`);
    },
    /**
     * 轮中转向：把一条用户消息注入该 chat 正在运行的 CoreLoop 回合。
     * 返回 true 表示已被接受（UI 立即上屏）；false 表示当前没有可转向的
     * 回合（TS 循环路径或回合刚结束）——调用方应保留草稿。
     */
    steerChat: async (chatId: string, content: string): Promise<boolean> => {
      const result = (await ipcRenderer.invoke('local-backend:steer', {
        chatId,
        content,
      })) as { ok?: boolean };
      return result?.ok === true;
    },
    traces: {
      list: async (chatId: string, limit = 50): Promise<unknown> => {
        const path = `/api/v2/local/traces?chatId=${encodeURIComponent(chatId)}&limit=${limit}`;
        return await electronAPI.localBackend.request({ method: 'GET', path });
      },
      get: async (traceId: string): Promise<unknown> => {
        const path = `/api/v2/local/traces/${encodeURIComponent(traceId)}`;
        return await electronAPI.localBackend.request({ method: 'GET', path });
      },
    },
  },
  agent: {
    hasToken: async (): Promise<boolean> => {
      return true;
    },
    provision: async (_userJWT: string) => {
      return { success: true };
    },
    start: async () => {
      return { success: true };
    },
    getStatus: async (): Promise<{ status: string; agentId: string | null }> => {
      return { status: 'registered', agentId: 'local-agent' };
    },
    getInfo: async () => {
      return {
        id: 'local-agent',
        machineId: 'local',
        name: 'Local Agent',
        platform: process.platform,
        hostname: 'localhost',
        shell: process.platform === 'darwin' ? 'zsh' : 'bash',
        isOnline: true,
      };
    },
    onNeedProvision: (_callback: () => void) => undefined,
    offNeedProvision: () => undefined,
  },
  terminal: {
    list: async (): Promise<TerminalSession[]> => {
      return await ipcRenderer.invoke('terminal:list');
    },
    spawn: async (options: TerminalSpawnOptions = {}): Promise<TerminalSession> => {
      return await ipcRenderer.invoke('terminal:spawn', options);
    },
    ensure: async (options: TerminalSpawnOptions = {}): Promise<TerminalSession> => {
      return await ipcRenderer.invoke('terminal:ensure', options);
    },
    write: async (id: string, data: string): Promise<boolean> => {
      return await ipcRenderer.invoke('terminal:write', { id, data });
    },
    resize: async (id: string, cols: number, rows: number): Promise<boolean> => {
      return await ipcRenderer.invoke('terminal:resize', { id, cols, rows });
    },
    kill: async (id: string): Promise<boolean> => {
      return await ipcRenderer.invoke('terminal:kill', id);
    },
    exec: async (
      payload: { id?: string; command: string; timeoutMs?: number }
    ): Promise<TerminalExecResult> => {
      return await ipcRenderer.invoke('terminal:exec', payload);
    },
    onData: (callback: (payload: { sessionId: string; chunk: string }) => void) => {
      const fn = (_event: Electron.IpcRendererEvent, payload: { sessionId: string; chunk: string }) =>
        callback(payload);
      ipcRenderer.on('terminal:data', fn);
      return () => ipcRenderer.removeListener('terminal:data', fn);
    },
    onExit: (callback: (payload: { sessionId: string; code: number; signal: string | null }) => void) => {
      const fn = (
        _event: Electron.IpcRendererEvent,
        payload: { sessionId: string; code: number; signal: string | null }
      ) => callback(payload);
      ipcRenderer.on('terminal:exit', fn);
      return () => ipcRenderer.removeListener('terminal:exit', fn);
    },
    onSpawned: (callback: (session: TerminalSession) => void) => {
      const fn = (_event: Electron.IpcRendererEvent, session: TerminalSession) => callback(session);
      ipcRenderer.on('terminal:spawned', fn);
      return () => ipcRenderer.removeListener('terminal:spawned', fn);
    },
  },
};

contextBridge.exposeInMainWorld('electron', { ...electronAPI, ...packContributions });
}

/**
 * window.electron 的 API 面。shell 本体不含包符号（2.3 起包段由产品
 * preload 入口组合）；渲染端的完整类型镜像维护在
 * apps/web/src/lib/electron-bridge.ts。
 */
export type ElectronAPI = Record<string, unknown>;

declare global {
  interface Window {
    electron: ElectronAPI;
  }
}
