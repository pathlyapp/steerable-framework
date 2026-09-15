/**
 * W-egress-hint: translate cryptic egress failures into user-facing hints.
 *
 * The sidecar's LLM clients (httpx, `trust_env=True`) honor ambient proxy
 * configuration. When the sandbox or egress proxy denies a connection, the
 * raw error is often a generic "All connection attempts failed" or a 403
 * from the proxy — useless to the user. This module maps known failure
 * patterns to actionable Chinese hints.
 */

/**
 * Translate a raw egress/LLM error into a user-facing hint. Returns null
 * when the error does not match a known egress failure pattern (caller
 * should fall back to the raw message).
 */
export function egressFailureHint(raw: string): string | null {
  const text = raw.toLowerCase();

  // httpx "All connection attempts failed" — the sandbox denied the
  // connection (EPERM) or the proxy refused it. Most common cause: the
  // provider endpoint is not in the allow-list.
  if (text.includes('all connection attempts failed')) {
    return '无法连接模型服务：沙箱或代理拒绝了连接。请检查 baseUrl 是否已加入允许列表，或尝试关闭系统代理后重试。';
  }

  // 403 from the egress proxy — the host is not in the per-host allow-list.
  if (text.includes('403') && (text.includes('forbidden') || text.includes('denied'))) {
    return '模型服务被代理拒绝（403）。请确认 baseUrl 已加入允许列表，或联系管理员放行该域名。';
  }

  // 502 Bad Gateway — the proxy itself failed to reach the upstream.
  if (text.includes('502') && text.includes('bad gateway')) {
    return '代理无法连接到模型服务（502）。请检查网络连接或代理配置，或尝试直连（关闭代理）后重试。';
  }

  // 504 Gateway Timeout — upstream timed out through the proxy.
  if (text.includes('504') && text.includes('gateway timeout')) {
    return '代理连接模型服务超时（504）。请检查网络连接或代理配置，或尝试直连（关闭代理）后重试。';
  }

  // ECONNREFUSED / ENOTFOUND / ETIMEDOUT — direct connection failures.
  if (text.includes('econnrefused')) {
    return '无法连接模型服务：连接被拒绝。请检查 baseUrl 是否正确，或确认服务已启动。';
  }
  if (text.includes('enotfound')) {
    return '无法连接模型服务：域名解析失败。请检查 baseUrl 域名是否正确，或检查 DNS 配置。';
  }
  if (text.includes('etimedout')) {
    return '无法连接模型服务：连接超时。请检查网络连接，或确认防火墙/代理未拦截该域名。';
  }

  return null;
}
