import { describe, expect, it } from 'vitest';
import { egressFailureHint } from '../../src/sidecar/egress-hint.js';

describe('egressFailureHint', () => {
  it('translates "All connection attempts failed"', () => {
    expect(egressFailureHint('All connection attempts failed')).toContain('沙箱或代理拒绝了连接');
  });

  it('translates 403 Forbidden from the proxy', () => {
    expect(egressFailureHint('HTTP 403 Forbidden')).toContain('被代理拒绝');
  });

  it('translates 502 Bad Gateway', () => {
    expect(egressFailureHint('HTTP 502 Bad Gateway')).toContain('代理无法连接到模型服务');
  });

  it('translates 504 Gateway Timeout', () => {
    expect(egressFailureHint('HTTP 504 Gateway Timeout')).toContain('代理连接模型服务超时');
  });

  it('translates ECONNREFUSED', () => {
    expect(egressFailureHint('connect ECONNREFUSED 127.0.0.1:11434')).toContain('连接被拒绝');
  });

  it('translates ENOTFOUND', () => {
    expect(egressFailureHint('getaddrinfo ENOTFOUND api.example.com')).toContain('域名解析失败');
  });

  it('translates ETIMEDOUT', () => {
    expect(egressFailureHint('connect ETIMEDOUT 10.0.0.1:443')).toContain('连接超时');
  });

  it('returns null for unknown errors', () => {
    expect(egressFailureHint('some random error')).toBeNull();
  });
});
