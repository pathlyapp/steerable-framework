import { randomUUID } from 'crypto';

import { getProductConfig } from '../product-config.js';

/**
 * Product-insight consent + install identity. Pure logic (no sqlite/electron)
 * so vitest can cover the three separate confirmation flags.
 *
 * - shareBehavior: 行为事件可否自动上传
 * - shareConversation: 问答可否自动上传
 * - shareProfile: 用户资料可否自动上传
 * 三者默认全关。关掉的种类仍写入本地 outbox，用户可导出后手动发给开发者。
 *
 * 遥测端点（3.1 起产品注入）：优先级 用户设置 apiBase > 环境变量
 * DEEPPATH_INSIGHTS_API_BASE > 产品注入（product.json insightsApiBase）。
 * 三者皆空 = 无上报端点（中性 shell 默认），flush 直接跳过。
 */

export interface InsightsProfile {
  displayName: string;
  email: string;
  company: string;
  note: string;
}

export interface InsightsSettings {
  installId: string;
  shareBehavior: boolean;
  shareConversation: boolean;
  shareProfile: boolean;
  /** ISO time of first consent prompt answer; empty = not prompted yet. */
  promptedAt?: string;
  /** Optional override. Empty = env DEEPPATH_INSIGHTS_API_BASE or 产品注入端点。 */
  apiBase?: string;
  profile: InsightsProfile;
}

export const DEFAULT_INSIGHTS_PROFILE: InsightsProfile = {
  displayName: '',
  email: '',
  company: '',
  note: '',
};

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function isUuid(value: unknown): value is string {
  return typeof value === 'string' && UUID_RE.test(value.trim());
}

export function normalizeInsightsApiBase(raw: unknown): string | undefined {
  if (typeof raw !== 'string') return undefined;
  const trimmed = raw.trim().replace(/\/+$/, '');
  if (!trimmed) return undefined;
  try {
    const url = new URL(trimmed);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return undefined;
    return trimmed;
  } catch {
    return undefined;
  }
}

/**
 * 解析遥测端点：用户设置 > 环境变量 > 产品注入。皆空返回 ''（调用方
 * 视为空端点 = 遥测未配置，跳过上报）。
 */
export function resolveInsightsApiBase(
  settings: InsightsSettings | null | undefined,
  envBase?: string | null,
): string {
  return (
    normalizeInsightsApiBase(settings?.apiBase) ||
    normalizeInsightsApiBase(envBase) ||
    normalizeInsightsApiBase(getProductConfig().insightsApiBase) ||
    ''
  );
}

function asBool(value: unknown): boolean {
  return value === true;
}

function asTrimmed(value: unknown, max: number): string {
  if (typeof value !== 'string') return '';
  return value.trim().slice(0, max);
}

export function mergeInsightsProfile(raw?: Partial<InsightsProfile> | null): InsightsProfile {
  const incoming = raw ?? {};
  return {
    displayName: asTrimmed(incoming.displayName, 128),
    email: asTrimmed(incoming.email, 191),
    company: asTrimmed(incoming.company, 191),
    note: asTrimmed(incoming.note, 500),
  };
}

export function mergeInsightsSettings(
  settings?: InsightsSettingsPatch | null,
  generateId: () => string = () => randomUUID(),
): InsightsSettings {
  const incoming = settings ?? {};
  const installId = isUuid(incoming.installId) ? incoming.installId.trim().toLowerCase() : generateId();
  const promptedAt =
    typeof incoming.promptedAt === 'string' && incoming.promptedAt.trim()
      ? incoming.promptedAt.trim()
      : undefined;
  return {
    installId,
    shareBehavior: asBool(incoming.shareBehavior),
    shareConversation: asBool(incoming.shareConversation),
    shareProfile: asBool(incoming.shareProfile),
    promptedAt,
    apiBase: normalizeInsightsApiBase(incoming.apiBase),
    profile: mergeInsightsProfile(incoming.profile),
  };
}

export function insightsNeedsPrompt(settings: InsightsSettings | null | undefined): boolean {
  return !settings?.promptedAt;
}

export type InsightKind = 'event' | 'turn' | 'profile';

/** PATCH-style input: flags optional, profile fields individually optional. */
export type InsightsSettingsPatch = Partial<Omit<InsightsSettings, 'profile'>> & {
  profile?: Partial<InsightsProfile>;
};

export function canAutoUpload(settings: InsightsSettings, kind: InsightKind): boolean {
  if (kind === 'event') return settings.shareBehavior;
  if (kind === 'turn') return settings.shareConversation;
  return settings.shareProfile;
}

export function rowsEligibleForAutoUpload<T extends { kind: InsightKind }>(
  settings: InsightsSettings,
  rows: T[],
): { upload: T[]; skipped: number } {
  const upload: T[] = [];
  let skipped = 0;
  for (const row of rows) {
    if (canAutoUpload(settings, row.kind)) upload.push(row);
    else skipped += 1;
  }
  return { upload, skipped };
}
