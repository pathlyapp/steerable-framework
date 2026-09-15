import { randomUUID } from 'crypto';
import path from 'path';
import Database from 'better-sqlite3';
import { getUserDataDir } from '../runtime.js';
import { getProductConfig } from '../product-config.js';
import {
  ALL_ROUND_ASSISTANT_AGENT_ID,
  LOCAL_ASSISTANT_AGENT_ID,
  getBrand,
} from '../brand.js';
import {
  DEFAULT_LLM_SETTINGS,
  llmSettingsCarryExpiredBakedKey,
  llmSettingsMatchOllamaDefaults,
  mergeLlmSettings,
  type LlmSettings,
} from './llm-settings.js';
import {
  MESSAGE_CUT_FROM_ID,
  MESSAGE_ORDER_DESC,
} from './message-order.js';
import { LIST_EMPTY_CHAT_IDS_SQL } from './empty-chats.js';
import {
  mergeTelemetrySettings,
  type TelemetrySettings,
} from './telemetry-settings.js';
import {
  mergeWebSearchSettings,
  type WebSearchSettings,
} from './web-search-settings.js';
import {
  mergeInsightsProfile,
  mergeInsightsSettings,
  type InsightKind,
  type InsightsProfile,
  type InsightsSettings,
  type InsightsSettingsPatch,
} from './insights-settings.js';
import { acquireWriteLease, lockPathForDb, type HeldWriteLease } from './write-lease.js';
import { createLocalStore } from './local-store-singleton.js';
import { getPackMigrations } from './pack-migrations.js';
import {
  getPackAgentSeeds,
  isUntouchedSeed,
  matchesPreviousIdentity,
  type AgentSeedRow,
} from './pack-seeds.js';
import type { AgentSeed } from '../scenario/pack.js';
import {
  buildUsageSummary,
  type UsageSummary,
  type UsageSummaryRow,
} from './usage-summary.js';

export type { UsageModelBucket, UsageSummary } from './usage-summary.js';

export interface ChatSessionRecord {
  id: string;
  title: string;
  userId: string;
  agentId: string | null;
  /** 项目模式：绑定的项目 id（ProjectRegistry）。null = 无项目对话（不沙箱）。 */
  projectId: string | null;
  createdAt: string;
  updatedAt: string;
  isPinned: boolean;
  systemPrompt: string | null;
  pinnedRefs: unknown[] | null;
}

export interface ChatMessageRecord {
  id: string;
  chatId: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  messageMetadata: string | null;
  createdAt: string;
}

export interface ChatAgentRecord {
  id: string;
  slug: string | null;
  name: string;
  icon: string | null;
  color: string | null;
  description: string | null;
  rolePrompt: string | null;
  forbiddenPrompt: string | null;
  /**
   * 勾选的技能（skill `dirName` 或 `name`）：正文无条件常驻系统提示词
   * （绕过触发条件与 eager/catalog 分层）。空 = 不钉任何技能。
   */
  skillIds: string[];
  /**
   * 模型可见工具的准入策略。`allowlist`/`denylist` 是真实限制：每轮工具
   * 列表、`tool_search` 发现结果、以及反向通道的分发复检三处都执行。
   */
  toolPolicy: { mode: 'all' | 'allowlist' | 'denylist'; tools: string[] };
  /**
   * true（默认）= 未勾选的技能照旧按触发条件/目录按需加载；
   * false = 只允许 {@link skillIds} 里的技能（硬白名单，注入、目录、
   * `/技能名` 显式触发三处都拦）。
   */
  allowExternalSkills: boolean;
  /**
   * true = 无视技能的触发条件全量加载（内置「智能助手」的行为）。
   * 与 `allowExternalSkills: false` 同时设置时白名单仍然优先。
   */
  loadAllSkills: boolean;
  isBuiltin: boolean;
  isArchived: boolean;
  sortOrder: number;
  createdAt: string;
  updatedAt: string;
}

export interface HarnessTraceRecord {
  id: string;
  chatId: string;
  messageId: string | null;
  startedAtMs: number;
  durationMs: number | null;
  status: string;
  payload: string;
  createdAt: string;
}

/**
 * 跨 turn 后台任务（4.6a）。task_run 工具落一行后立刻返回，任务由宿主的
 * 独立 sidecar 流跑完（不绑父 turn 生命周期），终态回写本表。
 *
 * worktree* 三列是 Task×Worktree 组合（4.6c）：worktreeState 仅对
 * worktree 任务有意义——任务终态后 'pending'（等待用户合并/丢弃），
 * 操作后落 'merged' / 'discarded'。
 */
export interface TaskRecord {
  id: string;
  chatId: string;
  /** 完整的任务指令（模型给的 task 原文）。 */
  task: string;
  /** blocked = 依赖未就绪，等调度点火（依赖任务全部 completed 后自动转 running）。 */
  status: 'blocked' | 'running' | 'completed' | 'failed';
  answer: string | null;
  error: string | null;
  worktreePath: string | null;
  worktreeBranch: string | null;
  worktreeState: 'pending' | 'merged' | 'discarded' | null;
  /** sidecar durable record id（task:<id>），trace.fetch / 排障用。 */
  recordId: string | null;
  traceId: string | null;
  /** 编排依赖：本任务等这些 taskId 全部 completed 才点火（null = 无依赖，立即点火）。 */
  dependsOn: string[] | null;
  /** 推理时间线 JSON（与主对话 TurnBlock 同形）；流过程中回写，重启后可回看。 */
  processJson: string | null;
  createdAt: string;
  updatedAt: string;
}

/** tasks.depends_on 列的 JSON 解析——坏行（非数组/非字符串项）归一为 null 而不是炸掉读路径。 */
function parseDependsOn(raw: unknown): string[] | null {
  if (raw == null) return null;
  try {
    const parsed = JSON.parse(String(raw));
    if (!Array.isArray(parsed)) return null;
    const ids = parsed.filter((x): x is string => typeof x === 'string' && x.length > 0);
    return ids.length ? ids : null;
  } catch {
    return null;
  }
}

export type { LlmSettings, LlmProvider } from './llm-settings.js';
export { DEFAULT_SYSTEM_PROMPT } from './llm-settings.js';
export type { TelemetrySettings, TelemetryPrivacyMode } from './telemetry-settings.js';
export {
  DEFAULT_TELEMETRY_SETTINGS,
  telemetryEnabled,
  normalizeTelemetryEndpoint,
} from './telemetry-settings.js';
export type { WebSearchSettings, WebSearchProviderId } from './web-search-settings.js';
export {
  DEFAULT_WEB_SEARCH_SETTINGS,
  mergeWebSearchSettings,
  hostedSearchAvailable,
  sidecarWebSearchEnv,
  defaultSearchBaseUrl,
} from './web-search-settings.js';
export type { InsightsSettings, InsightsProfile, InsightKind, InsightsSettingsPatch } from './insights-settings.js';
export {
  mergeInsightsSettings,
  insightsNeedsPrompt,
  canAutoUpload,
  rowsEligibleForAutoUpload,
  resolveInsightsApiBase,
} from './insights-settings.js';

export interface InsightOutboxRow {
  id: string;
  kind: InsightKind;
  payload: Record<string, unknown>;
  createdAt: string;
  uploadedAt: string | null;
  uploadError: string | null;
}

const DEFAULT_LOCAL_USER_ID = 'local';

// 线协议常量：insights 导出包 schema 名由遥测接收端（产品注入的
// insightsApiBase 对端）定义，改名会破坏对端解析。
const INSIGHTS_EXPORT_SCHEMA = 'deeppath-agent-insights/v1'; // shell-neutral:allow（线协议常量，见上行）

export class LocalStore {
  private readonly db: Database.Database;
  private readonly writeLease: HeldWriteLease;

