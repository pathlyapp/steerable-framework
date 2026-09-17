/**
 * Typed wrappers around `window.electron.localBackend.request` for the
 * endpoints that apps/web actually consumes today. Shapes follow what
 * `src/local-backend/router.ts` returns — see the corresponding route
 * handlers there if you need to add fields.
 *
 * Conventions:
 *   - Every helper throws if the bridge is missing (caller should `isElectron()`
 *     check first if a graceful degradation is needed).
 *   - We deliberately keep the local return shapes (not Pydantic / protocol
 *     types) — they're the contract between local-backend and renderer, and
 *     may drift from any public cloud API. Aligning the two is out of scope.
 */

import { getElectronBridge } from './electron-bridge';

export interface LocalChat {
  id: string;
  projectId: string | null;
  userId: string;
  title: string;
  agentId: string | null;
  createdAt: string;
  updatedAt: string;
  isPinned: boolean;
  systemPrompt: string | null;
  pinnedRefs: unknown;
}

/** 智能体的工具准入策略；`all` = 不限制。 */
export interface ChatAgentToolPolicy {
  mode: 'all' | 'allowlist' | 'denylist';
  tools: string[];
}

export interface LocalChatAgent {
  id: string;
  slug: string | null;
  name: string;
  icon: string | null;
  color: string | null;
  description: string | null;
  rolePrompt: string | null;
  /**
   * 勾选的技能（技能 `dirName`）：正文无条件常驻该智能体的系统提示词，
   * 绕过技能自身的触发条件。
   */
  skillIds?: string[];
  /** 模型可见工具的准入策略；每轮工具列表与分发层都执行。 */
  toolPolicy?: ChatAgentToolPolicy;
  /** false = 只允许 {@link skillIds} 里的技能（硬白名单）。缺省 true。 */
  allowExternalSkills?: boolean;
  /** true = 无视触发条件加载全部技能（内置「智能助手」如此）。 */
  loadAllSkills?: boolean;
  isBuiltin: boolean;
  isArchived?: boolean;
  /**
   * Display order for the AgentSidebar's "专家团队" list. Local-backend
   * persists this on every CRUD write (see router.ts:341 / 373). Lower
   * numbers render first; same-value items fall back to alphabetic by name.
   * Optional because the schema still tolerates legacy rows without it.
   */
  sortOrder?: number | null;
}

export interface LocalChatMessage {
  id: string;
  chatId: string;
  role: 'system' | 'user' | 'assistant';
  content: string;
  createdAt: string;
  messageMetadata?: string | null;
}

interface ChatListResponse {
  chats: LocalChat[];
  pagination: {
    page: number;
    limit: number;
    total: number;
    totalPages: number;
    hasMore: boolean;
  };
}

interface ChatAgentListResponse {
  agents: LocalChatAgent[];
  total: number;
}

interface CreateChatResponse {
  success: boolean;
  chatId: string;
  projectId: string | null;
  isTemporary: boolean;
}

function bridge() {
  const b = getElectronBridge();
  if (!b) {
    throw new Error(
      'Electron bridge unavailable — local API can only be used inside the desktop shell.',
    );
  }
  return b;
}

export async function listChats(params: { page?: number; limit?: number } = {}) {
  const page = params.page ?? 1;
  const limit = params.limit ?? 50;
  return bridge().localBackend.request<ChatListResponse>({
    method: 'GET',
    path: `/api/v2/chats?page=${page}&limit=${limit}`,
  });
}

export async function createChat(input: { agentId?: string; projectId?: string } = {}) {
  return bridge().localBackend.request<CreateChatResponse>({
    method: 'POST',
    path: '/api/v2/chats/new',
    body: input,
  });
}

export async function deleteChat(chatId: string) {
  return bridge().localBackend.request<{
    success: boolean;
    message: string;
    chatId: string;
  }>({
    method: 'DELETE',
    path: `/api/v2/chats/${encodeURIComponent(chatId)}`,
  });
}

/** 没有消息的会话才删；已有内容或会话不存在都返回 `deleted: false`。 */
export async function deleteChatIfEmpty(chatId: string) {
  return bridge().localBackend.request<{
    success: boolean;
    deleted: boolean;
    chatId: string;
  }>({
    method: 'DELETE',
    path: `/api/v2/chats/${encodeURIComponent(chatId)}?onlyIfEmpty=1`,
  });
}

