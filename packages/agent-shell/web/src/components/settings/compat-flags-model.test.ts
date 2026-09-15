import { describe, expect, it } from 'vitest';
import {
  COMPAT_AUTO,
  formStateFromOverrides,
  overridesFromFormState,
} from './compat-flags-model';
import type { CompatFlagDescriptor } from '@/lib/local-api';

// 与框架 compat.py `_FLAG_WIRE_SPEC` 同形的描述符夹具。
const FLAGS: CompatFlagDescriptor[] = [
  { key: 'supportsUsageInStreaming', field: 'supports_usage_in_streaming', kind: 'bool', default: true, description: '' },
  { key: 'maxTokensField', field: 'max_tokens_field', kind: 'enum:max_tokens,max_completion_tokens', default: 'max_tokens', description: '' },
  { key: 'supportsTemperature', field: 'supports_temperature', kind: 'bool', default: true, description: '' },
  { key: 'reasoningDeltaFields', field: 'reasoning_delta_fields', kind: 'string-list', default: ['reasoning_content', 'reasoning'], description: '' },
];

describe('compat-flags-model (W1.3.2)', () => {
  it('unset overrides render as auto for every flag', () => {
    expect(formStateFromOverrides(undefined, FLAGS)).toEqual({
      supportsUsageInStreaming: COMPAT_AUTO,
      maxTokensField: COMPAT_AUTO,
      supportsTemperature: COMPAT_AUTO,
      reasoningDeltaFields: COMPAT_AUTO,
    });
  });

  it('persisted overrides map to their form values', () => {
    const state = formStateFromOverrides(
      { supportsTemperature: false, reasoningDeltaFields: ['reasoning_content'] },
      FLAGS,
    );
    expect(state.supportsTemperature).toBe('false');
    expect(state.reasoningDeltaFields).toBe('reasoning_content');
    expect(state.maxTokensField).toBe(COMPAT_AUTO);
  });

  it('all-auto form state produces no overrides (auto-detect fallback)', () => {
    const state = formStateFromOverrides(undefined, FLAGS);
    expect(overridesFromFormState(state, FLAGS)).toBeUndefined();
  });

  it('form state builds a typed overrides payload', () => {
    const overrides = overridesFromFormState(
      {
        supportsTemperature: 'false',
        maxTokensField: 'max_completion_tokens',
        reasoningDeltaFields: 'reasoning_content, reasoning',
        supportsUsageInStreaming: COMPAT_AUTO,
      },
      FLAGS,
    );
    expect(overrides).toEqual({
      supportsTemperature: false,
      maxTokensField: 'max_completion_tokens',
      reasoningDeltaFields: ['reasoning_content', 'reasoning'],
    });
  });

  it('invalid enum values and empty lists are dropped', () => {
    const overrides = overridesFromFormState(
      { maxTokensField: 'bogus_field', reasoningDeltaFields: '  ' },
      FLAGS,
    );
    expect(overrides).toBeUndefined();
  });

  it('round-trips: overrides → form → overrides', () => {
    const original = { supportsTemperature: false as const, reasoningDeltaFields: ['reasoning'] };
    expect(overridesFromFormState(formStateFromOverrides(original, FLAGS), FLAGS)).toEqual(original);
  });
});
