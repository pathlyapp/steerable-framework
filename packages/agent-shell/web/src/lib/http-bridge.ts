/**
 * BS 模式的宿主 bridge：与 Electron preload（src/preload.ts）暴露的
 * `window.electron` 同一接口（ElectronBridge），但传输换成 HTTP + SSE：
 *
 *   localBackend.request   → fetch /api/v2/*
 *   localBackend.startStream → fetch（ReadableStream 逐 chunk 喂 onEvent）
 *   steerChat / approval / terminal → /host/* 端点
 *   所有宿主 → 浏览器事件  → 单条 EventSource(/api/v2/events) 按 channel 分发
 *
 * 场景包的 invoke 命名空间不进本实现（3.1）：包 web 模块在 BS 下自带
 * fetch 传输直连自己的 /host/<pack>/* 端点；包广播事件经下面的通用
 * onPackEvent（= subscribeChannel）订阅。
 *
 * server 在 index.html 里注入 `window.__DEEPPATH_BS__`（platform/brand），
 * getElectronBridge() 据此在浏览器里选中本实现。
 */
import type {
  ApprovalPromptRequest,
  AskUserPromptRequest,
  ElectronBridge,
  LocalBackendRequestInput,
  LocalBackendStreamEvent,
  TerminalSession,
  TerminalSpawnOptions,
} from './electron-bridge';

interface BsBootstrap {
  platform: NodeJS.Platform;
  flavor: string;
  brandName: string;
  /** 每次启动生成的 Bearer token；/api/v2/* 与 /host/* 全部要求携带。 */
  token?: string;
}

function bootstrap(): BsBootstrap {
  const b = typeof window !== 'undefined' ? window.__DEEPPATH_BS__ : undefined;
  return typeof b === 'object' && b
    ? b
    : { platform: 'linux', flavor: 'generic', brandName: '' };
}

function authHeaders(): Record<string, string> {
  const token = bootstrap().token;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function http<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: {
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...authHeaders(),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = (await res.json().catch(() => null)) as T & { detail?: string };
  if (!res.ok) {
    const err = new Error(
      (data as { detail?: string } | null)?.detail || `Request failed (${res.status})`,
    ) as Error & { status?: number };
    err.status = res.status;
    throw err;
  }
  return data;
}

// ---------------------------------------------------------------------------
// 事件总线：一条 EventSource 承载所有 channel（channel 名与 IPC 通道一致）。
// ---------------------------------------------------------------------------
type ChannelListener = (payload: never) => void;
const channelListeners = new Map<string, Set<ChannelListener>>();
let eventSource: EventSource | null = null;

function ensureEventSource(): EventSource {
  if (eventSource) return eventSource;
  // EventSource 不能设请求头，token 走 query（server 端两种都收）。
  const token = bootstrap().token;
  eventSource = new EventSource(
    `/api/v2/events${token ? `?token=${encodeURIComponent(token)}` : ''}`,
  );
  return eventSource;
}

function subscribeChannel<T>(channel: string, callback: (payload: T) => void): () => void {
  let set = channelListeners.get(channel);
  if (!set) {
    set = new Set();
    channelListeners.set(channel, set);
    ensureEventSource().addEventListener(channel, (event) => {
      let payload: unknown = null;
      try {
        payload = JSON.parse((event as MessageEvent).data);
      } catch {
        /* 非 JSON 帧（注释/心跳）忽略 */
      }
      const listeners = channelListeners.get(channel);
      if (!listeners) return;
      for (const fn of Array.from(listeners)) {
        try {
          (fn as (p: unknown) => void)(payload);
        } catch (err) {
          console.error(`[http-bridge] listener for ${channel} threw`, err);
        }
      }
    });
  }
  set.add(callback as ChannelListener);
  return () => {
    set.delete(callback as ChannelListener);
  };
}

// ---------------------------------------------------------------------------
// 流式聊天：fetch + ReadableStream。取消 = AbortController（server 端
// req close → abort agent 循环，对齐 Electron cancelStream 语义）。
// ---------------------------------------------------------------------------
const activeStreams = new Map<string, AbortController>();

async function startStream(
  input: LocalBackendRequestInput,
  onEvent: (payload: LocalBackendStreamEvent) => void,
): Promise<string | null> {
  const controller = new AbortController();
  const streamId = crypto.randomUUID();
  let res: Response;
  try {
    res = await fetch(input.path, {
      method: input.method,
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: input.body !== undefined ? JSON.stringify(input.body) : undefined,
      signal: controller.signal,
    });
  } catch (err) {
    onEvent({ type: 'error', error: err instanceof Error ? err.message : String(err) });
    return null;
  }
  if (!res.ok || !res.body) {
    onEvent({ type: 'error', error: `start stream failed (${res.status})` });
    return null;
  }
  activeStreams.set(streamId, controller);
  void (async () => {
    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        onEvent({ type: 'data', chunk: decoder.decode(value, { stream: true }) });
      }
      onEvent({ type: 'end', status: res.status });
    } catch (err) {
      if (controller.signal.aborted) {
        // 本地取消：server 已 abort agent 循环并落库，按正常结束上报。
        onEvent({ type: 'end', status: 200 });
      } else {
        onEvent({ type: 'error', error: err instanceof Error ? err.message : String(err) });
      }
    } finally {
      activeStreams.delete(streamId);
    }
  })();
  return streamId;
}

