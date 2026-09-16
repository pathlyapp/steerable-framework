import { describe, expect, it } from 'vitest';
import { shouldEvictMcpConnection, withMcpTroubleshootHint } from '../src/mcp-executor';

const IDLE_TIMEOUT_MS = 5 * 60 * 1000;

describe('shouldEvictMcpConnection', () => {
  it('does not evict a connection that has been idle for less than the timeout', () => {
    const now = 1_000_000;
    expect(
      shouldEvictMcpConnection({ lastUsed: now - 1000, inflight: 0 }, now, IDLE_TIMEOUT_MS),
    ).toBe(false);
  });

  it('evicts a connection idle for longer than the timeout with nothing in flight', () => {
    const now = 1_000_000;
    expect(
      shouldEvictMcpConnection(
        { lastUsed: now - IDLE_TIMEOUT_MS - 1, inflight: 0 },
        now,
        IDLE_TIMEOUT_MS,
      ),
    ).toBe(true);
  });

  it('never evicts a connection with an in-flight call, no matter how stale lastUsed looks', () => {
    // Regression: `lastUsed` used to be stamped when a call *started*, so a
    // single tool call running longer than IDLE_TIMEOUT_MS (e.g. a slow MCP
    // server) looked idle to the 60s sweep and got closed out from under the
    // in-flight request.
    const now = 1_000_000;
    expect(
      shouldEvictMcpConnection(
        { lastUsed: now - IDLE_TIMEOUT_MS * 10, inflight: 1 },
        now,
        IDLE_TIMEOUT_MS,
      ),
    ).toBe(false);
  });

  it('resumes normal idle eviction once inflight drops back to 0', () => {
    const now = 1_000_000;
    const staleEntry = { lastUsed: now - IDLE_TIMEOUT_MS - 1, inflight: 0 };
    expect(shouldEvictMcpConnection(staleEntry, now, IDLE_TIMEOUT_MS)).toBe(true);
  });
});

describe('withMcpTroubleshootHint', () => {
  it('appends a first-run hint to MCP timeout errors (-32001)', () => {
    // Regression: 2026-07-31 user report — first-ever `npx -y <pkg>` run needs
    // to download the package; the SDK's 60s handshake timeout fired and the
    // UI showed a bare "Request timed out" with no actionable guidance.
    const hinted = withMcpTroubleshootHint('MCP server startup failed: MCP error -32001: Request timed out');
    expect(hinted).toContain('-32001');
    expect(hinted).toContain('预热缓存');
    expect(hinted).toContain('registry.npmmirror.com');
  });

  it('appends the hint to Chinese timeout messages too', () => {
    const hinted = withMcpTroubleshootHint('连接超时');
    expect(hinted).toContain('预热缓存');
  });

  it('leaves non-timeout errors untouched', () => {
    const msg = 'MCP server startup failed: spawn npx ENOENT';
    expect(withMcpTroubleshootHint(msg)).toBe(msg);
  });
});