/** 清掉空会话；`exceptChatId` 是当前打开的对话，避免和首条发送抢跑。 */
export async function pruneEmptyChats(exceptChatId?: string | null) {
  return bridge().localBackend.request<{ deletedChatIds: string[] }>({
    method: 'POST',
    path: '/api/v2/chats/prune-empty',
    body: { exceptChatId: exceptChatId ?? null },
  });
}

/** W1.2.1: one node in a chat's branch family (framework history record). */
export interface ChatBranchPoint {
  recordId: string;
  sourceRecordId: string | null;
  sourceUntilSeq: number | null;
  label: string;
  depth?: number;
}

export interface ChatBranchesResponse {
  activeRecordId: string;
  lineage: ChatBranchPoint[];
  children: ChatBranchPoint[];
}

/** W1.2.1: the chat's regenerate-fork branch family (empty when sidecar off). */
export async function getChatBranches(chatId: string) {
  return bridge().localBackend.request<ChatBranchesResponse>({
    method: 'GET',
    path: `/api/v2/chats/${encodeURIComponent(chatId)}/branches`,
  });
}

/**
 * W1.2.1: switch the chat's active record to another branch. The backend
 * re-projects the desktop message store from the framework record; the
 * caller must re-hydrate the message list afterwards.
 */
export async function activateChatBranch(chatId: string, recordId: string) {
  return bridge().localBackend.request<{
    activeRecordId: string;
    messageCount: number;
  }>({
    method: 'POST',
    path: `/api/v2/chats/${encodeURIComponent(chatId)}/branches/activate`,
    body: { recordId },
  });
}

/** Session tree: one node in the chat's full branch family (recursive). */
export interface ChatBranchTreeNode {
  recordId: string;
  sourceRecordId: string | null;
  sourceUntilSeq: number | null;
  label: string;
  depth: number;
  children: ChatBranchTreeNode[];
}

export interface ChatBranchTreeResponse {
  activeRecordId: string;
  /** Null when the sidecar is off or the record is unknown to it. */
  tree: ChatBranchTreeNode | null;
  nodeCount: number;
  /** True when the sidecar's safety bounds (depth 32 / 500 nodes) cut the tree. */
  truncated: boolean;
}

/**
 * Session tree: the chat's full branch family from the root (cousins
 * included) — the data behind the SessionTreeModal. Unlike
 * `getChatBranches` (lineage + direct children), this sees every fork.
 */
export async function getChatBranchTree(chatId: string) {
  return bridge().localBackend.request<ChatBranchTreeResponse>({
    method: 'GET',
    path: `/api/v2/chats/${encodeURIComponent(chatId)}/branches/tree`,
  });
}

export async function listChatAgents(includeArchived = false) {
  return bridge().localBackend.request<ChatAgentListResponse>({
    method: 'GET',
    path: `/api/v2/chat-agents${includeArchived ? '?include_archived=true' : ''}`,
  });
}

export interface ChatAgentWriteInput {
  name: string;
  color?: string | null;
  description?: string | null;
  rolePrompt?: string | null;
  sortOrder?: number;
  skillIds?: string[];
  toolPolicy?: ChatAgentToolPolicy;
  allowExternalSkills?: boolean;
  loadAllSkills?: boolean;
}

/** 技能目录条目（`GET /api/v2/chat-agents/skills`）。 */
export interface ChatAgentSkillOption {
  /** 技能 `dirName`，也是写入 `skillIds` 的值。 */
  id: string;
  name: string;
  displayName: string;
  description: string;
  /** eager = 正文常驻；catalog = 模型按需加载。 */
  layer: 'eager' | 'catalog';
  origin?: 'builtin' | 'user' | 'workspace';
}

/** 工具目录条目（`GET /api/v2/chat-agents/tools`）。 */
export interface ChatAgentToolOption {
  name: string;
  description: string;
  category: string;
}

export async function listChatAgentSkills() {
  return bridge().localBackend.request<{ skills: ChatAgentSkillOption[] }>({
    method: 'GET',
    path: '/api/v2/chat-agents/skills',
  });
}

