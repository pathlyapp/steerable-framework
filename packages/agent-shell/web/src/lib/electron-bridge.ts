/**
 * Typed view of `window.electron`, the bridge that Electron's preload script
 * (src/preload.ts in the main project) exposes via `contextBridge`.
 *
 * This file MUST stay a subset / aligned shape with `ElectronAPI` in
 * `../../../src/preload.ts`. We intentionally don't TypeScript-reference that
 * file across workspaces — it would drag the whole main-process source tree
 * into the renderer's compile graph. Treat this as a contract surface and
 * extend it lazily as apps/web starts using new bridge methods.
 *
 * Outside Electron (e.g. plain `vite dev` in a browser tab without preload),
 * `window.electron` is undefined. Always read it via `getElectronBridge()`
 * and handle the `null` case so the SPA stays demoable in a normal browser.
 */

import type { SSEEvent } from '@steerable/agent-protocol';
import { getHttpBridge } from './http-bridge';

export type LocalBackendRequestInput = {
  method: string;
  path: string;
  body?: unknown;
};

export type LocalBackendStreamEvent =
  | { type: 'data'; chunk: string; parsed?: SSEEvent }
  | { type: 'end'; status: number }
  | { type: 'error'; error: string };

/**
 * W4-1: mirror of the approval algebra's decision variants
 * (`steerable_agent_runtime.approval.APPROVAL_KINDS` minus `timed_out`,
 * which the sidecar synthesizes itself on timeout).
 */
export type ApprovalDecisionKind =
  | 'allow_once'
  | 'allow_for_session'
  | 'allow_always'
  | 'deny_once'
  | 'deny_for_session'
  | 'deny_always'
  | 'abort';

export interface ApprovalPromptRequest {
  requestId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  mode: string;
  category: string;
  round: number;
}

/**
 * W8: mirror of the sidecar's `ask_user.request` reverse-call payload
 * (`HostAskUserHandler` in steerable_sidecar.host_tools). `questions`
 * entries match `AskUserQuestionsPayload['questions'][number]` from
 * `@steerable/agent-protocol`; kept structurally typed here so the
 * renderer's compile graph stays free of main-process imports.
 */
export interface AskUserPromptRequest {
  requestId: string;
  intro: string;
  questions: Array<Record<string, unknown>>;
}

/**
 * Mirror of `TerminalSession` / `TerminalSpawnOptions` in
 * `src/terminal-manager.ts`. Kept inline (rather than cross-imported)
 * for the same reason as the rest of this file — we don't want the
 * renderer's compile graph to pull in main-process modules.
 */
export interface TerminalSession {
  id: string;
  shell: string;
  pid: number;
  cwd: string;
  cols: number;
  rows: number;
}

export interface TerminalSpawnOptions {
  shell?: string;
  cwd?: string;
  env?: Record<string, string>;
  cols?: number;
  rows?: number;
}

