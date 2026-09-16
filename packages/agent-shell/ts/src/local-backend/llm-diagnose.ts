/**
 * W-llm-diagnose: LLM link diagnosis orchestration.
 *
 * The settings page's "诊断" button runs a battery of probes against the
 * configured provider: DNS resolution, TCP connect, TLS handshake, HTTP
 * GET /models, and a minimal chat completion. Each probe is timed and
 * classified so the UI can render a per-step verdict.
 *
 * The diagnosis runs in the host process (not the sidecar) because it
 * needs to observe the *host's* network path — the sidecar's sandboxed
 * view would hide proxy misconfigurations that the user needs to see.
 */

import { lookup } from 'node:dns/promises';
import { connect } from 'node:net';
import { request as httpsRequest } from 'node:https';
import { request as httpRequest } from 'node:http';
import { collectAmbientProxyEndpoints } from '../sidecar/proxy-detect.js';
import { egressFailureHint } from '../sidecar/egress-hint.js';

export interface DiagnoseStep {
  name: string;
  ok: boolean;
  durationMs: number;
  detail: string;
}

export interface DiagnoseResult {
  ok: boolean;
  steps: DiagnoseStep[];
  /** Ambient proxy endpoints detected on the host (env + system). */
  ambientProxies: string[];
  /** User-facing hint when a step fails, translated from the raw error. */
  hint: string | null;
}

/**
 * Run the full diagnosis battery against `baseUrl` with `apiKey`.
 * `model` is used for the chat-completion probe; when omitted the
 * battery stops after the HTTP probe.
 */
