#!/usr/bin/env node
/**
 * 零依赖演示 MCP 服务（stdio 传输）。
 *
 * 用途：给"外部 MCP 服务导入"功能提供一个 100% 本地、秒启动的测试目标。
 * 不经过 npx/uvx 下载（2026-07-31 用户实测 npm 官方源慢导致 60s 握手超时），
 * 任何装有 Node.js 的机器上 `node scripts/demo-mcp-server.mjs` 即可运行。
 *
 * 导入配置（Claude Desktop 格式）：
 *   { "mcpServers": { "demo-local": { "command": "node",
 *     "args": ["<本仓库绝对路径>/scripts/demo-mcp-server.mjs"] } } }
 *
 * 协议要点（MCP stdio = 每行一个 JSON-RPC 2.0 消息）：
 *   initialize → 回显 protocolVersion，声明 tools 能力
 *   notifications/initialized → 通知，不回复
 *   tools/list → 返回工具清单
 *   tools/call → 执行并返回 { content: [{ type: 'text', text }] }
 *   ping → 回复空 result
 *
 * 注意：stdout 只能写协议消息，任何调试输出一律走 stderr，
 * 否则会破坏 NDJSON 帧导致客户端解析失败。
 */

import { EOL, platform, release } from 'node:os';

const PROTOCOL_VERSION_FALLBACK = '2024-11-05';

const TOOLS = [
  {
    name: 'echo',
    description: '原样回显一段文本，用于验证 MCP 调用链路是否通畅',
    inputSchema: {
      type: 'object',
      properties: {
        message: { type: 'string', description: '要回显的文本' },
      },
      required: ['message'],
    },
  },
  {
    name: 'add',
    description: '两个数字相加，返回和。等价于 server-everything 的 get-sum',
    inputSchema: {
      type: 'object',
      properties: {
        a: { type: 'number', description: '第一个数' },
        b: { type: 'number', description: '第二个数' },
      },
      required: ['a', 'b'],
    },
  },
  {
    name: 'get_current_time',
    description: '返回服务器当前时间（ISO 与本地格式）',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'get_server_info',
    description: '返回演示服务的运行环境信息（Node 版本、平台等）',
    inputSchema: { type: 'object', properties: {} },
  },
];

function textResult(text) {
  return { content: [{ type: 'text', text }] };
}

function callTool(name, args = {}) {
  switch (name) {
    case 'echo':
      return textResult(`Echo: ${String(args.message ?? '')}`);
    case 'add': {
      const a = Number(args.a);
      const b = Number(args.b);
      if (Number.isNaN(a) || Number.isNaN(b)) {
        return { ...textResult('参数 a、b 必须是数字'), isError: true };
      }
      return textResult(String(a + b));
    }
    case 'get_current_time': {
      const now = new Date();
      return textResult(`ISO: ${now.toISOString()}\nLocal: ${now.toString()}`);
    }
    case 'get_server_info':
      return textResult(
        [
          `server: demo-local-mcp v1.0.0 (zero-dependency)`,
          `node: ${process.version}`,
          `platform: ${platform()} ${release()}`,
          `pid: ${process.pid}`,
        ].join(EOL),
      );
    default:
      return { ...textResult(`未知工具: ${name}`), isError: true };
  }
}

function writeMessage(payload) {
  process.stdout.write(JSON.stringify(payload) + '\n');
}

function handleMessage(msg) {
  // 通知没有 id，按协议不回包
  if (msg.id === undefined || msg.id === null) return;

  const send = (result) => writeMessage({ jsonrpc: '2.0', id: msg.id, result });
  const sendError = (code, message) =>
    writeMessage({ jsonrpc: '2.0', id: msg.id, error: { code, message } });

  if (msg.method === 'initialize') {
    send({
      protocolVersion: msg.params?.protocolVersion ?? PROTOCOL_VERSION_FALLBACK,
      capabilities: { tools: {} },
      serverInfo: { name: 'demo-local-mcp', version: '1.0.0' },
    });
    return;
  }
  if (msg.method === 'ping') {
    send({});
    return;
  }
  if (msg.method === 'tools/list') {
    send({ tools: TOOLS });
    return;
  }
  if (msg.method === 'tools/call') {
    send(callTool(msg.params?.name, msg.params?.arguments ?? {}));
    return;
  }
  sendError(-32601, `Method not found: ${msg.method}`);
}

let buffer = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buffer += chunk;
  for (;;) {
    const idx = buffer.indexOf('\n');
    if (idx === -1) break;
    const line = buffer.slice(0, idx).replace(/\r$/, '');
    buffer = buffer.slice(idx + 1);
    if (!line.trim()) continue;

    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      process.stderr.write(`[demo-mcp] 无法解析的消息: ${line.slice(0, 200)}\n`);
      continue;
    }
    handleMessage(msg);
  }
});

process.stdin.on('end', () => process.exit(0));
process.on('SIGTERM', () => process.exit(0));
process.on('SIGINT', () => process.exit(0));

process.stderr.write('[demo-mcp] ready (stdio, zero-dependency)\n');