function cancelStream(streamId: string): void {
  activeStreams.get(streamId)?.abort();
  activeStreams.delete(streamId);
}

// ---------------------------------------------------------------------------
// 终端：ensure 返回 { session, replay }——replay 是 attach 前的输出缓冲，
// 经事件分发补发给已注册的 onData（TerminalView 先订阅后 ensure，顺序安全）。
// ---------------------------------------------------------------------------
async function terminalEnsure(options?: TerminalSpawnOptions): Promise<TerminalSession> {
  const { session, replay } = await http<{ session: TerminalSession; replay: string }>(
    'POST',
    '/host/terminal/ensure',
    options ?? {},
  );
  if (replay) {
    queueMicrotask(() => {
      const listeners = channelListeners.get('terminal:data');
      if (!listeners) return;
      for (const fn of Array.from(listeners)) {
        (fn as (p: unknown) => void)({ sessionId: session.id, chunk: replay });
      }
    });
  }
  return session;
}

export function createHttpBridge(): ElectronBridge {
  const boot = bootstrap();
  const bridge: ElectronBridge = {
    runtime: 'local',
    platform: boot.platform,

    local: {
      // 浏览器没有系统目录选择器（showDirectoryPicker 不给路径）。返回
      // canceled 让 UI 走"手动输入路径"的既有分支。
      selectDirectory: async () => ({ canceled: true, filePaths: [] }),
      saveTextFile: async (options) => {
        const blob = new Blob([options.content], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = options.defaultPath || 'deeppath-insights.json';
        a.click();
        URL.revokeObjectURL(url);
        return { canceled: false, filePath: a.download };
      },
      captureScreenshot: async () => ({
        success: false as const,
        error: '浏览器模式不支持窗口截图，请用系统截图工具。',
      }),
    },

    localBackend: {
      request: <T>(input: LocalBackendRequestInput) =>
        http<T>(input.method, input.path, input.body),
      startStream,
      cancelStream,
      steerChat: async (chatId, content) => {
        const result = await http<{ ok?: boolean }>('POST', '/host/steer', { chatId, content });
        return result.ok === true;
      },
    },

    // 会话附件：浏览器没有文件路径，renderer 把字节读成 base64 经此端点落盘。
    attachments: {
      save: (input) => http('POST', '/host/attachments/save', input),
    },

    // BS 没有原生菜单；对应快捷键由 web 内部处理，这里留 no-op。
    onMenuNewChat: () => undefined,
    offMenuNewChat: () => undefined,
    onMenuOpenTerminal: () => undefined,
    offMenuOpenTerminal: () => undefined,

    onChatTitleUpdated: (callback) =>
      subscribeChannel<{ chatId: string; title: string }>('chat-title-updated', callback),

    onSuggestedReplies: (callback) =>
      subscribeChannel<{ chatId: string; messageId: string; suggestions: string[] }>(
        'suggested-replies',
        callback,
      ),

    onChatCreated: (callback) =>
      subscribeChannel<{ chatId: string; agentId?: string | null }>('chat-created', callback),

    onTaskUpdated: (callback) =>
      subscribeChannel<{ chatId: string; taskId: string }>(
        'task-updated',
        callback,
      ),

    // 场景包广播事件（3.1 通用化）：通道名即 SSE 分发键，载荷由订阅方
    // （包）自行收窄校验。
    onPackEvent: (channel, callback) => subscribeChannel(channel, callback),

    onTaskProcess: (callback) =>
      subscribeChannel<{
        chatId: string;
        taskId: string;
        timeline: unknown;
        live: boolean;
      }>('task-process', callback),

    approval: {
      onRequest: (callback) =>
        subscribeChannel<ApprovalPromptRequest>('approval:request', callback),
      decide: async (decision) => {
        await http('POST', '/host/approval/decide', decision);
      },
    },

    askUser: {
      onRequest: (callback) =>
        subscribeChannel<AskUserPromptRequest>('ask-user:request', callback),
      answer: async (reply) => {
        await http('POST', '/host/ask-user/answer', reply);
      },
    },

    terminal: {
      ensure: terminalEnsure,
      write: (id, data) => http<boolean>('POST', '/host/terminal/write', { id, data }),
      resize: (id, cols, rows) => http<boolean>('POST', '/host/terminal/resize', { id, cols, rows }),
      onData: (callback) =>
        subscribeChannel<{ sessionId: string; chunk: string }>('terminal:data', callback),
      onExit: (callback) =>
        subscribeChannel<{ sessionId: string; code: number; signal: string | null }>(
          'terminal:exit',
          callback,
        ),
    },

  };

  // 浏览器通知等 Electron 专属字段不实现（web 端类型未声明，也无调用点）。
  return bridge;
}

let singleton: ElectronBridge | null = null;

export function getHttpBridge(): ElectronBridge {
  if (!singleton) singleton = createHttpBridge();
  return singleton;
}
