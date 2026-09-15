/**
 * 后台任务的推理时间线：和主对话同一套 TurnBlock
 * （reasoning / tools / text），供右侧过程面板渲染。
 *
 * 来源优先级（TaskService.getProcess）：
 *   1. 进程内正在跑的流（live）
 *   2. 任务表 process_json（流过程中回写）
 *   3. sidecar durable history（task:<id> 记录）——重启后仍能回看
 */

import Database from 'better-sqlite3';
import { resolveSidecarStoragePath } from '../sidecar/storage-path.js';
import {
  appendTimelineDelta,
  syncTimelineTools,
  type PersistedTurnBlock,
} from './turn-timeline.js';

export function timelineFromHistoryEntries(entries: unknown[]): PersistedTurnBlock[] {
  const blocks: PersistedTurnBlock[] = [];
  const actions: Array<Record<string, unknown>> = [];

  for (const raw of entries) {
    if (!raw || typeof raw !== 'object') continue;
    const entry = raw as {
      kind?: unknown;
      message?: unknown;
    };
    const kind = typeof entry.kind === 'string' ? entry.kind : '';
    if (
      kind === 'system' ||
      kind === 'user' ||
      kind === 'world_state.snapshot' ||
      kind === 'world_state.patch'
    ) {
      continue;
    }

    const message = asRecord(entry.message);
    if (!message) continue;
    const role = typeof message.role === 'string' ? message.role : kind;

    if (role === 'assistant' || kind === 'assistant') {
      const reasoning = messageReasoning(message);
      if (reasoning) appendTimelineDelta(blocks, 'reasoning', reasoning);
      const text = messageText(message).trim();
      if (text) appendTimelineDelta(blocks, 'text', text);
      const calls = messageToolCalls(message);
      if (calls.length > 0) {
        for (const call of calls) actions.push(call);
        syncTimelineTools(blocks, actions);
      }
      continue;
    }

    if (role === 'tool' || kind === 'tool') {
      const resultText = messageText(message);
      const parsed = parseToolPayload(resultText);
      const name =
        typeof message.name === 'string'
          ? message.name
          : typeof parsed.name === 'string'
            ? parsed.name
            : 'tool';
      const callId =
        (typeof message.tool_call_id === 'string' && message.tool_call_id) ||
        (typeof message.toolCallId === 'string' && message.toolCallId) ||
        null;
      let idx = callId ? actions.findIndex((row) => row.id === callId) : -1;
      if (idx < 0) {
        idx = actions.findIndex((row) => row.tool === name && row.result === undefined);
      }
      const row: Record<string, unknown> = {
        ...(idx >= 0 ? actions[idx] : { tool: name }),
        tool: name,
        result: parsed.data ?? parsed,
        success: parsed.success !== false && parsed.error == null,
        error: typeof parsed.error === 'string' ? parsed.error : undefined,
        threw: false,
      };
      if (idx >= 0) actions[idx] = row;
      else actions.push(row);
      syncTimelineTools(blocks, actions);
    }
  }

  return blocks;
}

export function readSidecarHistoryEntries(recordId: string): unknown[] {
  const dbPath = resolveSidecarStoragePath();
  let db: Database.Database;
  try {
    db = new Database(dbPath, { readonly: true, fileMustExist: true });
  } catch {
    return [];
  }
  try {
    const rows = db
      .prepare('SELECT data FROM history WHERE record_id = ? ORDER BY seq ASC')
      .all(recordId) as Array<{ data: string }>;
    const out: unknown[] = [];
    for (const row of rows) {
      try {
        out.push(JSON.parse(row.data) as unknown);
      } catch {
        /* 坏行跳过 */
      }
    }
    return out;
  } catch {
    return [];
  } finally {
    db.close();
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : null;
}

function messageText(message: Record<string, unknown>): string {
  const content = message.content;
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  let text = '';
  for (const part of content) {
    const rec = asRecord(part);
    if (!rec) continue;
    if (typeof rec.text === 'string') text += rec.text;
  }
  return text;
}

function messageReasoning(message: Record<string, unknown>): string {
  for (const key of ['reasoning', 'reasoning_content', 'reasoningContent']) {
    const value = message[key];
    if (typeof value === 'string' && value.trim()) return value;
    const rec = asRecord(value);
    if (rec && typeof rec.text === 'string' && rec.text.trim()) return rec.text;
  }
  return '';
}

function messageToolCalls(message: Record<string, unknown>): Array<Record<string, unknown>> {
  const raw = message.tool_calls ?? message.toolCalls;
  if (!Array.isArray(raw)) return [];
  const calls: Array<Record<string, unknown>> = [];
  for (const item of raw) {
    const rec = asRecord(item);
    if (!rec) continue;
    const name =
      typeof rec.name === 'string'
        ? rec.name
        : typeof asRecord(rec.function)?.name === 'string'
          ? String(asRecord(rec.function)!.name)
          : 'tool';
    const args =
      rec.arguments ??
      asRecord(rec.function)?.arguments ??
      {};
    calls.push({
      id: typeof rec.id === 'string' ? rec.id : undefined,
      tool: name,
      arguments: typeof args === 'string' ? tryJson(args) ?? args : args,
      threw: false,
    });
  }
  return calls;
}

function parseToolPayload(text: string): Record<string, unknown> {
  const parsed = tryJson(text);
  return asRecord(parsed) ?? { data: text };
}

function tryJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}
