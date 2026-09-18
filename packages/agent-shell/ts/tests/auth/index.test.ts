import { describe, expect, it, vi } from 'vitest';

import {
  getAuthProvider,
  registerAuthProvider,
  type AuthProvider,
} from '../../src/auth/index.js';

function provider(id: string): AuthProvider {
  return {
    id,
    authenticate: vi.fn(),
    describeSelf: vi.fn(),
  };
}

describe('auth provider registry', () => {
  it('registers one provider and its disposer only removes that provider', () => {
    const first = provider('first');
    const disposeFirst = registerAuthProvider(first);
    expect(getAuthProvider()).toBe(first);
    expect(() => registerAuthProvider(provider('second'))).toThrow(
      '[auth] provider already registered: first',
    );

    disposeFirst();
    expect(getAuthProvider()).toBeNull();

    const second = provider('second');
    const disposeSecond = registerAuthProvider(second);
    disposeFirst();
    expect(getAuthProvider()).toBe(second);
    disposeSecond();
  });
});
