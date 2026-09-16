import { describe, expect, it } from 'vitest';
import {
  DEFAULT_TELEMETRY_SETTINGS,
  mergeTelemetrySettings,
  normalizeTelemetryEndpoint,
  telemetryEnabled,
} from '../../src/storage/telemetry-settings';

describe('normalizeTelemetryEndpoint', () => {
  it('accepts an http(s) collector URL', () => {
    expect(normalizeTelemetryEndpoint('http://127.0.0.1:4318/v1/traces')).toBe(
      'http://127.0.0.1:4318/v1/traces',
    );
    expect(normalizeTelemetryEndpoint(' https://otel.example.com/v1/traces ')).toBe(
      'https://otel.example.com/v1/traces',
    );
  });

  it('treats empty / missing as unconfigured (telemetry off)', () => {
    expect(normalizeTelemetryEndpoint('')).toBeUndefined();
    expect(normalizeTelemetryEndpoint('   ')).toBeUndefined();
    expect(normalizeTelemetryEndpoint(undefined)).toBeUndefined();
    expect(normalizeTelemetryEndpoint(null)).toBeUndefined();
    expect(normalizeTelemetryEndpoint(4318)).toBeUndefined();
  });

  it('rejects non-http(s) schemes so a trace is never POSTed to file: etc.', () => {
    expect(normalizeTelemetryEndpoint('file:///etc/passwd')).toBeUndefined();
    expect(normalizeTelemetryEndpoint('ftp://x/v1/traces')).toBeUndefined();
    expect(normalizeTelemetryEndpoint('not a url')).toBeUndefined();
  });
});

describe('telemetryEnabled', () => {
  it('is off by default (no endpoint configured)', () => {
    expect(telemetryEnabled(DEFAULT_TELEMETRY_SETTINGS)).toBe(false);
    expect(telemetryEnabled(null)).toBe(false);
    expect(telemetryEnabled(undefined)).toBe(false);
  });

  it('is on once a valid endpoint is set, regardless of privacy mode', () => {
    expect(
      telemetryEnabled({ endpoint: 'http://127.0.0.1:4318/v1/traces', privacyMode: 'metadata' }),
    ).toBe(true);
    expect(
      telemetryEnabled({ endpoint: 'http://127.0.0.1:4318/v1/traces', privacyMode: 'full' }),
    ).toBe(true);
  });

  it('stays off when the endpoint is invalid', () => {
    expect(telemetryEnabled({ endpoint: 'file:///x', privacyMode: 'full' })).toBe(false);
  });
});

describe('mergeTelemetrySettings', () => {
  it('defaults to metadata privacy mode and the desktop service name', () => {
    const merged = mergeTelemetrySettings({
      endpoint: 'http://127.0.0.1:4318/v1/traces',
    });
    expect(merged.privacyMode).toBe('metadata');
    expect(merged.serviceName).toBe('steerable-agent-desktop');
    expect(merged.endpoint).toBe('http://127.0.0.1:4318/v1/traces');
  });

  it('honours an explicit full privacy mode', () => {
    const merged = mergeTelemetrySettings({
      endpoint: 'http://127.0.0.1:4318/v1/traces',
      privacyMode: 'full',
    });
    expect(merged.privacyMode).toBe('full');
  });

  it('normalizes an unknown privacy mode back to metadata', () => {
    const merged = mergeTelemetrySettings({
      endpoint: 'http://127.0.0.1:4318/v1/traces',
      // @ts-expect-error — deliberately invalid wire value
      privacyMode: 'everything',
    });
    expect(merged.privacyMode).toBe('metadata');
  });

  it('an invalid endpoint collapses to undefined (telemetry off), never left half-open', () => {
    const merged = mergeTelemetrySettings({ endpoint: 'file:///etc/passwd' });
    expect(merged.endpoint).toBeUndefined();
    expect(telemetryEnabled(merged)).toBe(false);
  });

  it('no-args yields the safe default (off)', () => {
    const merged = mergeTelemetrySettings();
    expect(merged.endpoint).toBeUndefined();
    expect(merged.privacyMode).toBe('metadata');
    expect(telemetryEnabled(merged)).toBe(false);
  });
});
