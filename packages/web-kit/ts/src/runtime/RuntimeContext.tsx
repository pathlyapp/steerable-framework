import React, { createContext, useContext } from 'react';
import type { SSEEvent } from '@steerable/agent-protocol';

export type RuntimeMode = 'remote' | 'local' | 'custom';

export interface LocalBackendStreamEvent {
  type: 'data' | 'end' | 'error';
  chunk?: string;
  parsed?: SSEEvent;
  error?: string;
}

export interface RuntimeAdapter {
  request<T = any>(method: string, path: string, body?: unknown): Promise<T>;
  stream(method: string, path: string, body?: unknown): AsyncIterable<SSEEvent>;
}

export type RuntimeConfig =
  | { mode: 'remote'; apiBaseUrl: string; getAuthToken?: () => string | null }
  | { mode: 'local'; bridge?: any }
  | { mode: 'custom'; adapter: RuntimeAdapter };

const RuntimeContext = createContext<RuntimeAdapter | null>(null);

// Standard HTTP/SSE parser helper
async function* parseSseStream(response: Response): AsyncIterable<SSEEvent> {
  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error('Response body is not readable');
  }

  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        if (trimmed.startsWith('data:')) {
          const rawData = trimmed.slice(5).trim();
          if (rawData === '[DONE]') continue;
          try {
            const parsed = JSON.parse(rawData) as SSEEvent;
            yield parsed;
          } catch (err) {
            console.warn('Failed to parse SSE line data:', rawData, err);
          }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export class RemoteRuntimeAdapter implements RuntimeAdapter {
  constructor(
    private readonly apiBaseUrl: string,
    private readonly getAuthToken?: () => string | null
  ) {}

  private getHeaders(): Record<string, string> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };
    if (this.getAuthToken) {
      const token = this.getAuthToken();
      if (token) {
        headers['Authorization'] = `Bearer ${token}`;
      }
    }
    return headers;
  }

  async request<T = any>(method: string, path: string, body?: unknown): Promise<T> {
    const url = `${this.apiBaseUrl}${path.startsWith('/') ? path : '/' + path}`;
    const response = await fetch(url, {
      method,
      headers: this.getHeaders(),
      body: body ? JSON.stringify(body) : undefined,
    });

    if (!response.ok) {
      const errText = await response.text();
      throw new Error(`HTTP Error ${response.status}: ${errText || response.statusText}`);
    }

    return (await response.json()) as T;
  }

  async *stream(method: string, path: string, body?: unknown): AsyncIterable<SSEEvent> {
    const url = `${this.apiBaseUrl}${path.startsWith('/') ? path : '/' + path}`;
    const response = await fetch(url, {
      method,
      headers: this.getHeaders(),
      body: body ? JSON.stringify(body) : undefined,
    });

    if (!response.ok) {
      const errText = await response.text();
      throw new Error(`HTTP Stream Error ${response.status}: ${errText || response.statusText}`);
    }

    yield* parseSseStream(response);
  }
}

export class LocalRuntimeAdapter implements RuntimeAdapter {
  private readonly bridge: any;

  constructor(bridgeOverride?: any) {
    this.bridge = bridgeOverride || (typeof window !== 'undefined' ? (window as any).electron?.localBackend : null);
  }

  private getBridge() {
    if (!this.bridge) {
      throw new Error('Local Electron bridge not available');
    }
    return this.bridge;
  }

  async request<T = any>(method: string, path: string, body?: unknown): Promise<T> {
    return await this.getBridge().request({ method, path, body }) as T;
  }

  async *stream(method: string, path: string, body?: unknown): AsyncIterable<SSEEvent> {
    const bridge = this.getBridge();
    const eventQueue: SSEEvent[] = [];
    let isDone = false;
    let streamError: Error | null = null;
    let resolveNext: (() => void) | null = null;

    const streamId = await bridge.startStream(
      { method, path, body },
      (event: LocalBackendStreamEvent) => {
        if (event.type === 'data' && event.parsed) {
          eventQueue.push(event.parsed);
        } else if (event.type === 'end') {
          isDone = true;
        } else if (event.type === 'error') {
          streamError = new Error(event.error || 'Local stream error');
          isDone = true;
        }
        if (resolveNext) {
          resolveNext();
          resolveNext = null;
        }
      }
    );

    if (!streamId) {
      throw new Error('Failed to start local stream');
    }

    try {
      while (true) {
        if (eventQueue.length > 0) {
          yield eventQueue.shift()!;
          continue;
        }
        if (isDone) {
          if (streamError) throw streamError;
          break;
        }
        await new Promise<void>((resolve) => {
          resolveNext = resolve;
        });
      }
    } finally {
      if (!isDone) {
        bridge.cancelStream(streamId);
      }
    }
  }
}

export const SteerableRuntimeProvider: React.FC<{
  config: RuntimeConfig;
  children: React.ReactNode;
}> = ({ config, children }) => {
  const adapter = React.useMemo<RuntimeAdapter>(() => {
    if (config.mode === 'custom') return config.adapter;
    if (config.mode === 'local') return new LocalRuntimeAdapter(config.bridge);
    return new RemoteRuntimeAdapter(config.apiBaseUrl, config.getAuthToken);
  }, [config]);

  return (
    <RuntimeContext.Provider value={adapter}>
      {children}
    </RuntimeContext.Provider>
  );
};

export function useSteerableRuntime(): RuntimeAdapter {
  const context = useContext(RuntimeContext);
  if (!context) {
    throw new Error('useSteerableRuntime must be used within a SteerableRuntimeProvider');
  }
  return context;
}
