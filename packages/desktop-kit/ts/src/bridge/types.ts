import type { SSEEvent } from '@steerable/agent-protocol';

export interface LocalExecRequest {
  command: string;
  cwd?: string;
  env?: Record<string, string>;
  timeoutMs?: number;
}

export interface LocalFileReadRequest {
  path: string;
}

export interface LocalFileWriteRequest {
  path: string;
  content: string;
  createDirs?: boolean;
}

export interface LocalOpenRequest {
  target: string;
}

export interface LocalExecResult {
  success: boolean;
  stdout?: string;
  stderr?: string;
  exitCode?: number;
  error?: string;
  truncated?: boolean;
  shell?: string;
  platform?: string;
}

export interface LocalFileReadResult {
  success: boolean;
  content?: string;
  error?: string;
}

export interface LocalFileWriteResult {
  success: boolean;
  error?: string;
}

export interface LocalOpenResult {
  success: boolean;
  error?: string;
}

export interface CommandSafetyConfigPayload {
  safetyEnabled: boolean;
  allowedPatterns: string[];
  blockedCommands: string[];
}

export interface CreateLocalScriptInput {
  name: string;
  command: string;
  cwd?: string;
  env?: Record<string, string>;
}

export interface LocalScript {
  id: string;
  name: string;
  command: string;
  cwd?: string;
  env?: Record<string, string>;
  createdAt: string;
}

export interface TerminalSession {
  id: string;
  cols: number;
  rows: number;
  pid?: number;
  title: string;
}

export interface TerminalSpawnOptions {
  id?: string;
  command?: string;
  args?: string[];
  cwd?: string;
  env?: Record<string, string>;
  cols?: number;
  rows?: number;
}

export interface TerminalExecResult {
  code: number;
  signal?: string;
  stdout: string;
  stderr: string;
}

export interface LocalBackendRequestInput {
  method: string;
  path: string;
  body?: unknown;
}

export type LocalBackendStreamEvent =
  | { type: 'data'; chunk: string; parsed?: SSEEvent }
  | { type: 'end'; status: number }
  | { type: 'error'; error: string };

export interface ElectronBridge {
  runtime: 'local';
  platform: string;
  onThemeChanged: (callback: (isDark: boolean) => void) => void;
  offThemeChanged: () => void;
  retryConnection: () => void;
  checkNetworkStatus: () => Promise<{ online: boolean }>;
  onNetworkStatusChanged: (callback: (event: any, data: { online: boolean }) => void) => void;
  offNetworkStatusChanged: () => void;
  showNotification: (payload: { title: string; body?: string; icon?: string; link?: string; tag?: string }) => void;
  onNotificationClicked: (callback: (data: { link: string }) => void) => void;
  offNotificationClicked: () => void;
  onMenuNewChat: (callback: () => void) => void;
  offMenuNewChat: () => void;
  onChatTitleUpdated: (callback: (payload: { chatId: string; title: string }) => void) => () => void;
  local: {
    execShell: (request: LocalExecRequest) => Promise<{ code: number; stdout: string; stderr: string }>;
    readFile: (request: LocalFileReadRequest) => Promise<{ contents: string }>;
    writeFile: (request: LocalFileWriteRequest) => Promise<{ success: boolean }>;
    openPath: (request: LocalOpenRequest) => Promise<{ success: boolean }>;
    listScripts: () => Promise<LocalScript[]>;
    addScript: (input: CreateLocalScriptInput) => Promise<LocalScript>;
    updateScript: (id: string, updates: Partial<CreateLocalScriptInput>) => Promise<LocalScript>;
    deleteScript: (id: string) => Promise<{ deleted: boolean }>;
    runScript: (id: string) => Promise<{ code: number; stdout: string; stderr: string }>;
    getPlatformInfo: () => { platform: string };
    updateSafetyConfig: (config: CommandSafetyConfigPayload) => Promise<{ success: boolean }>;
  };
  localBackend: {
    request: <T = any>(input: LocalBackendRequestInput) => Promise<T>;
    startStream: (
      input: LocalBackendRequestInput,
      onEvent: (payload: LocalBackendStreamEvent) => void
    ) => Promise<string | null>;
    cancelStream: (streamId: string) => void;
    traces: {
      list: (chatId: string, limit?: number) => Promise<unknown>;
      get: (traceId: string) => Promise<unknown>;
    };
  };
  terminal: {
    openWindow: () => Promise<{ success: boolean }>;
    list: () => Promise<TerminalSession[]>;
    spawn: (options?: TerminalSpawnOptions) => Promise<TerminalSession>;
    ensure: (options?: TerminalSpawnOptions) => Promise<TerminalSession>;
    write: (id: string, data: string) => Promise<boolean>;
    resize: (id: string, cols: number, rows: number) => Promise<boolean>;
    kill: (id: string) => Promise<boolean>;
    exec: (payload: { id?: string; command: string; timeoutMs?: number }) => Promise<TerminalExecResult>;
    onData: (callback: (payload: { sessionId: string; chunk: string }) => void) => () => void;
    onExit: (callback: (payload: { sessionId: string; code: number; signal: string | null }) => void) => () => void;
    onSpawned: (callback: (session: TerminalSession) => void) => () => void;
  };
}
