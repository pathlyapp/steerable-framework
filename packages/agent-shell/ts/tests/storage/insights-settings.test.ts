import { describe, expect, it } from 'vitest';
import {
  canAutoUpload,
  insightsNeedsPrompt,
  isUuid,
  mergeInsightsSettings,
  resolveInsightsApiBase,
  rowsEligibleForAutoUpload,
} from '../../src/storage/insights-settings';
import { redactInsightText, toolNamesOnly } from '../../src/storage/insights-redact';
import { setProductConfig } from '../../src/product-config';

const ID = '550e8400-e29b-41d4-a716-446655440000';

describe('insights consent flags are independent', () => {
  it('generates a uuid installId via node crypto when none is provided', () => {
    const s = mergeInsightsSettings({});
    expect(isUuid(s.installId)).toBe(true);
  });

  it('defaults to local-only (no auto upload, needs first prompt)', () => {
    const s = mergeInsightsSettings({}, () => ID);
    expect(s.installId).toBe(ID);
    expect(s.shareBehavior).toBe(false);
    expect(s.shareConversation).toBe(false);
    expect(s.shareProfile).toBe(false);
    expect(insightsNeedsPrompt(s)).toBe(true);
    expect(canAutoUpload(s, 'event')).toBe(false);
    expect(canAutoUpload(s, 'turn')).toBe(false);
    expect(canAutoUpload(s, 'profile')).toBe(false);
  });

  it('allows behavior without conversation or profile', () => {
    const s = mergeInsightsSettings(
      { installId: ID, shareBehavior: true, promptedAt: '2026-09-09T00:00:00.000Z' },
      () => ID,
    );
    expect(canAutoUpload(s, 'event')).toBe(true);
    expect(canAutoUpload(s, 'turn')).toBe(false);
    expect(canAutoUpload(s, 'profile')).toBe(false);
    expect(insightsNeedsPrompt(s)).toBe(false);
  });

  it('keeps a stable installId when merging later toggles', () => {
    const first = mergeInsightsSettings({ shareBehavior: true }, () => ID);
    const second = mergeInsightsSettings({ ...first, shareConversation: true }, () => '11111111-1111-4111-8111-111111111111');
    expect(second.installId).toBe(ID);
    expect(second.shareBehavior).toBe(true);
    expect(second.shareConversation).toBe(true);
  });
});

describe('insights redaction', () => {
  it('strips secrets and home directories from Q&A', () => {
    const text = redactInsightText(
      'sk-abc123456789 and C:\\Users\\alice\\secret.md /Users/bob/file',
      4000,
    );
    expect(text).not.toContain('sk-abc');
    expect(text).not.toContain('alice');
    expect(text).not.toContain('bob');
  });

  it('keeps tool names not arguments', () => {
    expect(toolNamesOnly([{ tool: 'local_exec_shell', arguments: { command: 'rm' } }, 'local_read_file'])).toEqual([
      'local_exec_shell',
      'local_read_file',
    ]);
  });
});

describe('insights api base', () => {
  it('prefers explicit setting then env then product-injected endpoint', () => {
    const s = mergeInsightsSettings({ installId: ID, apiBase: 'http://127.0.0.1:8000' }, () => ID);
    expect(resolveInsightsApiBase(s, 'https://ignored.example')).toBe('http://127.0.0.1:8000');
    const unset = mergeInsightsSettings({ installId: ID }, () => ID);
    expect(resolveInsightsApiBase(unset, 'http://127.0.0.1:8000')).toBe('http://127.0.0.1:8000');
    // 3.1 起最终兜底是产品注入端点（setProductConfig）；中性 shell 未注入 = ''。
    expect(resolveInsightsApiBase(unset, '')).toBe('');
    setProductConfig({ insightsApiBase: 'https://insights.example' });
    expect(resolveInsightsApiBase(unset, '')).toBe('https://insights.example');
  });
});

describe('auto-upload eligibility', () => {
  it('does not mix conversation or profile into behavior-only consent', () => {
    const s = mergeInsightsSettings(
      { installId: ID, shareBehavior: true, shareConversation: false, shareProfile: false },
      () => ID,
    );
    const { upload, skipped } = rowsEligibleForAutoUpload(s, [
      { kind: 'event' as const },
      { kind: 'turn' as const },
      { kind: 'profile' as const },
    ]);
    expect(upload.map((row) => row.kind)).toEqual(['event']);
    expect(skipped).toBe(2);
  });
});
