import type {
  ChatAgentRecord,
  ChatMessageRecord,
  ChatSessionRecord,
  HarnessTraceRecord,
  InsightOutboxRow,
  TaskRecord,
} from './index.js';
import type {
  InsightKind,
  InsightsSettings,
  InsightsSettingsPatch,
} from './insights-settings.js';
import type { LlmSettings } from './llm-settings.js';
import type { TelemetrySettings } from './telemetry-settings.js';
import type { UsageSummary } from './usage-summary.js';
import type { WebSearchSettings } from './web-search-settings.js';
import type { TenantScope } from './driver.js';

/** Explicit asynchronous storage API bound to one tenant and user. */
export interface ScopedStore {
  readonly scope: TenantScope;
  listChats(page?: number, limit?: number): Promise<{ chats: ChatSessionRecord[]; total: number }>;
  createChat(title?: string, agentId?: string | null, projectId?: string | null): Promise<ChatSessionRecord>;
  createChatWithId(chatId: string, title?: string, agentId?: string | null, projectId?: string | null): Promise<ChatSessionRecord>;
  clearProjectAssignment(projectId: string): Promise<number>;
  getChat(chatId: string): Promise<ChatSessionRecord | null>;
  updateChat(chatId: string, updates: Partial<Pick<ChatSessionRecord, 'title' | 'systemPrompt' | 'pinnedRefs' | 'isPinned' | 'projectId'>>): Promise<ChatSessionRecord | null>;
  deleteChat(chatId: string): Promise<boolean>;
  chatHasMessages(chatId: string): Promise<boolean>;
  deleteChatIfEmpty(chatId: string): Promise<boolean>;
  deleteEmptyChats(exceptChatId?: string | null): Promise<string[]>;
  getChatRecordId(chatId: string): Promise<string | null>;
  setChatRecordId(chatId: string, recordId: string): Promise<void>;
  setTurnActive(chatId: string): Promise<void>;
  clearTurnActive(chatId: string): Promise<void>;
  getTurnActive(chatId: string): Promise<{ startedAt: string } | null>;
  listMessages(chatId: string, limit?: number): Promise<ChatMessageRecord[]>;
  getMessage(chatId: string, messageId: string): Promise<ChatMessageRecord | null>;
  deleteMessagesFrom(chatId: string, messageId: string): Promise<number>;
  addMessage(chatId: string, role: ChatMessageRecord['role'], content: string, messageMetadata?: string | null): Promise<ChatMessageRecord>;
  patchMessageMetadata(chatId: string, messageId: string, patch: Record<string, unknown>): Promise<ChatMessageRecord | null>;
  replaceChatMessages(chatId: string, messages: Array<{ role: ChatMessageRecord['role']; content: string }>): Promise<void>;
  listChatAgents(includeArchived?: boolean): Promise<ChatAgentRecord[]>;
  getChatAgent(agentId: string): Promise<ChatAgentRecord | null>;
  createChatAgent(input: Partial<ChatAgentRecord> & { name: string }): Promise<ChatAgentRecord>;
  updateChatAgent(agentId: string, updates: Partial<ChatAgentRecord>): Promise<ChatAgentRecord | null>;
  archiveChatAgent(agentId: string): Promise<boolean>;
  saveTrace(input: { id: string; chatId: string; messageId?: string | null; startedAtMs: number; durationMs?: number | null; status: string; payload: Record<string, unknown> }): Promise<HarnessTraceRecord>;
  listTracesByChat(chatId: string, limit?: number): Promise<HarnessTraceRecord[]>;
  getTrace(traceId: string): Promise<HarnessTraceRecord | null>;
  createTask(input: { chatId: string; task: string; worktreePath?: string | null; worktreeBranch?: string | null; recordId?: string | null; dependsOn?: string[] | null; initialStatus?: 'blocked' | 'running' }): Promise<TaskRecord>;
  getTask(taskId: string): Promise<TaskRecord | null>;
  listTasks(chatId?: string, limit?: number): Promise<TaskRecord[]>;
  updateTask(taskId: string, updates: Partial<Pick<TaskRecord, 'status' | 'answer' | 'error' | 'worktreeState' | 'traceId' | 'recordId'>>): Promise<TaskRecord | null>;
  saveTaskProcess(taskId: string, processJson: string): Promise<void>;
  failRunningTasks(reason: string): Promise<number>;
  recordUsageEvent(input: { chatId?: string | null; kind: string; provider?: string | null; model?: string | null; promptTokens: number; completionTokens: number; totalTokens: number; cachedPromptTokens?: number; costUsd?: number | null }): Promise<void>;
  getUsageSummary(sinceDays?: number): Promise<UsageSummary>;
  getLlmSettings(): Promise<LlmSettings | null>;
  setLlmSettings(settings: LlmSettings): Promise<LlmSettings>;
  getTelemetrySettings(): Promise<TelemetrySettings | null>;
  setTelemetrySettings(settings: Partial<TelemetrySettings>): Promise<TelemetrySettings>;
  getWebSearchSettings(): Promise<WebSearchSettings | null>;
  setWebSearchSettings(settings: Partial<WebSearchSettings>): Promise<WebSearchSettings>;
  getInsightsSettings(): Promise<InsightsSettings | null>;
  ensureInsightsSettings(): Promise<InsightsSettings>;
  setInsightsSettings(settings: InsightsSettingsPatch): Promise<InsightsSettings>;
  enqueueInsight(kind: InsightKind, payload: Record<string, unknown>): Promise<InsightOutboxRow>;
  listInsightOutbox(opts?: { uploaded?: boolean; kind?: InsightKind; limit?: number }): Promise<InsightOutboxRow[]>;
  markInsightUploaded(id: string): Promise<void>;
  markInsightUploadError(id: string, error: string): Promise<void>;
  insightStats(): Promise<{ events: number; turns: number; profile: number; pending: number }>;
  exportInsightsBundle(): Promise<{
    schema: string;
    exportedAt: string;
    installId: string;
    settings: { shareBehavior: boolean; shareConversation: boolean; shareProfile: boolean };
    profile: import('./insights-settings.js').InsightsProfile;
    stats: { events: number; turns: number; profile: number; pending: number };
    records: InsightOutboxRow[];
  }>;
}