  constructor() {
    // 主库文件名是产品注入配置（3.1，product.json dbFileName）——产品
    // 借此保住存量数据文件；中性 shell 缺省 agent-shell.db。
    const dbPath = path.join(getUserDataDir(), getProductConfig().dbFileName ?? 'agent-shell.db');
    this.writeLease = acquireWriteLease(lockPathForDb(dbPath));
    this.db = new Database(dbPath);
    this.db.pragma('journal_mode = WAL');
    // The schema declares `ON DELETE CASCADE` foreign keys (chat_messages,
    // harness_traces -> chat_sessions), but SQLite ignores FK constraints
    // unless explicitly enabled per-connection. Without this, deleteChat()
    // only removed the session row and left orphaned messages/traces behind.
    this.db.pragma('foreign_keys = ON');
    this.migrate();
    this.cleanupOrphanedRows();
    this.seedDefaults();
  }

  /**
   * 场景包存储扩展点：把同一个 SQLite 连接借给包内存储类（各包的
   * <Pack>Store）。包只读写自己 migrations 声明的包前缀表；shell 表结构对包不透明。
   */
  getPackDb(): Database.Database {
    return this.db;
  }

  /**
   * One-time cleanup for rows that became orphaned while `foreign_keys` was
   * off in earlier versions of the app (deleteChat() previously left
   * chat_messages/harness_traces behind for deleted chat_sessions).
   */
  private cleanupOrphanedRows(): void {
    this.db.exec(`
      DELETE FROM chat_messages
      WHERE chat_id NOT IN (SELECT id FROM chat_sessions);
      DELETE FROM harness_traces
      WHERE chat_id NOT IN (SELECT id FROM chat_sessions);
      DELETE FROM tasks
      WHERE chat_id NOT IN (SELECT id FROM chat_sessions);
    `);
  }