export async function listChatAgentTools() {
  return bridge().localBackend.request<{ tools: ChatAgentToolOption[] }>({
    method: 'GET',
    path: '/api/v2/chat-agents/tools',
  });
}

export async function createChatAgent(input: ChatAgentWriteInput) {
  return bridge().localBackend.request<{ agent: LocalChatAgent }>({
    method: 'POST',
    path: '/api/v2/chat-agents',
    body: input,
  });
}

export async function updateChatAgent(
  agentId: string,
  input: Partial<ChatAgentWriteInput>,
) {
  return bridge().localBackend.request<{ agent: LocalChatAgent }>({
    method: 'PATCH',
    path: `/api/v2/chat-agents/${encodeURIComponent(agentId)}`,
    body: input,
  });
}

export async function archiveChatAgent(agentId: string) {
  return bridge().localBackend.request<{ id: string; status: string }>({
    method: 'DELETE',
    path: `/api/v2/chat-agents/${encodeURIComponent(agentId)}`,
  });
}

/**
 * 运行中回合的实时快照（`GET /api/v2/chats/:id/live-stream`）。用于切走再
 * 切回时恢复「正在运行」的状态：active=true 时携带已产出的部分文本、工具
 * 卡片、时间线与子代理事件；active=false 表示当前没有运行中的回合。
 */
export interface ChatLiveStream {
  active: boolean;
  status?: string;
  content?: string;
  executedActions?: unknown[];
  timeline?: unknown[];
  children?: unknown[];
}

export async function getChatLiveStream(chatId: string) {
  return bridge().localBackend.request<ChatLiveStream>({
    method: 'GET',
    path: `/api/v2/chats/${encodeURIComponent(chatId)}/live-stream`,
  });
}

/** 按 chatId 取消运行中的回合（切回后新挂载的视图手里没有原 streamId）。 */
export async function cancelChatTurn(chatId: string) {
  return bridge().localBackend.request<{ success: boolean; reason?: string }>({
    method: 'POST',
    path: `/api/v2/chats/${encodeURIComponent(chatId)}/cancel`,
    body: {},
  });
}

/**
 * 用系统默认应用打开本地绝对路径（回合产物列表的点击行为）。
 * 成功时后端返回 `{ success: true }`；打开失败返回 `{ success: false, error }`。
 */
export async function openLocalPath(targetPath: string) {
  return bridge().localBackend.request<{ success: boolean; error?: string }>({
    method: 'POST',
    path: '/api/v2/local/open-path',
    body: { path: targetPath },
  });
}

/** 后端确认存在的路径：`candidate` 是正文里的原字面量，`path` 是落地绝对路径。 */
export interface ResolvedLocalPath {
  candidate: string;
  path: string;
  isDirectory: boolean;
}

/**
 * 批量确认正文里提到的路径是否真实存在（用于决定行内代码要不要变成可点击）。
 * 相对路径由后端按对话绑定的项目根解析，无项目时按 home；不存在的候选不回。
 */
export async function resolveLocalPaths(candidates: string[], chatId?: string | null) {
  return bridge().localBackend.request<{ resolved: ResolvedLocalPath[] }>({
    method: 'POST',
    path: '/api/v2/local/resolve-paths',
    body: { candidates, ...(chatId ? { chatId } : {}) },
  });
}

/* ---------------- Projects（项目模式） ---------------- */

export interface LocalProject {
  id: string;
  name: string;
  folderPath: string;
  /** W6-5: 信任后该项目目录里的规则文件才会注入模型上下文。缺省 false。 */
  trusted?: boolean;
  createdAt: string;
  updatedAt: string;
}

/** W6-5: 会话的项目上下文状态（信任横幅用）。 */
export interface ChatProjectContext {
  project: { id: string; name: string; folderPath: string; trusted: boolean } | null;
  ruleFileCount?: number;
  rulesActive?: boolean;
}

export async function getChatProjectContext(chatId: string) {
  return bridge().localBackend.request<ChatProjectContext>({
    method: 'GET',
    path: `/api/v2/chats/${encodeURIComponent(chatId)}/project-context`,
  });
}