export interface ElectronBridge {
  runtime: 'local';
  platform: NodeJS.Platform;
  local?: {
    selectDirectory: (options?: {
      title?: string;
    }) => Promise<{ canceled: boolean; filePaths: string[] }>;
    saveTextFile?: (options: {
      title?: string;
      defaultPath?: string;
      content: string;
    }) => Promise<{ canceled: boolean; filePath?: string }>;
    /** 截取窗口（或指定 DIP 区域）并写入系统剪贴板。 */
    captureScreenshot: (rect?: {
      x: number;
      y: number;
      width: number;
      height: number;
    }) => Promise<
      | { success: true; width: number; height: number }
      | { success: false; error: string }
    >;
  };
  localBackend: {
    request: <T>(input: LocalBackendRequestInput) => Promise<T>;
    startStream: (
      input: LocalBackendRequestInput,
      onEvent: (payload: LocalBackendStreamEvent) => void,
    ) => Promise<string | null>;
    cancelStream: (streamId: string) => void;
    /** 轮中转向：注入一条用户消息到运行中的 CoreLoop 回合；false = 无可转向回合。 */
    steerChat?: (chatId: string, content: string) => Promise<boolean>;
  };
  /** 会话附件：把源文件路径 / base64 字节拷贝进会话空间（`attachments:save`）。 */
  attachments?: {
    save: (input: {
      chatId: string;
      files: Array<{ path?: string; name?: string; data?: string }>;
    }) => Promise<{
      files: Array<{ name: string; path: string; size: number; error?: string }>;
    }>;
  };
  /**
   * App menu IPC — `Cmd+N` / 文件 → 新建对话 fires `menu:new-chat`
   * (see src/main.ts:createMenu). Renderer subscribes via
   * `onMenuNewChat(cb)` and *must* call `offMenuNewChat()` on unmount
   * to clear the listener (otherwise multiple AgentSidebar mounts
   * during HMR would queue up duplicate callbacks).
   */
  onMenuNewChat?: (callback: () => void) => void;
  offMenuNewChat?: () => void;
  /**
   * App menu IPC — `Cmd+T` / 视图 → 打开终端 fires `menu:open-terminal`.
   * The terminal is a toggleable panel inside AgentLayout (not a separate
   * window, not a route), so the renderer just flips layout state. Same
   * subscribe/off pairing as `onMenuNewChat`.
   */
  onMenuOpenTerminal?: (callback: () => void) => void;
  offMenuOpenTerminal?: () => void;
  /**
   * 异步 AI 标题就绪通知。Backend 在每条 chat 首条助手回复完成后 fire-and-forget
   * 跑 LLM 生成标题；完成时通过这个通道把 `{chatId, title}` 推过来。返回的函数
   * 解除订阅（preload 用 add/remove 配对，不是 removeAllListeners——可以多处订阅）。
   */
  onChatTitleUpdated?: (
    callback: (payload: { chatId: string; title: string }) => void,
  ) => () => void;
  /**
   * 回合追问建议就绪。Backend 在助手回复完成后推 `{chatId, messageId, suggestions}`
   * （先启发式、后 LLM 替换）。返回解除订阅函数。
   */
  onSuggestedReplies?: (
    callback: (payload: {
      chatId: string;
      messageId: string;
      suggestions: string[];
    }) => void,
  ) => () => void;
  /**
   * 会话补建通知。Backend 在「向一个本地不存在的 chatId 首次发送」时按 URL
   * 里的 id 现场补建会话（见 router.handleStream），随后广播 `chat-created`
   * （{chatId, agentId}）；渲染端据此 refresh 一次侧栏，避免"URL 能聊、列表
   * 里却找不到这条会话"。返回解除订阅函数。
   */
  onChatCreated?: (
    callback: (payload: { chatId: string; agentId?: string | null }) => void,
  ) => () => void;
  /**
   * 4.6a 后台任务状态推送。任务到达终态或 worktree 合并/丢弃完成时主进程
   * 广播；载荷只有 chatId/taskId，面板收到后重新拉列表。
   */
  onTaskUpdated?: (
    callback: (payload: { chatId: string; taskId: string }) => void,
  ) => () => void;
  /**
   * 场景包广播事件订阅（3.1 通用化）：按通道名订阅主进程/BS 后端的
   * 广播事件，载荷由订阅方（包）自行收窄校验。shell 不知道包的事件
   * 语义——包的通道名约定带包前缀（如 `<pack>:updated`）。返回解除
   * 订阅函数。
   */
  onPackEvent?: (
    channel: string,
    callback: (payload: unknown) => void,
  ) => () => void;
  /**
   * 后台任务推理时间线推送。任务流的 reasoning / 工具 / 文本增量经主进程
   * 广播；右侧过程栏订阅后不用轮询。
   */
  onTaskProcess?: (
    callback: (payload: {
      chatId: string;
      taskId: string;
      timeline: unknown;
      live: boolean;
    }) => void,
  ) => () => void;
  /**
   * W4-1 审批代数：sidecar ApprovalExecutor 的请示经主进程广播到 renderer。
   * onRequest 订阅返回 unsubscribe；decide 把用户的 7 变体决定送回主进程。
   */
  approval?: {
    onRequest: (callback: (request: ApprovalPromptRequest) => void) => () => void;
    decide: (decision: {
      requestId: string;
      kind: ApprovalDecisionKind;
      reason?: string;
    }) => Promise<void>;
  };
  /**
   * W8 结构化提问：sidecar ask_user 工具的请示经主进程/BS 服务器广播到
   * renderer。onRequest 订阅返回 unsubscribe；answer 把答案映射送回
   * （空映射 = 用户未作答，模型自行推进）。
   */
  askUser?: {
    onRequest: (callback: (request: AskUserPromptRequest) => void) => () => void;
    answer: (reply: {
      requestId: string;
      answers: Record<string, string | string[]>;
    }) => Promise<void>;
  };
  terminal?: {
    /** Ensures a session exists (reuses last one if alive). */
    ensure: (options?: TerminalSpawnOptions) => Promise<TerminalSession>;
    write: (id: string, data: string) => Promise<boolean>;
    resize: (id: string, cols: number, rows: number) => Promise<boolean>;
    onData: (
      callback: (payload: { sessionId: string; chunk: string }) => void,
    ) => () => void;
    onExit: (
      callback: (payload: {
        sessionId: string;
        code: number;
        signal: string | null;
      }) => void,
    ) => () => void;
  };
  // 场景包的 invoke 命名空间（如包 preload 贡献的 `<pack>` / `<pack>Mock`）
  // 不进本接口——包用自己的结构化收窄访问器（见各包 web/bridge.ts），
  // shell 桥接口保持产品中立（3.1）。
}

declare global {
  interface Window {
    electron?: ElectronBridge;
    /**
     * BS server（src/server/）托管 index.html 时注入的引导信息；存在即代表
     * 当前跑在浏览器-服务器模式，getElectronBridge() 会返回 HTTP 实现。
     */
    __DEEPPATH_BS__?: {
      platform: NodeJS.Platform;
      flavor: string;
      brandName: string;
    };
  }
}

export function getElectronBridge(): ElectronBridge | null {
  if (typeof window === 'undefined') return null;
  if (window.electron) return window.electron;
  if (window.__DEEPPATH_BS__) return getHttpBridge();
  return null;
}

export function isElectron(): boolean {
  return getElectronBridge() !== null;
}
