/**
 * W6-6 遥测合规化:OTLP collector 连接 + 隐私模式的纯逻辑(默认值/校验/归一)。
 *
 * 与 `llm-settings.ts` 同样的拆分理由:不依赖 electron / better-sqlite3,
 * 可在纯 Node/vitest 下直接单测。持久化在 `storage/index.ts` 的
 * `settings_kv` 表(key = 'telemetry_settings')。
 *
 * 隐私模型(与 framework `otel.py` 的 PrivacyMode 对齐):
 * - 未配置 endpoint = 关。一条 trace 都不出进程——这是默认,也是隐私安全默认。
 * - 'metadata':只导出结构/时延/状态,事件 payload 正文与自由属性一律丢弃。
 * - 'full':导出(已脱敏的)payload 与属性。仅当 collector 可信时选。
 */

export type TelemetryPrivacyMode = 'full' | 'metadata';

export interface TelemetrySettings {
  /**
   * OTLP/HTTP collector 的 /v1/traces URL,例如
   * `http://127.0.0.1:4318/v1/traces`。空/缺省 = 遥测关闭(默认)。
   */
  endpoint?: string;
  /** 隐私档位;缺省 'metadata'(可观测但不外发内容)。 */
  privacyMode: TelemetryPrivacyMode;
  /** 资源属性 service.name;缺省 'steerable-agent-desktop'。 */
  serviceName?: string;
}

export const DEFAULT_TELEMETRY_SETTINGS: TelemetrySettings = {
  endpoint: undefined, // 默认关闭:没有 collector 地址就一条都不发
  privacyMode: 'metadata',
  serviceName: 'steerable-agent-desktop',
};

/** 遥测是否启用:有合法 endpoint 才启用(privacyMode 不影响开关)。 */
export function telemetryEnabled(s: TelemetrySettings | null | undefined): boolean {
  return !!normalizeTelemetryEndpoint(s?.endpoint);
}

/**
 * 归一 endpoint:trim;空串视为未配置。只接受 http(s) URL——别的协议
 * (file: 等)直接拒绝,避免把 trace POST 到非预期 scheme。返回 undefined
 * 表示无效/未配置。
 */
export function normalizeTelemetryEndpoint(raw: unknown): string | undefined {
  if (typeof raw !== 'string') return undefined;
  const trimmed = raw.trim();
  if (!trimmed) return undefined;
  try {
    const url = new URL(trimmed);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return undefined;
    return trimmed;
  } catch {
    return undefined;
  }
}

function normalizePrivacyMode(raw: unknown): TelemetryPrivacyMode {
  return raw === 'full' ? 'full' : 'metadata';
}

/**
 * 合并/校验一份(可能残缺的)incoming 设置,落到合法 TelemetrySettings。
 * endpoint 非法一律归一为 undefined(=关),绝不让一个坏地址把遥测打开。
 */
export function mergeTelemetrySettings(
  settings?: Partial<TelemetrySettings> | null,
): TelemetrySettings {
  const incoming = settings ?? {};
  return {
    endpoint: normalizeTelemetryEndpoint(incoming.endpoint),
    privacyMode: normalizePrivacyMode(incoming.privacyMode),
    serviceName:
      typeof incoming.serviceName === 'string' && incoming.serviceName.trim()
        ? incoming.serviceName.trim()
        : DEFAULT_TELEMETRY_SETTINGS.serviceName,
  };
}