export async function setProjectTrusted(projectId: string, trusted: boolean) {
  return bridge().localBackend.request<{ success: boolean; project: LocalProject }>({
    method: 'PUT',
    path: `/api/v2/projects/${encodeURIComponent(projectId)}/trust`,
    body: { trusted },
  });
}

/* ---------------- Tasks（4.6 跨 turn 后台任务 + worktree） ---------------- */

/** 与后端 TaskRecord（src/storage/index.ts）同形。 */
export interface LocalTask {
  id: string;
  chatId: string;
  task: string;
  /** blocked=等依赖任务全部完成后自动点火（task_run dependsOn 编排）。 */
  status: 'blocked' | 'running' | 'completed' | 'failed';
  answer: string | null;
  error: string | null;
  worktreePath: string | null;
  worktreeBranch: string | null;
  /** pending=等用户决定合并/丢弃；null=非 worktree 任务。 */
  worktreeState: 'pending' | 'merged' | 'discarded' | null;
  /** 编排依赖：本任务等待的 taskId 列表（null=无依赖）。 */
  dependsOn?: string[] | null;
  createdAt: string;
  updatedAt: string;
}

export async function listChatTasks(chatId: string) {
  return bridge().localBackend.request<{ tasks: LocalTask[] }>({
    method: 'GET',
    path: `/api/v2/chats/${encodeURIComponent(chatId)}/tasks`,
  });
}

export interface TaskProcessSnapshot {
  task: LocalTask;
  timeline: unknown[];
  live: boolean;
  stale: boolean;
}

export async function getTaskProcess(taskId: string) {
  return bridge().localBackend.request<TaskProcessSnapshot>({
    method: 'GET',
    path: `/api/v2/tasks/${encodeURIComponent(taskId)}/process`,
  });
}

/** 把已完成 worktree 任务的分支合并回主仓当前分支（4.6c）。 */
export async function mergeTaskWorktree(taskId: string) {
  return bridge().localBackend.request<{ success: boolean; task: LocalTask }>({
    method: 'POST',
    path: `/api/v2/tasks/${encodeURIComponent(taskId)}/merge`,
  });
}

/** 丢弃已完成 worktree 任务的改动并移除 worktree（4.6c）。 */
export async function discardTaskWorktree(taskId: string) {
  return bridge().localBackend.request<{ success: boolean; task: LocalTask }>({
    method: 'POST',
    path: `/api/v2/tasks/${encodeURIComponent(taskId)}/discard`,
  });
}

export async function listProjects() {
  return bridge().localBackend.request<{ projects: LocalProject[] }>({
    method: 'GET',
    path: '/api/v2/projects',
  });
}

export async function createProject(input: { name: string; folderPath: string }) {
  return bridge().localBackend.request<{ success: boolean; project: LocalProject }>({
    method: 'POST',
    path: '/api/v2/projects',
    body: input,
  });
}

export async function updateProject(
  projectId: string,
  updates: { name?: string; folderPath?: string },
) {
  return bridge().localBackend.request<{ success: boolean; project: LocalProject }>({
    method: 'PUT',
    path: `/api/v2/projects/${encodeURIComponent(projectId)}`,
    body: updates,
  });
}

export async function deleteProject(projectId: string) {
  return bridge().localBackend.request<{ success: boolean; detachedChats: number }>({
    method: 'DELETE',
    path: `/api/v2/projects/${encodeURIComponent(projectId)}`,
  });
}

/**
 * 改变会话的项目归属：projectId 为项目 id 表示关联过去，null 表示移出项目。
 * 走通用的 chat settings PATCH（后端会校验项目存在性）。
 */
export async function updateChatProject(chatId: string, projectId: string | null) {
  return bridge().localBackend.request<{ id: string; projectId: string | null }>({
    method: 'PATCH',
    path: `/api/v2/chats/${encodeURIComponent(chatId)}/settings`,
    body: { projectId },
  });
}

/* ---------------- Local LLM settings ---------------- */

export type LlmProvider = 'ollama' | 'openai-compat' | 'anthropic' | 'google' | 'openai-responses';