  private migrate(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS chat_sessions (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        user_id TEXT NOT NULL,
        agent_id TEXT,
        is_pinned INTEGER NOT NULL DEFAULT 0,
        system_prompt TEXT,
        pinned_refs TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS chat_messages (
        id TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        message_metadata TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(chat_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
      );

      CREATE TABLE IF NOT EXISTS chat_agents (
        id TEXT PRIMARY KEY,
        slug TEXT,
        name TEXT NOT NULL,
        icon TEXT,
        color TEXT,
        description TEXT,
        role_prompt TEXT,
        forbidden_prompt TEXT,
        skill_ids TEXT NOT NULL,
        tool_policy TEXT NOT NULL,
        allow_external_skills INTEGER NOT NULL DEFAULT 0,
        is_builtin INTEGER NOT NULL DEFAULT 0,
        is_archived INTEGER NOT NULL DEFAULT 0,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS settings_kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS harness_traces (
        id TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL,
        message_id TEXT,
        started_at_ms INTEGER NOT NULL,
        duration_ms INTEGER,
        status TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(chat_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
      );

      CREATE INDEX IF NOT EXISTS idx_harness_traces_chat ON harness_traces(chat_id, started_at_ms DESC);

      CREATE TABLE IF NOT EXISTS usage_events (
        id TEXT PRIMARY KEY,
        chat_id TEXT,
        kind TEXT NOT NULL,
        provider TEXT,
        model TEXT,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens INTEGER NOT NULL DEFAULT 0,
        cached_prompt_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL,
        created_at TEXT NOT NULL
      );

      CREATE INDEX IF NOT EXISTS idx_usage_events_created ON usage_events(created_at DESC);

      CREATE TABLE IF NOT EXISTS insights_outbox (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL,
        uploaded_at TEXT,
        upload_error TEXT
      );

      CREATE INDEX IF NOT EXISTS idx_insights_outbox_pending ON insights_outbox(kind, uploaded_at, created_at);

      CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL,
        task TEXT NOT NULL,
        status TEXT NOT NULL,
        answer TEXT,
        error TEXT,
        worktree_path TEXT,
        worktree_branch TEXT,
        worktree_state TEXT,
        record_id TEXT,
        trace_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(chat_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
      );

      CREATE INDEX IF NOT EXISTS idx_tasks_chat ON tasks(chat_id, datetime(updated_at) DESC);
    `);

    // 跨轮压缩已下沉框架 CoreLoop（token 压力触发 CompactionHooks，压缩边界
    // 持久化到 durable record）。桌面不再维护滚动摘要；历史库里的
    // context_summary / context_summary_upto / context_compaction_delegated
    // 列成为无人读写的孤儿列（SQLite 不易 drop，保留无害）。

    // 项目模式（增量迁移）：chat 可绑定 ProjectRegistry 里的项目 id。
    // 项目记录本身存在 electron-store（agent-projects.json），这里只存外键；
    // 删项目时由路由层把本列置 NULL（会话降级为无项目对话）。
    this.ensureColumn('chat_sessions', 'project_id', 'TEXT');

    // 任务编排（增量迁移）：depends_on 存 JSON string[]——本任务等待的
    // 依赖 taskId 列表；NULL = 无依赖。依赖必须指向建任务时已存在的
    // 任务，所以依赖图天然无环。
    this.ensureColumn('tasks', 'depends_on', 'TEXT');
    // 后台任务推理过程：流事件回写，不bump updated_at（见 saveTaskProcess）。
    this.ensureColumn('tasks', 'process_json', 'TEXT');

    // 智能体技能面（增量迁移）：load_all_skills = 无视触发条件全量加载。
    // 此前这个行为是 router 里 `chatAgentId === 'all-round-assistant'` 的
    // 硬编码，自建智能体拿不到；建列时一次性回填给内置「智能助手」，之后
    // 由用户在智能体管理页自由开关（不会每次启动被重置）。
    if (this.ensureColumn('chat_agents', 'load_all_skills', 'INTEGER NOT NULL DEFAULT 0')) {
      this.db
        .prepare(`UPDATE chat_agents SET load_all_skills = 1 WHERE id = ?`)
        .run(ALL_ROUND_ASSISTANT_AGENT_ID);
    }

    // 场景包迁移（0.3b）：构造时应用一次已注册的包迁移（兼容注册先于
    // 构造的路径）；注册晚于构造的容错路径见 applyRegisteredPackMigrations
    // 模块级函数（host/runtime 在包装配前调用）。
    this.applyPackMigrations();
  }

  private readonly appliedPackMigrations = new Set<string>();

  /**
   * 应用尚未应用的已注册包迁移（幂等，按包 id 去重）。
   */
  applyPackMigrations(): void {
    for (const { packId, migration } of getPackMigrations()) {
      if (this.appliedPackMigrations.has(packId)) continue;
      if (migration.ddl && migration.ddl.length > 0) {
        this.db.exec(migration.ddl.join(';\n'));
      }
      for (const col of migration.ensureColumns ?? []) {
        this.ensureColumn(col.table, col.column, col.type);
      }
      this.appliedPackMigrations.add(packId);
    }
  }

  /**
   * 应用一条包智能体种子（0.3d 的通用机制）：
   * 包激活 —— 不存在则种子；命中历史代文案则静默升级到当前代；被归档但
   * 未定制的行重新露出。包缺席 —— 未定制的内置行归档。用户改过的行不动。
   */
  private applyPackAgentSeed(
    seed: AgentSeed,
    active: boolean,
    insertAgent: Database.Statement,
    now: string,
  ): void {
    const row = this.db
      .prepare(
        `SELECT id, name, description, role_prompt, is_archived FROM chat_agents WHERE id = ?`,
      )
      .get(seed.id) as AgentSeedRow | undefined;

    if (!active) {
      if (row && row.is_archived === 0 && isUntouchedSeed(seed, row)) {
        this.db
          .prepare(`UPDATE chat_agents SET is_archived = 1, updated_at = ? WHERE id = ?`)
          .run(now, seed.id);
      }
      return;
    }

    if (!row) {
      insertAgent.run({
        id: seed.id,
        slug: seed.slug,
        name: seed.name,
        icon: seed.icon ?? null,
        color: seed.color ?? null,
        description: seed.description ?? null,
        rolePrompt: seed.rolePrompt,
        forbiddenPrompt: seed.forbiddenPrompt ?? null,
        skillIds: JSON.stringify(seed.skillIds ?? []),
        toolPolicy: JSON.stringify({ mode: 'all', tools: [] }),
        allowExternalSkills: 1,
        loadAllSkills: seed.loadAllSkills ? 1 : 0,
        isBuiltin: 1,
        isArchived: 0,
        sortOrder: seed.sortOrder ?? 0,
        createdAt: now,
        updatedAt: now,
      });
      return;
    }

    // 历史代文案 = 用户从未定制：静默升级到当前代（改过的保留用户定制）。
    if (matchesPreviousIdentity(seed, row)) {
      this.db
        .prepare(
          `UPDATE chat_agents
           SET name = @name,
               description = @description,
               role_prompt = @rolePrompt,
               updated_at = @updatedAt
           WHERE id = @id`,
        )
        .run({
          name: seed.name,
          description: seed.description ?? null,
          rolePrompt: seed.rolePrompt,
          updatedAt: now,
          id: seed.id,
        });
    }
    // 从未定制过、却被归档的行：包装回该产品后重新露出。
    if (row.is_archived === 1 && isUntouchedSeed(seed, row)) {
      this.db
        .prepare(`UPDATE chat_agents SET is_archived = 0, updated_at = ? WHERE id = ?`)
        .run(now, seed.id);
    }
  }

  /**
   * SQLite 没有 ADD COLUMN IF NOT EXISTS，先查 PRAGMA 再加列。
   *
   * @returns 本次是否真的加了列（调用方据此做一次性回填）。
   */
  private ensureColumn(table: string, column: string, type: string): boolean {
    const columns = this.db.prepare(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>;
    if (columns.some((c) => c.name === column)) return false;
    this.db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${type}`);
    return true;
  }

  private seedDefaults(): void {
    const now = new Date().toISOString();
    const hasBuiltin = this.db
      .prepare(`SELECT id FROM chat_agents WHERE id = ?`)
      .get(LOCAL_ASSISTANT_AGENT_ID) as { id: string } | undefined;
    // 激活包中 sortOrder 0 的主打种子占据列表首位时，shell 默认智能体
    // 让位到 1（0.3d 起由包种子驱动，不再按 flavor 硬编码）。
    const hasHeroSeed = getPackAgentSeeds().some(
      (r) => r.active && r.seeds.some((s) => s.sortOrder === 0),
    );

    const insertAgent = this.db.prepare(`
      INSERT INTO chat_agents (
        id, slug, name, icon, color, description, role_prompt, forbidden_prompt,
        skill_ids, tool_policy, allow_external_skills, load_all_skills, is_builtin, is_archived,
        sort_order, created_at, updated_at
      ) VALUES (
        @id, @slug, @name, @icon, @color, @description, @rolePrompt, @forbiddenPrompt,
        @skillIds, @toolPolicy, @allowExternalSkills, @loadAllSkills, @isBuiltin, @isArchived,
        @sortOrder, @createdAt, @updatedAt
      )
    `);

    if (!hasBuiltin) {
      insertAgent.run({
        id: LOCAL_ASSISTANT_AGENT_ID,
        slug: LOCAL_ASSISTANT_AGENT_ID,
        name: '电脑操作员',
        icon: 'Cpu',
        color: '#4f46e5',
        description: '默认本地助手，可调用 shell、文件与 MCP 工具。',
        rolePrompt: '你是本地离线助手，回答时清晰、可执行。',
        forbiddenPrompt: null,
        skillIds: JSON.stringify([]),
        toolPolicy: JSON.stringify({ mode: 'all', tools: [] }),
        allowExternalSkills: 1,
        loadAllSkills: 0,
        isBuiltin: 1,
        isArchived: 0,
        // 激活包有主打种子（sortOrder 0）时让位到 1。
        sortOrder: hasHeroSeed ? 1 : 0,
        createdAt: now,
        updatedAt: now,
      });
    } else {
      this.db.prepare(`UPDATE chat_agents SET name = ? WHERE id = ?`).run('电脑操作员', LOCAL_ASSISTANT_AGENT_ID);
    }

    const hasAllRound = this.db
      .prepare(`SELECT id FROM chat_agents WHERE id = ?`)
      .get(ALL_ROUND_ASSISTANT_AGENT_ID) as { id: string } | undefined;

    if (!hasAllRound) {
      insertAgent.run({
        id: ALL_ROUND_ASSISTANT_AGENT_ID,
        slug: ALL_ROUND_ASSISTANT_AGENT_ID,
        name: '智能助手',
        icon: '🔮',
        color: '#a855f7',
        description: '全能智能体，自动加载全部已安装本地技能和工具，无需限定条件。',
        rolePrompt: [
          '你是 **智能助手**，一个具备高权限且集成了全部本地技能（Skills）的行动体。',
          '你具备完整的本地命令执行（local_exec_shell）、文件读写能力，以及当前产品安装的全部场景工具。',
          '你的系统提示词中已经无条件加载了所有已安装的本地技能与工具。',
          '请根据用户的需求，直接、高效地调用最合适的本地工具或执行脚本来解决任务。'
        ].join('\n'),
        forbiddenPrompt: null,
        skillIds: JSON.stringify([]),
        toolPolicy: JSON.stringify({ mode: 'all', tools: [] }),
        allowExternalSkills: 1,
        // 「智能助手」的定位就是无条件加载全部技能（此前是 router 硬编码）。
        loadAllSkills: 1,
        isBuiltin: 1,
        isArchived: 0,
        sortOrder: 2,
        createdAt: now,
        updatedAt: now,
      });
    } else {
      const agent = this.db.prepare(`SELECT role_prompt FROM chat_agents WHERE id = ?`).get(ALL_ROUND_ASSISTANT_AGENT_ID) as { role_prompt: string } | undefined;
      if (agent) {
        const updatedPrompt = agent.role_prompt.replace(/全能专家助手/g, '智能助手');
        this.db.prepare(`UPDATE chat_agents SET name = ?, role_prompt = ? WHERE id = ?`).run('智能助手', updatedPrompt, ALL_ROUND_ASSISTANT_AGENT_ID);
      } else {
        this.db.prepare(`UPDATE chat_agents SET name = ? WHERE id = ?`).run('智能助手', ALL_ROUND_ASSISTANT_AGENT_ID);
      }
    }

    // ─── 场景包智能体种子（0.3d） ───
    // 激活包：不存在则种子；命中历史代文案（用户从未定制）则静默升级；
    // 被归档但未定制的行重新露出。未激活包：未定制的内置行归档（用户改
    // 过的保留）。种子数据在 packs/<id>/seeds.ts，由 packs/active.ts 注册。
    for (const { seeds, active } of getPackAgentSeeds()) {
      for (const seed of seeds) {
        this.applyPackAgentSeed(seed, active, insertAgent, now);
      }
    }

    // 主打种子（激活包 sortOrder 0）把 shell 默认智能体挤到第二位：仅当
    // 双方都还在出厂位置（local=0、种子=0/1）才交换，用户调过序不动。
    for (const { seeds, active } of getPackAgentSeeds()) {
      if (!active) continue;
      for (const seed of seeds) {
        if (seed.sortOrder !== 0) continue;
        const localSort = this.db
          .prepare(`SELECT sort_order FROM chat_agents WHERE id = ?`)
          .get(LOCAL_ASSISTANT_AGENT_ID) as { sort_order: number } | undefined;
        const seedSort = this.db
          .prepare(`SELECT sort_order FROM chat_agents WHERE id = ?`)
          .get(seed.id) as { sort_order: number } | undefined;
        if (
          localSort &&
          seedSort &&
          localSort.sort_order === 0 &&
          (seedSort.sort_order === 0 || seedSort.sort_order === 1)
        ) {
          this.db
            .prepare(`UPDATE chat_agents SET sort_order = 1, updated_at = ? WHERE id = ?`)
            .run(now, LOCAL_ASSISTANT_AGENT_ID);
          this.db
            .prepare(`UPDATE chat_agents SET sort_order = 0, updated_at = ? WHERE id = ?`)
            .run(now, seed.id);
        }
      }
    }

    const hasSettings = this.getLlmSettings();
    if (!hasSettings) {
      // 全新安装：直接写出厂默认 (DEFAULT_LLM_SETTINGS = OpenAI 兼容 / DeepSeek)。
      this.setLlmSettings(DEFAULT_LLM_SETTINGS);
    } else if (llmSettingsMatchOllamaDefaults(hasSettings)) {
      // 老安装但用户从未改过 LLM 设置（依然停留在出厂 Ollama 预设）：静默迁移
      // 到 DeepSeek 默认，避免 Windows 用户卡在"本机没装 ollama → 上来就不能聊"。
      this.setLlmSettings(DEFAULT_LLM_SETTINGS);
    } else if (llmSettingsCarryExpiredBakedKey(hasSettings)) {
      // 老安装保存过已作废的出厂内置 key（≤0.0.35）：静默清掉（保留其它自定义
      // 字段），设置页会引导用户填自己的 key。不清则用户永远拿着死 key 撞 401。
      this.setLlmSettings({ ...hasSettings, apiKey: undefined });
    }
    // 其它情况（用户改过 provider/model/baseUrl/apiKey 任一项）：尊重自定义，不动。
  }

  /**
   * `Math.max`/`Math.min` propagate `NaN` instead of clamping it, so a bad
   * caller-supplied value (e.g. `?page=abc` parsed via `Number()`) used to
   * produce a NaN OFFSET/LIMIT and an ill-defined SQL query result. All
   * paginated list methods below route through this to clamp to a sane
   * integer range, falling back to `fallback` for non-finite input.
   */
  private static clampInt(value: number, fallback: number, min: number, max: number): number {
    if (!Number.isFinite(value)) return fallback;
    return Math.max(min, Math.min(Math.trunc(value), max));
  }

  listChats(page = 1, limit = 50): { chats: ChatSessionRecord[]; total: number } {
    const safePage = LocalStore.clampInt(page, 1, 1, Number.MAX_SAFE_INTEGER);
    const safeLimit = LocalStore.clampInt(limit, 50, 1, 200);
    const offset = (safePage - 1) * safeLimit;
    const totalRow = this.db.prepare(`SELECT COUNT(*) as count FROM chat_sessions`).get() as { count: number };
    const rows = this.db.prepare(`
      SELECT *
      FROM chat_sessions
      ORDER BY is_pinned DESC, datetime(updated_at) DESC
      LIMIT ? OFFSET ?
    `).all(safeLimit, offset) as Array<Record<string, unknown>>;
    return {
      chats: rows.map(row => this.mapChatSession(row)),
      total: totalRow.count,
    };
  }

  createChat(
    title = '新对话',
    agentId: string | null = getBrand().defaultAgentId,
    projectId: string | null = null,
  ): ChatSessionRecord {
    return this.createChatWithId(randomUUID(), title, agentId, projectId);
  }

  /**
   * 用调用方给定的 id 建会话（**幂等**：已存在则原样返回，不覆盖）。
   *
   * 会话 URL（`/api/v2/chats/:id/*`、前端 `#/agent/:chatId`）在产品和外部
   * 平台眼里就是「一次会话任务」的身份。本地会话可能因为用户清理、空会话
   * prune、或换机重装而消失，但平台仍会拿原来的 URL 再次运行——首次发送时
   * 按 URL 里的 id 现场补建，才不会把一次本来能跑的任务打断在
   * `chat not found`。
   */
  createChatWithId(
    chatId: string,
    title = '新对话',
    agentId: string | null = 'local-assistant',
    projectId: string | null = null,
  ): ChatSessionRecord {
    const existing = this.getChat(chatId);
    if (existing) return existing;
    const now = new Date().toISOString();
    // INSERT OR IGNORE：两个请求同时补建同一个 id（并发首发送）时，后到的
    // 那条不会因主键冲突被打成 500——两边拿到的都是同一条会话。
    this.db.prepare(`
      INSERT OR IGNORE INTO chat_sessions (id, title, user_id, agent_id, project_id, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?)
    `).run(chatId, title, DEFAULT_LOCAL_USER_ID, agentId, projectId, now, now);
    const row = this.db.prepare(`SELECT * FROM chat_sessions WHERE id = ?`).get(chatId) as Record<string, unknown>;
    return this.mapChatSession(row);
  }

  /** 删除项目时调用：把该项目下所有会话降级为无项目对话（不删会话）。 */
  clearProjectAssignment(projectId: string): number {
    const info = this.db
      .prepare(`UPDATE chat_sessions SET project_id = NULL WHERE project_id = ?`)
      .run(projectId);
    return info.changes;
  }

  getChat(chatId: string): ChatSessionRecord | null {
    const row = this.db.prepare(`SELECT * FROM chat_sessions WHERE id = ?`).get(chatId) as Record<string, unknown> | undefined;
    if (!row) return null;
    return this.mapChatSession(row);
  }

  updateChat(
    chatId: string,
    updates: Partial<Pick<ChatSessionRecord, 'title' | 'systemPrompt' | 'pinnedRefs' | 'isPinned' | 'projectId'>>
  ): ChatSessionRecord | null {
    const existing = this.getChat(chatId);
    if (!existing) return null;
    // 过滤 undefined：调用方对"未提供的字段"传 undefined，直接展开会把
    // 现有值抹成 undefined（JSON body 里没有 undefined，缺字段≠置空）。
    // projectId 显式传 null 才是"移出项目"，会保留下来。
    const clean = Object.fromEntries(
      Object.entries(updates).filter(([, value]) => value !== undefined),
    );
    const next: ChatSessionRecord = {
      ...existing,
      ...clean,
      updatedAt: new Date().toISOString(),
    };
    this.db.prepare(`
      UPDATE chat_sessions
      SET title = ?, system_prompt = ?, pinned_refs = ?, is_pinned = ?, project_id = ?, updated_at = ?
      WHERE id = ?
    `).run(
      next.title,
      next.systemPrompt,
      JSON.stringify(next.pinnedRefs ?? null),
      next.isPinned ? 1 : 0,
      next.projectId ?? null,
      next.updatedAt,
      chatId
    );
    return this.getChat(chatId);
  }

  deleteChat(chatId: string): boolean {
    const info = this.db.prepare(`DELETE FROM chat_sessions WHERE id = ?`).run(chatId);
    this.db.prepare(`DELETE FROM settings_kv WHERE key = ?`).run(`chat_record:${chatId}`);
    this.db.prepare(`DELETE FROM settings_kv WHERE key = ?`).run(`turn_active:${chatId}`);
    return info.changes > 0;
  }

  chatHasMessages(chatId: string): boolean {
    const row = this.db
      .prepare(`SELECT 1 FROM chat_messages WHERE chat_id = ? LIMIT 1`)
      .get(chatId);
    return Boolean(row);
  }

  /**
   * Drop a chat that never received a message. Used when the user leaves a
   * "新对话" composer without sending — empty sessions must not stay in the
   * sidebar. Missing or already-populated chats are a no-op (`false`).
   */
  deleteChatIfEmpty(chatId: string): boolean {
    if (!this.getChat(chatId) || this.chatHasMessages(chatId)) return false;
    return this.deleteChat(chatId);
  }

  /**
   * Delete every session with zero messages, optionally keeping `exceptChatId`
   * (the open composer). Returns the removed ids.
   */
  deleteEmptyChats(exceptChatId?: string | null): string[] {
    const except = exceptChatId ?? null;
    const rows = this.db.prepare(LIST_EMPTY_CHAT_IDS_SQL).all(except, except) as Array<{
      id: string;
    }>;
    const ids = rows.map((row) => row.id);
    for (const id of ids) this.deleteChat(id);
    return ids;
  }

  /**
   * W5-2: the chat's ACTIVE durable-record id on the sidecar. Regenerate
   * forks the record (non-destructive — the old tail stays discoverable as
   * a branch), so a chat's live record moves from `chatId` to its branch;
   * null means the chatId itself.
   */
  getChatRecordId(chatId: string): string | null {
    const row = this.db
      .prepare(`SELECT value FROM settings_kv WHERE key = ?`)
      .get(`chat_record:${chatId}`) as { value: string } | undefined;
    return row?.value ?? null;
  }

  setChatRecordId(chatId: string, recordId: string): void {
    this.db.prepare(`
      INSERT INTO settings_kv (key, value) VALUES (?, ?)
      ON CONFLICT(key) DO UPDATE SET value = excluded.value
    `).run(`chat_record:${chatId}`, recordId);
  }

  /**
   * W7-1: durable "a turn is in flight" marker, written BEFORE the stream
   * starts and cleared only after the terminal assistant message persists
   * (or the turn fails before any reply could exist). A crash or kill
   * mid-turn never reaches the clear, so a surviving marker is the crash
   * signature — distinct from `completionStatus: 'cancelled' | 'failed'`,
   * which only a live process can write. Cleared AFTER the assistant
   * write, never before: the reverse order would turn a crash in that gap
   * into a lost reply with no recourse, while this order's worst case is a
   * stale marker the detector dismisses once it sees the trailing
   * assistant message.
   */
  setTurnActive(chatId: string): void {
    this.db.prepare(`
      INSERT INTO settings_kv (key, value) VALUES (?, ?)
      ON CONFLICT(key) DO UPDATE SET value = excluded.value
    `).run(`turn_active:${chatId}`, JSON.stringify({ startedAt: new Date().toISOString() }));
  }

  clearTurnActive(chatId: string): void {
    this.db.prepare(`DELETE FROM settings_kv WHERE key = ?`).run(`turn_active:${chatId}`);
  }

  getTurnActive(chatId: string): { startedAt: string } | null {
    const row = this.db
      .prepare(`SELECT value FROM settings_kv WHERE key = ?`)
      .get(`turn_active:${chatId}`) as { value: string } | undefined;
    if (!row) return null;
    try {
      const parsed = JSON.parse(row.value) as { startedAt?: unknown };
      return typeof parsed.startedAt === 'string' ? { startedAt: parsed.startedAt } : null;
    } catch {
      return null;
    }
  }

  /**
   * Newest first under {@link MESSAGE_ORDER_DESC}, capped at `limit`; callers
   * that want transcript order reverse it.
   */
  listMessages(chatId: string, limit = 200): ChatMessageRecord[] {
    const rows = this.db.prepare(`
      SELECT *
      FROM chat_messages
      WHERE chat_id = ?
      ORDER BY ${MESSAGE_ORDER_DESC}
      LIMIT ?
    `).all(chatId, LocalStore.clampInt(limit, 200, 1, 1000)) as Array<Record<string, unknown>>;
    return rows.map(row => this.mapChatMessage(row));
  }

  getMessage(chatId: string, messageId: string): ChatMessageRecord | null {
    const row = this.db
      .prepare(`SELECT * FROM chat_messages WHERE id = ? AND chat_id = ?`)
      .get(messageId, chatId) as Record<string, unknown> | undefined;
    return row ? this.mapChatMessage(row) : null;
  }

  /**
   * Deletes `messageId` and every message after it in the same chat. Used by
   * regenerate: truncate from the assistant reply being regenerated onward, so
   * the next turn re-runs against the history as it stood right before that
   * reply, instead of appending a new "please regenerate" user turn on top of
   * stale history.
   *
   * "After" is {@link MESSAGE_CUT_FROM_ID}, the same key `listMessages` orders
   * by, so the cut matches what the user sees below the regenerated reply.
   */
  deleteMessagesFrom(chatId: string, messageId: string): number {
    const info = this.db
      .prepare(`
        DELETE FROM chat_messages
        WHERE chat_id = ?
          AND ${MESSAGE_CUT_FROM_ID}
      `)
      .run(chatId, messageId, chatId);
    return info.changes;
  }

  addMessage(chatId: string, role: ChatMessageRecord['role'], content: string, messageMetadata: string | null = null): ChatMessageRecord {
    const id = randomUUID();
    const now = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO chat_messages (id, chat_id, role, content, message_metadata, created_at)
      VALUES (?, ?, ?, ?, ?, ?)
    `).run(id, chatId, role, content, messageMetadata, now);
    this.db.prepare(`UPDATE chat_sessions SET updated_at = ? WHERE id = ?`).run(now, chatId);
    const row = this.db.prepare(`SELECT * FROM chat_messages WHERE id = ?`).get(id) as Record<string, unknown>;
    return this.mapChatMessage(row);
  }

  /**
   * W1.2.1: replace the chat's whole message list with a branch projection.
   * The desktop store is the UI projection of the ACTIVE framework record —
   * switching branches re-projects it. The previous projection is not lost:
   * its source of truth is the framework history record it came from.
   * Timestamps are synthesized (base + index ms) to preserve projection
   * order under the created_at ordering.
   */
  replaceChatMessages(
    chatId: string,
    messages: Array<{ role: ChatMessageRecord['role']; content: string }>,
  ): void {
    const base = Date.now();
    const insert = this.db.prepare(`
      INSERT INTO chat_messages (id, chat_id, role, content, message_metadata, created_at)
      VALUES (?, ?, ?, ?, NULL, ?)
    `);
    this.db.transaction(() => {
      this.db.prepare(`DELETE FROM chat_messages WHERE chat_id = ?`).run(chatId);
      messages.forEach((m, i) => {
        insert.run(randomUUID(), chatId, m.role, m.content, new Date(base + i).toISOString());
      });
      this.db
        .prepare(`UPDATE chat_sessions SET updated_at = ? WHERE id = ?`)
        .run(new Date(base + messages.length).toISOString(), chatId);
    })();
  }

  listChatAgents(includeArchived = false): ChatAgentRecord[] {
    const rows = this.db.prepare(`
      SELECT *
      FROM chat_agents
      ${includeArchived ? '' : 'WHERE is_archived = 0'}
      ORDER BY sort_order ASC, created_at ASC, rowid ASC
    `).all() as Array<Record<string, unknown>>;
    return rows.map(row => this.mapChatAgent(row));
  }

  getChatAgent(agentId: string): ChatAgentRecord | null {
    const row = this.db.prepare(`SELECT * FROM chat_agents WHERE id = ?`).get(agentId) as Record<string, unknown> | undefined;
    if (!row) return null;
    return this.mapChatAgent(row);
  }

  createChatAgent(input: Partial<ChatAgentRecord> & { name: string }): ChatAgentRecord {
    const id = input.id || randomUUID();
    const now = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO chat_agents (
        id, slug, name, icon, color, description, role_prompt, forbidden_prompt,
        skill_ids, tool_policy, allow_external_skills, load_all_skills,
        is_builtin, is_archived, sort_order, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      id,
      input.slug ?? null,
      input.name,
      input.icon ?? null,
      input.color ?? null,
      input.description ?? null,
      input.rolePrompt ?? null,
      input.forbiddenPrompt ?? null,
      JSON.stringify(input.skillIds ?? []),
      JSON.stringify(input.toolPolicy ?? { mode: 'all', tools: [] }),
      input.allowExternalSkills ? 1 : 0,
      input.loadAllSkills ? 1 : 0,
      input.isBuiltin ? 1 : 0,
      input.isArchived ? 1 : 0,
      input.sortOrder ?? 0,
      now,
      now
    );
    const created = this.getChatAgent(id);
    if (!created) throw new Error('Failed to create chat agent');
    return created;
  }

  updateChatAgent(agentId: string, updates: Partial<ChatAgentRecord>): ChatAgentRecord | null {
    const existing = this.getChatAgent(agentId);
    if (!existing) return null;
    // 过滤 undefined（同 updateChat）：PATCH 路由对"未提供的字段"传 undefined，
    // 直接展开会把现有值抹成 undefined——sort_order 等 NOT NULL 列写入直接抛，
    // icon / forbidden_prompt 等可空列被静默清空。
    const clean = Object.fromEntries(
      Object.entries(updates).filter(([, value]) => value !== undefined),
    );
    const merged: ChatAgentRecord = {
      ...existing,
      ...clean,
      updatedAt: new Date().toISOString(),
    };
    this.db.prepare(`
      UPDATE chat_agents
      SET slug = ?, name = ?, icon = ?, color = ?, description = ?, role_prompt = ?, forbidden_prompt = ?,
          skill_ids = ?, tool_policy = ?, allow_external_skills = ?, load_all_skills = ?,
          is_builtin = ?, is_archived = ?, sort_order = ?, updated_at = ?
      WHERE id = ?
    `).run(
      merged.slug,
      merged.name,
      merged.icon,
      merged.color,
      merged.description,
      merged.rolePrompt,
      merged.forbiddenPrompt,
      JSON.stringify(merged.skillIds),
      JSON.stringify(merged.toolPolicy),
      merged.allowExternalSkills ? 1 : 0,
      merged.loadAllSkills ? 1 : 0,
      merged.isBuiltin ? 1 : 0,
      merged.isArchived ? 1 : 0,
      merged.sortOrder,
      merged.updatedAt,
      agentId
    );
    return this.getChatAgent(agentId);
  }

  archiveChatAgent(agentId: string): boolean {
    const updated = this.updateChatAgent(agentId, { isArchived: true });
    return Boolean(updated);
  }

  saveTrace(input: {
    id: string;
    chatId: string;
    messageId?: string | null;
    startedAtMs: number;
    durationMs?: number | null;
    status: string;
    payload: Record<string, unknown>;
  }): HarnessTraceRecord {
    const now = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO harness_traces (id, chat_id, message_id, started_at_ms, duration_ms, status, payload, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      input.id,
      input.chatId,
      input.messageId ?? null,
      input.startedAtMs,
      input.durationMs ?? null,
      input.status,
      JSON.stringify(input.payload),
      now
    );

    const row = this.db.prepare(`SELECT * FROM harness_traces WHERE id = ?`).get(input.id) as Record<string, unknown>;
    return this.mapHarnessTrace(row);
  }

  listTracesByChat(chatId: string, limit = 50): HarnessTraceRecord[] {
    const rows = this.db.prepare(`
      SELECT *
      FROM harness_traces
      WHERE chat_id = ?
      ORDER BY started_at_ms DESC
      LIMIT ?
    `).all(chatId, LocalStore.clampInt(limit, 50, 1, 500)) as Array<Record<string, unknown>>;
    return rows.map(row => this.mapHarnessTrace(row));
  }

  getTrace(traceId: string): HarnessTraceRecord | null {
    const row = this.db.prepare(`SELECT * FROM harness_traces WHERE id = ?`).get(traceId) as Record<string, unknown> | undefined;
    if (!row) return null;
    return this.mapHarnessTrace(row);
  }

  // ─── 4.6a 跨 turn 后台任务 ───────────────────────────────────────────

  createTask(input: {
    chatId: string;
    task: string;
    worktreePath?: string | null;
    worktreeBranch?: string | null;
    recordId?: string | null;
    /** 编排依赖（taskId 列表）；非空且未就绪时初始状态为 blocked 而非 running。 */
    dependsOn?: string[] | null;
    /** 初始状态（缺省 running；有未就绪依赖时调用方传 blocked）。 */
    initialStatus?: 'blocked' | 'running';
  }): TaskRecord {
    const id = randomUUID();
    const now = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO tasks (
        id, chat_id, task, status, answer, error,
        worktree_path, worktree_branch, worktree_state, record_id, trace_id,
        depends_on, created_at, updated_at
      ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, NULL, ?, ?, ?)
    `).run(
      id,
      input.chatId,
      input.task,
      input.initialStatus ?? 'running',
      input.worktreePath ?? null,
      input.worktreeBranch ?? null,
      // worktree 任务一创建就进入 pending（等待终态后合并/丢弃）；
      // 非 worktree 任务该列无意义，保持 NULL。
      input.worktreePath ? 'pending' : null,
      input.recordId ?? null,
      input.dependsOn?.length ? JSON.stringify(input.dependsOn) : null,
      now,
      now,
    );
    const row = this.db.prepare(`SELECT * FROM tasks WHERE id = ?`).get(id) as Record<string, unknown>;
    return this.mapTask(row);
  }

  getTask(taskId: string): TaskRecord | null {
    const row = this.db.prepare(`SELECT * FROM tasks WHERE id = ?`).get(taskId) as Record<string, unknown> | undefined;
    return row ? this.mapTask(row) : null;
  }

  /** 按 chat 列任务（updated_at 新→旧）；chatId 缺省时列全部（任务面板的全局视图）。 */
  listTasks(chatId?: string, limit = 100): TaskRecord[] {
    const safeLimit = LocalStore.clampInt(limit, 100, 1, 500);
    const rows = (chatId
      ? this.db.prepare(`SELECT * FROM tasks WHERE chat_id = ? ORDER BY datetime(updated_at) DESC LIMIT ?`).all(chatId, safeLimit)
      : this.db.prepare(`SELECT * FROM tasks ORDER BY datetime(updated_at) DESC LIMIT ?`).all(safeLimit)
    ) as Array<Record<string, unknown>>;
    return rows.map(row => this.mapTask(row));
  }

  /**
   * 终态/进度回写。只写传入的字段（undefined 过滤），updated_at 总是刷新。
   * 与 updateChat 同约定：显式传 null 才是置空。
   */
  updateTask(
    taskId: string,
    updates: Partial<Pick<TaskRecord, 'status' | 'answer' | 'error' | 'worktreeState' | 'traceId' | 'recordId'>>,
  ): TaskRecord | null {
    const existing = this.getTask(taskId);
    if (!existing) return null;
    const clean = Object.fromEntries(
      Object.entries(updates).filter(([, value]) => value !== undefined),
    );
    const next: TaskRecord = {
      ...existing,
      ...clean,
      updatedAt: new Date().toISOString(),
    };
    this.db.prepare(`
      UPDATE tasks
      SET status = ?, answer = ?, error = ?, worktree_state = ?, trace_id = ?, record_id = ?, updated_at = ?
      WHERE id = ?
    `).run(
      next.status,
      next.answer,
      next.error,
      next.worktreeState,
      next.traceId,
      next.recordId,
      next.updatedAt,
      taskId,
    );
    return this.getTask(taskId);
  }

  /**
   * 回写推理时间线。故意不碰 updated_at——流过程中的增量不应把任务
   * 面板按「最近活动」反复顶到最上。
   */
  saveTaskProcess(taskId: string, processJson: string): void {
    this.db.prepare(`UPDATE tasks SET process_json = ? WHERE id = ?`).run(processJson, taskId);
  }

  /**
   * 启动清扫：running 任务只可能属于上一个已死的进程（任务流不跨进程
   * 存活），全部落成 failed/interrupted——与 W7-1 turn_active 检测器同
   * 理，残留 running 是崩溃签名，不是真相。
   */
  failRunningTasks(reason: string): number {
    const info = this.db
      .prepare(`UPDATE tasks SET status = 'failed', error = ?, updated_at = ? WHERE status = 'running'`)
      .run(reason, new Date().toISOString());
    // 级联：blocked 任务的点火能力在 TaskService（进程内），不跨进程存活；
    // 重启后统一标 failed，模型经 task_status 看到原因后可重跑。
    const blocked = this.db
      .prepare(`SELECT id FROM tasks WHERE status = 'blocked'`)
      .all() as Array<{ id: string }>;
    const now = new Date().toISOString();
    for (const row of blocked) {
      this.db
        .prepare(`UPDATE tasks SET status = 'failed', error = ?, updated_at = ? WHERE id = ?`)
        .run(`进程重启中断了编排等待（依赖任务已随进程终止）；请重新 task_run。`, now, row.id);
    }
    return info.changes + blocked.length;
  }

  // ─── W6-9 用量与成本归因 ─────────────────────────────────────────────

  /**
   * 记录一条用量事件(一轮 chat / 一次标题生成 / 一次摘要压缩等)。
   * `costUsd` 为 null 表示该模型无单价(本地/未知)——存 NULL,面板渲染为 "—"。
   */
  recordUsageEvent(input: {
    chatId?: string | null;
    kind: string;
    provider?: string | null;
    model?: string | null;
    promptTokens: number;
    completionTokens: number;
    totalTokens: number;
    cachedPromptTokens?: number;
    costUsd?: number | null;
  }): void {
    this.db.prepare(`
      INSERT INTO usage_events
        (id, chat_id, kind, provider, model, prompt_tokens, completion_tokens, total_tokens, cached_prompt_tokens, cost_usd, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      randomUUID(),
      input.chatId ?? null,
      input.kind,
      input.provider ?? null,
      input.model ?? null,
      Math.max(0, Math.floor(input.promptTokens)),
      Math.max(0, Math.floor(input.completionTokens)),
      Math.max(0, Math.floor(input.totalTokens)),
      Math.max(0, Math.floor(input.cachedPromptTokens ?? 0)),
      input.costUsd ?? null,
      new Date().toISOString()
    );
  }

  /**
   * 聚合最近 `sinceDays` 天的用量:按 model 分桶( token 合计 + 成本合计),
   * 外加总计。成本只统计有单价的模型(cost_usd 非 NULL);无单价模型的
   * token 照常计入,但成本列不计。返回按 totalTokens 降序。
   */
  getUsageSummary(sinceDays = 30): UsageSummary {
    const since = new Date(Date.now() - sinceDays * 24 * 60 * 60 * 1000).toISOString();
    const rows = this.db.prepare(`
      SELECT
        COALESCE(model, '(unknown)') AS model,
        COALESCE(provider, '') AS provider,
        COUNT(*) AS turns,
        SUM(prompt_tokens) AS prompt_tokens,
        SUM(completion_tokens) AS completion_tokens,
        SUM(total_tokens) AS total_tokens,
        SUM(cached_prompt_tokens) AS cached_prompt_tokens,
        SUM(cost_usd) AS cost_usd
      FROM usage_events
      WHERE created_at >= ?
      GROUP BY model, provider
      ORDER BY total_tokens DESC
    `).all(since) as UsageSummaryRow[];
    return buildUsageSummary(rows, sinceDays);
  }

  getLlmSettings(): LlmSettings | null {
    const row = this.db.prepare(`SELECT value FROM settings_kv WHERE key = 'llm_settings'`).get() as { value: string } | undefined;
    if (!row) return null;
    try {
      return JSON.parse(row.value) as LlmSettings;
    } catch {
      return null;
    }
  }

  setLlmSettings(settings: LlmSettings): LlmSettings {
    const merged = mergeLlmSettings(settings);
    this.db.prepare(`
      INSERT INTO settings_kv (key, value) VALUES ('llm_settings', ?)
      ON CONFLICT(key) DO UPDATE SET value = excluded.value
    `).run(JSON.stringify(merged));
    return merged;
  }

  // ─── W6-6 遥测(OTLP collector + 隐私模式)────────────────────────────

  /** 读取遥测设置;从未配置过时返回 null(调用方按"关"处理)。 */
  getTelemetrySettings(): TelemetrySettings | null {
    const row = this.db.prepare(`SELECT value FROM settings_kv WHERE key = 'telemetry_settings'`).get() as { value: string } | undefined;
    if (!row) return null;
    try {
      return mergeTelemetrySettings(JSON.parse(row.value) as Partial<TelemetrySettings>);
    } catch {
      return null;
    }
  }

  setTelemetrySettings(settings: Partial<TelemetrySettings>): TelemetrySettings {
    const merged = mergeTelemetrySettings(settings);
    this.db.prepare(`
      INSERT INTO settings_kv (key, value) VALUES ('telemetry_settings', ?)
      ON CONFLICT(key) DO UPDATE SET value = excluded.value
    `).run(JSON.stringify(merged));
    return merged;
  }

  getWebSearchSettings(): WebSearchSettings | null {
    const row = this.db.prepare(`SELECT value FROM settings_kv WHERE key = 'web_search_settings'`).get() as { value: string } | undefined;
    if (!row) return null;
    try {
      return mergeWebSearchSettings(JSON.parse(row.value) as Partial<WebSearchSettings>);
    } catch {
      return null;
    }
  }

  setWebSearchSettings(settings: Partial<WebSearchSettings>): WebSearchSettings {
    const merged = mergeWebSearchSettings(settings);
    this.db.prepare(`
      INSERT INTO settings_kv (key, value) VALUES ('web_search_settings', ?)
      ON CONFLICT(key) DO UPDATE SET value = excluded.value
    `).run(JSON.stringify(merged));
    return merged;
  }

  // ─── 帮助改进产品（行为 / 对话 / 资料 分项同意 + 本地队列）──────────

  getInsightsSettings(): InsightsSettings | null {
    const row = this.db.prepare(`SELECT value FROM settings_kv WHERE key = 'insights_settings'`).get() as
      | { value: string }
      | undefined;
    if (!row) return null;
    try {
      return mergeInsightsSettings(JSON.parse(row.value) as Partial<InsightsSettings>);
    } catch {
      return null;
    }
  }

  ensureInsightsSettings(): InsightsSettings {
    const existing = this.getInsightsSettings();
    if (existing) return existing;
    return this.setInsightsSettings({});
  }

  setInsightsSettings(settings: InsightsSettingsPatch): InsightsSettings {
    const current = this.getInsightsSettings();
    const merged = mergeInsightsSettings({
      ...current,
      ...settings,
      installId: current?.installId ?? settings.installId,
      profile: mergeInsightsProfile({ ...current?.profile, ...settings.profile }),
    });
    this.db.prepare(`
      INSERT INTO settings_kv (key, value) VALUES ('insights_settings', ?)
      ON CONFLICT(key) DO UPDATE SET value = excluded.value
    `).run(JSON.stringify(merged));
    return merged;
  }

  enqueueInsight(kind: InsightKind, payload: Record<string, unknown>): InsightOutboxRow {
    const row: InsightOutboxRow = {
      id: randomUUID(),
      kind,
      payload,
      createdAt: new Date().toISOString(),
      uploadedAt: null,
      uploadError: null,
    };
    this.db.prepare(`
      INSERT INTO insights_outbox (id, kind, payload, created_at, uploaded_at, upload_error)
      VALUES (?, ?, ?, ?, NULL, NULL)
    `).run(row.id, row.kind, JSON.stringify(row.payload), row.createdAt);
    return row;
  }

  listInsightOutbox(opts: { uploaded?: boolean; kind?: InsightKind; limit?: number } = {}): InsightOutboxRow[] {
    const limit = Math.max(1, Math.min(opts.limit ?? 200, 500));
    let sql = `SELECT * FROM insights_outbox`;
    const where: string[] = [];
    const params: unknown[] = [];
    if (opts.kind) {
      where.push('kind = ?');
      params.push(opts.kind);
    }
    if (opts.uploaded === true) {
      where.push('uploaded_at IS NOT NULL');
    } else if (opts.uploaded === false) {
      where.push('uploaded_at IS NULL');
    }
    if (where.length) sql += ` WHERE ${where.join(' AND ')}`;
    sql += ` ORDER BY created_at DESC LIMIT ?`;
    params.push(limit);
    const rows = this.db.prepare(sql).all(...params) as Array<Record<string, unknown>>;
    return rows.map((row) => this.mapInsightOutbox(row));
  }

  markInsightUploaded(id: string): void {
    this.db.prepare(
      `UPDATE insights_outbox SET uploaded_at = ?, upload_error = NULL WHERE id = ?`,
    ).run(new Date().toISOString(), id);
  }

  markInsightUploadError(id: string, error: string): void {
    this.db.prepare(`UPDATE insights_outbox SET upload_error = ? WHERE id = ?`).run(error.slice(0, 200), id);
  }

  insightStats(): { events: number; turns: number; profile: number; pending: number } {
    const row = this.db.prepare(`
      SELECT
        SUM(CASE WHEN kind = 'event' THEN 1 ELSE 0 END) AS events,
        SUM(CASE WHEN kind = 'turn' THEN 1 ELSE 0 END) AS turns,
        SUM(CASE WHEN kind = 'profile' THEN 1 ELSE 0 END) AS profile,
        SUM(CASE WHEN uploaded_at IS NULL THEN 1 ELSE 0 END) AS pending
      FROM insights_outbox
    `).get() as { events: number; turns: number; profile: number; pending: number };
    return {
      events: Number(row?.events ?? 0),
      turns: Number(row?.turns ?? 0),
      profile: Number(row?.profile ?? 0),
      pending: Number(row?.pending ?? 0),
    };
  }

  exportInsightsBundle(): {
    schema: typeof INSIGHTS_EXPORT_SCHEMA;
    exportedAt: string;
    installId: string;
    settings: {
      shareBehavior: boolean;
      shareConversation: boolean;
      shareProfile: boolean;
    };
    profile: InsightsProfile;
    stats: { events: number; turns: number; profile: number; pending: number };
    records: InsightOutboxRow[];
  } {
    const settings = this.ensureInsightsSettings();
    return {
      schema: INSIGHTS_EXPORT_SCHEMA,
      exportedAt: new Date().toISOString(),
      installId: settings.installId,
      settings: {
        shareBehavior: settings.shareBehavior,
        shareConversation: settings.shareConversation,
        shareProfile: settings.shareProfile,
      },
      profile: settings.profile,
      stats: this.insightStats(),
      records: this.listInsightOutbox({ limit: 500 }),
    };
  }

  private mapInsightOutbox(row: Record<string, unknown>): InsightOutboxRow {
    return {
      id: String(row.id),
      kind: row.kind === 'turn' || row.kind === 'profile' ? row.kind : 'event',
      payload: this.safeJson(row.payload, {}),
      createdAt: String(row.created_at),
      uploadedAt: row.uploaded_at ? String(row.uploaded_at) : null,
      uploadError: row.upload_error ? String(row.upload_error) : null,
    };
  }

  // ─── 场景包存储 ─────────────────────────────────────────────────
  // 包数据访问收敛进包内存储（0.4，如 packages/pack-*/src/store.ts）。
  // 宿主只通过 getPackDb() 把同一个 SQLite 句柄借给包存储。

  private mapChatSession(row: Record<string, unknown>): ChatSessionRecord {
    return {
      id: String(row.id),
      title: String(row.title ?? '新对话'),
      userId: String(row.user_id ?? DEFAULT_LOCAL_USER_ID),
      agentId: row.agent_id ? String(row.agent_id) : null,
      projectId: row.project_id ? String(row.project_id) : null,
      createdAt: String(row.created_at),
      updatedAt: String(row.updated_at),
      isPinned: Number(row.is_pinned ?? 0) === 1,
      systemPrompt: row.system_prompt ? String(row.system_prompt) : null,
      pinnedRefs: row.pinned_refs ? this.safeJson(row.pinned_refs) : null,
    };
  }

  private mapChatMessage(row: Record<string, unknown>): ChatMessageRecord {
    return {
      id: String(row.id),
      chatId: String(row.chat_id),
      role: String(row.role) as ChatMessageRecord['role'],
      content: String(row.content ?? ''),
      messageMetadata: row.message_metadata ? String(row.message_metadata) : null,
      createdAt: String(row.created_at),
    };
  }

  private mapHarnessTrace(row: Record<string, unknown>): HarnessTraceRecord {
    return {
      id: String(row.id),
      chatId: String(row.chat_id),
      messageId: row.message_id ? String(row.message_id) : null,
      startedAtMs: Number(row.started_at_ms ?? 0),
      durationMs: row.duration_ms === null || row.duration_ms === undefined ? null : Number(row.duration_ms),
      status: String(row.status ?? 'unknown'),
      payload: String(row.payload ?? '{}'),
      createdAt: String(row.created_at ?? new Date().toISOString()),
    };
  }

  private mapTask(row: Record<string, unknown>): TaskRecord {
    return {
      id: String(row.id),
      chatId: String(row.chat_id),
      task: String(row.task ?? ''),
      status: String(row.status ?? 'running') as TaskRecord['status'],
      answer: row.answer != null ? String(row.answer) : null,
      error: row.error != null ? String(row.error) : null,
      worktreePath: row.worktree_path != null ? String(row.worktree_path) : null,
      worktreeBranch: row.worktree_branch != null ? String(row.worktree_branch) : null,
      worktreeState: (row.worktree_state != null ? String(row.worktree_state) : null) as TaskRecord['worktreeState'],
      recordId: row.record_id != null ? String(row.record_id) : null,
      traceId: row.trace_id != null ? String(row.trace_id) : null,
      dependsOn: parseDependsOn(row.depends_on),
      processJson: row.process_json != null ? String(row.process_json) : null,
      createdAt: String(row.created_at),
      updatedAt: String(row.updated_at),
    };
  }

  private mapChatAgent(row: Record<string, unknown>): ChatAgentRecord {
    return {
      id: String(row.id),
      slug: row.slug ? String(row.slug) : null,
      name: String(row.name),
      icon: row.icon ? String(row.icon) : null,
      color: row.color ? String(row.color) : null,
      description: row.description ? String(row.description) : null,
      rolePrompt: row.role_prompt ? String(row.role_prompt) : null,
      forbiddenPrompt: row.forbidden_prompt ? String(row.forbidden_prompt) : null,
      skillIds: this.safeJson(row.skill_ids, []),
      toolPolicy: this.safeJson(row.tool_policy, { mode: 'all', tools: [] }),
      allowExternalSkills: Number(row.allow_external_skills ?? 0) === 1,
      loadAllSkills: Number(row.load_all_skills ?? 0) === 1,
      isBuiltin: Number(row.is_builtin ?? 0) === 1,
      isArchived: Number(row.is_archived ?? 0) === 1,
      sortOrder: Number(row.sort_order ?? 0),
      createdAt: String(row.created_at),
      updatedAt: String(row.updated_at),
    };
  }

  private safeJson<T>(value: unknown, fallback: T | null = null): T {
    if (typeof value !== 'string') return fallback as T;
    try {
      return JSON.parse(value) as T;
    } catch {
      return fallback as T;
    }
  }
}

export const localStore = createLocalStore(() => new LocalStore());