export async function diagnoseLlmConnection(options: {
  baseUrl: string;
  apiKey?: string;
  model?: string;
  timeoutMs?: number;
}): Promise<DiagnoseResult> {
  const { baseUrl, apiKey, model, timeoutMs = 15_000 } = options;
  const steps: DiagnoseStep[] = [];
  const ambientProxies = await collectAmbientProxyEndpoints();

  let url: URL;
  try {
    url = new URL(baseUrl);
  } catch {
    return {
      ok: false,
      steps: [
        {
          name: 'parse-url',
          ok: false,
          durationMs: 0,
          detail: `无法解析 baseUrl: ${baseUrl}`,
        },
      ],
      ambientProxies,
      hint: 'baseUrl 格式不正确，请检查是否包含协议（http:// 或 https://）。',
    };
  }

  const host = url.hostname;
  const port = url.port ? Number(url.port) : url.protocol === 'https:' ? 443 : 80;
  const isHttps = url.protocol === 'https:';

  // Step 1: DNS resolution
  const dnsStart = Date.now();
  try {
    const addresses = await lookup(host, { all: true });
    steps.push({
      name: 'dns',
      ok: true,
      durationMs: Date.now() - dnsStart,
      detail: `解析到 ${addresses.length} 个地址: ${addresses.map((a) => a.address).join(', ')}`,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    steps.push({
      name: 'dns',
      ok: false,
      durationMs: Date.now() - dnsStart,
      detail: `DNS 解析失败: ${message}`,
    });
    return {
      ok: false,
      steps,
      ambientProxies,
      hint: '域名解析失败，请检查 baseUrl 域名是否正确，或检查 DNS 配置。',
    };
  }

  // Step 2: TCP connect
  const tcpStart = Date.now();
  try {
    await new Promise<void>((resolve, reject) => {
      const socket = connect(port, host, () => {
        socket.destroy();
        resolve();
      });
      socket.once('error', reject);
      socket.setTimeout(timeoutMs, () => {
        socket.destroy();
        reject(new Error('ETIMEDOUT'));
      });
    });
    steps.push({
      name: 'tcp',
      ok: true,
      durationMs: Date.now() - tcpStart,
      detail: `TCP 连接 ${host}:${port} 成功`,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    steps.push({
      name: 'tcp',
      ok: false,
      durationMs: Date.now() - tcpStart,
      detail: `TCP 连接失败: ${message}`,
    });
    return {
      ok: false,
      steps,
      ambientProxies,
      hint: egressFailureHint(message) ?? '无法连接到模型服务，请检查网络或防火墙配置。',
    };
  }

  // Step 3: TLS handshake (https only)
  if (isHttps) {
    const tlsStart = Date.now();
    try {
      await new Promise<void>((resolve, reject) => {
        const req = httpsRequest(
          {
            host,
            port,
            method: 'HEAD',
            path: '/',
            timeout: timeoutMs,
            rejectUnauthorized: false, // diagnosis only — we want to see the cert, not enforce it
          },
          (res) => {
            res.resume();
            resolve();
          },
        );
        req.once('error', reject);
        req.once('timeout', () => {
          req.destroy();
          reject(new Error('ETIMEDOUT'));
        });
        req.end();
      });
      steps.push({
        name: 'tls',
        ok: true,
        durationMs: Date.now() - tlsStart,
        detail: 'TLS 握手成功',
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      steps.push({
        name: 'tls',
        ok: false,
        durationMs: Date.now() - tlsStart,
        detail: `TLS 握手失败: ${message}`,
      });
      return {
        ok: false,
        steps,
        ambientProxies,
        hint: 'TLS 握手失败，可能是证书问题或代理拦截。请检查系统代理设置，或尝试 HTTP 直连（如果服务支持）。',
      };
    }
  }

  // Step 4: HTTP GET /models
  const httpStart = Date.now();
  const modelsPath = url.pathname.replace(/\/$/, '') + '/models';
  try {
    const status = await new Promise<number>((resolve, reject) => {
      const requestFn = isHttps ? httpsRequest : httpRequest;
      const req = requestFn(
        {
          host,
          port,
          method: 'GET',
          path: modelsPath,
          timeout: timeoutMs,
          rejectUnauthorized: false,
          headers: {
            ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
            Accept: 'application/json',
          },
        },
        (res) => {
          res.resume();
          resolve(res.statusCode ?? 0);
        },
      );
      req.once('error', reject);
      req.once('timeout', () => {
        req.destroy();
        reject(new Error('ETIMEDOUT'));
      });
      req.end();
    });
    const ok = status >= 200 && status < 500; // 4xx means the endpoint is reachable but auth/model is wrong
    steps.push({
      name: 'http-models',
      ok,
      durationMs: Date.now() - httpStart,
      detail: `GET ${modelsPath} → HTTP ${status}`,
    });
    if (!ok) {
      return {
        ok: false,
        steps,
        ambientProxies,
        hint: `模型服务返回 HTTP ${status}，请检查 baseUrl 与 API Key 配置。`,
      };
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    steps.push({
      name: 'http-models',
      ok: false,
      durationMs: Date.now() - httpStart,
      detail: `GET ${modelsPath} 失败: ${message}`,
    });
    return {
      ok: false,
      steps,
      ambientProxies,
      hint: egressFailureHint(message) ?? '无法访问模型服务，请检查网络或代理配置。',
    };
  }

  // Step 5: chat completion (optional)
  if (model) {
    const chatStart = Date.now();
    const chatPath = url.pathname.replace(/\/$/, '') + '/chat/completions';
    try {
      const status = await new Promise<number>((resolve, reject) => {
        const requestFn = isHttps ? httpsRequest : httpRequest;
        const req = requestFn(
          {
            host,
            port,
            method: 'POST',
            path: chatPath,
            timeout: timeoutMs,
            rejectUnauthorized: false,
            headers: {
              'Content-Type': 'application/json',
              ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
            },
          },
          (res) => {
            res.resume();
            resolve(res.statusCode ?? 0);
          },
        );
        req.once('error', reject);
        req.once('timeout', () => {
          req.destroy();
          reject(new Error('ETIMEDOUT'));
        });
        req.write(
          JSON.stringify({
            model,
            messages: [{ role: 'user', content: 'ping' }],
            max_tokens: 1,
          }),
        );
        req.end();
      });
      const ok = status >= 200 && status < 500;
      steps.push({
        name: 'chat-completion',
        ok,
        durationMs: Date.now() - chatStart,
        detail: `POST ${chatPath} → HTTP ${status}`,
      });
      if (!ok) {
        return {
          ok: false,
          steps,
          ambientProxies,
          hint: `模型服务返回 HTTP ${status}，请检查模型名与 API Key 配置。`,
        };
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      steps.push({
        name: 'chat-completion',
        ok: false,
        durationMs: Date.now() - chatStart,
        detail: `POST ${chatPath} 失败: ${message}`,
      });
      return {
        ok: false,
        steps,
        ambientProxies,
        hint: egressFailureHint(message) ?? '无法调用模型服务，请检查网络或代理配置。',
      };
    }
  }

  return { ok: true, steps, ambientProxies, hint: null };
}