/** 与 src/storage/llm-settings.ts 的同名接口镜像（主进程侧是真源）。 */
export interface OpenAICompatOverrides {
  supportsUsageInStreaming?: boolean;
  maxTokensField?: 'max_tokens' | 'max_completion_tokens';
  supportsReasoningEffort?: boolean;
  supportsTemperature?: boolean;
  reasoningDeltaFields?: string[];
  cachedTokensFields?: string[];
}

/** sidecar `compat.describe` 返回的旗标描述符——设置页按它渲染，键名不硬编码。 */
export interface CompatFlagDescriptor {
  key: string;
  field: string;
  /** "bool" | "string-list" | "enum:<opt>,<opt>" */
  kind: string;
  default: unknown;
  description: string;
}

/** 与 src/storage/llm-settings.ts 的同名接口镜像（主进程侧是真源）。 */
export interface ProviderPresetOverride {
  temperature?: number;
  topP?: number;
  maxTokens?: number;
  reasoningEffort?: string;
  extraBody?: Record<string, unknown>;
}

/** 厂商参数预制选择：缺省/enabled=true = 自动匹配；false = 关闭；override = 钉死一条。 */
export interface ProviderPresetsChoice {
  enabled?: boolean;
  override?: ProviderPresetOverride;
}

/** sidecar `presets.describe` 返回的注册表行——设置页的预制选择器按它渲染。 */
export interface ProviderPresetDescriptor {
  host: string | null;
  modelPrefix: string | null;
  temperature: number | null;
  topP: number | null;
  maxTokens: number | null;
  reasoningEffort: string | null;
  extraBody: Record<string, unknown> | null;
}

export interface LlmSettings {
  provider: LlmProvider;
  /** 设置页选中的服务商目录 id；缺省由 baseUrl 回推。 */
  vendorId?: string;
  model: string;
  baseUrl?: string;
  apiKey?: string;
  /** 缺省 = 自动：命中厂商预制时用预制值，否则不下发（厂商服务端默认）。 */
  temperature?: number;
  systemPrompt?: string;
  maxTotalTokens?: number;
  /** 本地命令默认超时（秒）。留空 = 内置默认（headless 30s / 可见终端 60s）。 */
  execTimeoutSeconds?: number;
  /** OpenAI 兼容旗标覆盖；缺省 = 框架按 baseUrl 自动探测。 */
  compat?: OpenAICompatOverrides;
  /** 厂商参数预制选择；缺省 = 框架按 baseUrl+model 自动匹配。 */
  presets?: ProviderPresetsChoice;
}

export async function getCompatFlags() {
  return bridge().localBackend.request<{ flags: CompatFlagDescriptor[]; error?: string }>({
    method: 'GET',
    path: '/api/v2/compat/flags',
  });
}

export async function getProviderPresets() {
  return bridge().localBackend.request<{ presets: ProviderPresetDescriptor[]; error?: string }>({
    method: 'GET',
    path: '/api/v2/llm/presets',
  });
}

/** 当前 baseUrl+model 自动匹配命中的预制（设置页「生效预览」）；未命中 = null。 */
export async function resolveProviderPreset(baseUrl?: string, model?: string) {
  const query = new URLSearchParams();
  if (baseUrl) query.set('baseUrl', baseUrl);
  if (model) query.set('model', model);
  return bridge().localBackend.request<{ preset: ProviderPresetOverride | null; error?: string }>({
    method: 'GET',
    path: `/api/v2/llm/presets/resolve?${query.toString()}`,
  });
}

/**
 * 网关活模型目录的一行（sidecar `models.list`）：网关真正接受的 id +
 * models.dev 能力表 join 的结果。`capabilities: 'unknown'` 表示目录没
 * join 上——reasoningLevels 不可用而非为空；选择仍可下发（发现而非
 * 路由白名单）。
 */
export interface GatewayModelEntry {
  id: string;
  name: string | null;
  window: number | null;
  modalities: string[];
  reasoningLevels: string[];
  pricing: {
    promptPerMtok: number | null;
    completionPerMtok: number | null;
  } | null;
  joinedFrom: string | null;
  capabilities: 'known' | 'unknown';
}

