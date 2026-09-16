import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { getBrand } from '../brand.js';
import {
  resolveInsightsApiBase,
  rowsEligibleForAutoUpload,
  type InsightKind,
  type InsightsSettings,
} from '../storage/insights-settings.js';
import { localStore, type InsightOutboxRow } from '../storage/index.js';

const FLUSH_TIMEOUT_MS = 8_000;
const MAX_BATCH = 20;

export function insightsNetworkDisabled(): boolean {
  return Boolean(process.env.VITEST) || process.env.DEEPPATH_INSIGHTS_DISABLE_UPLOAD === '1';
}

let cachedVersion: string | null = null;

/** Packaged Electron has no npm_package_version — read package.json like brand.ts does. */
function detectAppVersion(): string {
  if (cachedVersion) return cachedVersion;
  const fromEnv = process.env.npm_package_version;
  if (fromEnv) {
    cachedVersion = fromEnv;
    return fromEnv;
  }
  try {
    const here = path.dirname(fileURLToPath(import.meta.url));
    const pkgPath = path.join(here, '..', '..', 'package.json');
    const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8')) as { version?: string };
    if (typeof pkg.version === 'string' && pkg.version) {
      cachedVersion = pkg.version;
      return cachedVersion;
    }
  } catch {
    // fall through
  }
  cachedVersion = '0.0.0';
  return cachedVersion;
}

function clientMeta(settings: InsightsSettings): {
  installId: string;
  flavor: string;
  appVersion: string;
} {
  return {
    installId: settings.installId,
    flavor: getBrand().flavor,
    appVersion: detectAppVersion(),
  };
}

export function buildInsightsExportPayload(): Record<string, unknown> {
  const settings = localStore.ensureInsightsSettings();
  return {
    ...localStore.exportInsightsBundle(),
    ...clientMeta(settings),
  };
}

function ingestHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const token = (process.env.DEEPPATH_INSIGHTS_INGEST_TOKEN || '').trim();
  // 线协议常量：header 名由遥测接收端（产品注入的 insightsApiBase）定义，
  // shell 只是按对端契约发送——不属产品硬编码。
  if (token) headers['X-DeepPath-Insights-Key'] = token; // shell-neutral:allow
  return headers;
}

export async function postInsightsJson(url: string, body: unknown): Promise<boolean> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FLUSH_TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: ingestHeaders(),
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    return res.ok;
  } catch (err) {
    console.warn('[insights] upload failed', err instanceof Error ? err.message : err);
    return false;
  } finally {
    clearTimeout(timer);
  }
}

function pathForKind(kind: InsightKind): string {
  if (kind === 'event') return '/api/v2/agent-insights/events';
  if (kind === 'turn') return '/api/v2/agent-insights/turns';
  return '/api/v2/agent-insights/profile';
}

export async function flushInsightsOutbox(): Promise<{
  uploaded: number;
  skipped: number;
  failed: number;
}> {
  if (insightsNetworkDisabled()) {
    return { uploaded: 0, skipped: 0, failed: 0 };
  }
  const settings = localStore.ensureInsightsSettings();
  const pending = localStore.listInsightOutbox({ uploaded: false, limit: MAX_BATCH });
  const { upload, skipped } = rowsEligibleForAutoUpload(settings, pending);
  let uploaded = 0;
  let failed = 0;
  const base = resolveInsightsApiBase(settings, process.env.DEEPPATH_INSIGHTS_API_BASE);
  // 无端点（中性 shell 未注入遥测配置）= 不上报，outbox 留本地待导出。
  if (!base) return { uploaded, skipped, failed };
  const meta = clientMeta(settings);

  for (const row of upload) {
    const ok = await postInsightsJson(`${base}${pathForKind(row.kind)}`, {
      ...meta,
      ...row.payload,
      clientEventId: row.id,
    });
    if (ok) {
      localStore.markInsightUploaded(row.id);
      uploaded += 1;
    } else {
      localStore.markInsightUploadError(row.id, 'upload_failed');
      failed += 1;
    }
  }
  return { uploaded, skipped, failed };
}

export async function uploadInsightsBundle(): Promise<boolean> {
  if (insightsNetworkDisabled()) {
    return false;
  }
  const settings = localStore.ensureInsightsSettings();
  const bundle = buildInsightsExportPayload();
  const base = resolveInsightsApiBase(settings, process.env.DEEPPATH_INSIGHTS_API_BASE);
  if (!base) return false;
  const ok = await postInsightsJson(`${base}/api/v2/agent-insights/bundle`, bundle);
  if (ok) {
    const records = Array.isArray(bundle.records) ? (bundle.records as InsightOutboxRow[]) : [];
    for (const row of records) localStore.markInsightUploaded(row.id);
  }
  return ok;
}

export function scheduleInsightsFlush(): void {
  void flushInsightsOutbox().catch((err) => {
    console.warn('[insights] flush crashed', err);
  });
}
