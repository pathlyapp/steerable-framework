import { describe, expect, it } from 'vitest';
import { LOCAL_ASSISTANT_AGENT_ID, pickDefaultAgentId } from './brand';

describe('pickDefaultAgentId', () => {
  const catalog = [
    { id: LOCAL_ASSISTANT_AGENT_ID },
    { id: 'cflog-operator' },
    { id: 'all-round-assistant' },
  ];

  it('returns null when the catalog is empty', () => {
    expect(pickDefaultAgentId([])).toBeNull();
  });

  it('prefers the build-time default (shell: 电脑操作员) even when it is not first', () => {
    expect(pickDefaultAgentId(catalog)).toBe(LOCAL_ASSISTANT_AGENT_ID);
  });

  it('falls back to the first visible agent when the build default is absent', () => {
    expect(pickDefaultAgentId([{ id: 'writer' }, { id: 'reader' }])).toBe('writer');
  });
});
