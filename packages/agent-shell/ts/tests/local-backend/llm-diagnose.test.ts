import { describe, expect, it } from 'vitest';
import { diagnoseLlmConnection } from '../../src/local-backend/llm-diagnose.js';

describe('diagnoseLlmConnection', () => {
  it('fails fast on an unparseable baseUrl', async () => {
    const result = await diagnoseLlmConnection({ baseUrl: 'not-a-url' });
    expect(result.ok).toBe(false);
    expect(result.steps).toHaveLength(1);
    expect(result.steps[0].name).toBe('parse-url');
    expect(result.hint).toContain('baseUrl 格式不正确');
  });

  it('reports DNS failure for a non-existent host', async () => {
    const result = await diagnoseLlmConnection({
      baseUrl: 'https://this-host-does-not-exist-12345.example.com',
      timeoutMs: 5_000,
    });
    expect(result.ok).toBe(false);
    expect(result.steps[0].name).toBe('dns');
    expect(result.steps[0].ok).toBe(false);
    expect(result.hint).toContain('域名解析失败');
  });

  it('reports TCP failure for a refused connection', async () => {
    const result = await diagnoseLlmConnection({
      baseUrl: 'http://127.0.0.1:1',
      timeoutMs: 5_000,
    });
    expect(result.ok).toBe(false);
    const tcpStep = result.steps.find((s) => s.name === 'tcp');
    expect(tcpStep).toBeDefined();
    expect(tcpStep?.ok).toBe(false);
    expect(result.hint).toContain('连接被拒绝');
  });

  it('succeeds against a local HTTP server', async () => {
    // Spin up a minimal HTTP server on an ephemeral port.
    const { createServer } = await import('node:http');
    const server = createServer((req, res) => {
      if (req.url === '/models') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end('{"data":[]}');
      } else if (req.url === '/chat/completions') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end('{"choices":[]}');
      } else {
        res.writeHead(404);
        res.end();
      }
    });
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    const address = server.address();
    const port = typeof address === 'object' && address ? address.port : 0;

    try {
      const result = await diagnoseLlmConnection({
        baseUrl: `http://127.0.0.1:${port}`,
        model: 'test-model',
        timeoutMs: 5_000,
      });
      expect(result.ok).toBe(true);
      expect(result.steps.map((s) => s.name)).toEqual([
        'dns',
        'tcp',
        'http-models',
        'chat-completion',
      ]);
      expect(result.hint).toBeNull();
    } finally {
      server.close();
    }
  });
});