/** `GET /api/v2/llm/models` 的响应。catalogStatus：live 刚拉取 /
 * stale 缓存兜底 / offline 目录不可用（models 为空，error 带原因）。 */
export interface GatewayModelCatalog {
  models: GatewayModelEntry[];
  catalogStatus: 'live' | 'stale' | 'offline';
  error?: string;
  fetchedAt?: number;
  current?: { model: string | null; reasoningEffort: string | null };
}

/** sidecar `catalog.describe`：服务商默认 URL、wire 种类、可对话模型 id。 */
export interface CatalogProviderDescriptor {
  id: string;
  apiBaseUrl: string | null;
  envVars: string[];
  wireKind: string;
  models: string[];
}

export async function getCatalogProviders() {
  return bridge().localBackend.request<{ providers: CatalogProviderDescriptor[]; error?: string }>({
    method: 'GET',
    path: '/api/v2/llm/catalog',
  });
}

/** 聊天输入框 / 设置页的模型目录。设置页可带尚未保存的 draft 凭证。 */
export async function getLlmModels(draft?: {
  baseUrl?: string;
  apiKey?: string;
  provider?: LlmProvider;
  refresh?: boolean;
}) {
  const query = new URLSearchParams();
  if (draft?.baseUrl) query.set('baseUrl', draft.baseUrl);
  if (draft?.apiKey) query.set('apiKey', draft.apiKey);
  if (draft?.provider) query.set('provider', draft.provider);
  if (draft?.refresh) query.set('refresh', '1');
  const qs = query.toString();
  const suffix = qs ? `?${qs}` : '';
  return bridge().localBackend.request<GatewayModelCatalog>({
    method: 'GET',
    path: `/api/v2/llm/models${suffix}`,
  });
}

/**
 * W4-3: layer-1（sidecar 进程沙箱）态势——主进程在 spawn 时记录，与
 * src/sidecar/types.ts 的 SidecarSandboxPosture 同形。enforcement 沿用
 * layer-3 词汇（partial|none）；reason 区分手动关闭与非自愿降级，
 * 设置页「安全」区只对后者告警。
 */
export interface SidecarSandboxPosture {
  backend: 'seatbelt' | 'bwrap' | 'landlock' | 'windows-restricted-token' | 'none';
  enforcement: 'partial' | 'none';
  reason:
    | 'active'
    | 'disabled_by_option'
    | 'disabled_by_env'
    | 'platform_unsupported'
    | 'seatbelt_missing'
    | 'profile_failed'
    | 'wrap_failed'
    | 'helper_missing';
}

/**
 * 出网管控态势（W-egress-posture）：per-host 代理生效 / 端口级退回
 * （reason 含原因，如检测到系统代理）/ 用户显式关闭。与
 * src/sidecar/egress-proxy.ts 的 EgressPosture 同形。
 */
export interface EgressPosture {
  mode: 'per-host-proxy' | 'port-only-fallback' | 'disabled';
  reason: string | null;
}

export async function getSidecarSandboxPosture() {
  return bridge().localBackend.request<{
    posture: SidecarSandboxPosture | null;
    egress?: EgressPosture | null;
    error?: string;
  }>({
    method: 'GET',
    path: '/api/v2/sidecar/sandbox-posture',
  });
}

export async function getLlmSettings() {
  return bridge().localBackend.request<LlmSettings>({
    method: 'GET',
    path: '/api/v2/local-settings/llm',
  });
}

export async function setLlmSettings(input: LlmSettings) {
  return bridge().localBackend.request<LlmSettings>({
    method: 'POST',
    path: '/api/v2/local-settings/llm',
    body: input,
  });
}

/* ---------------- LLM link diagnosis ---------------- */

export interface DiagnoseStep {
  name: string;
  ok: boolean;
  durationMs: number;
  detail: string;
}

export interface DiagnoseResult {
  ok: boolean;
  steps: DiagnoseStep[];
  ambientProxies: string[];
  hint: string | null;
}

export async function diagnoseLlmConnection(input: {
  baseUrl?: string;
  apiKey?: string;
  model?: string;
}) {
  return bridge().localBackend.request<DiagnoseResult>({
    method: 'POST',
    path: '/api/v2/llm/diagnose',
    body: input,
  });
}
