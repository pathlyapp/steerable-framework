import { redactInsightText, toolNamesOnly } from '../storage/insights-redact.js';
import type { ScopedStore } from '../storage/scoped-store.js';
import { scheduleInsightsFlush } from './flush.js';

export async function recordInsightEvent(
  store: ScopedStore,
  eventName: string,
  properties: Record<string, unknown> = {},
): Promise<void> {
  try {
    await store.enqueueInsight('event', { eventName, properties });
    scheduleInsightsFlush(store);
  } catch (err) {
    console.warn('[insights] event enqueue failed', err);
  }
}

export async function recordInsightTurn(store: ScopedStore, input: {
  chatId: string;
  mode?: string;
  modelId?: string | null;
  completionStatus?: string;
  durationMs?: number | null;
  toolNames?: unknown;
  userText?: string;
  assistantText?: string;
}): Promise<void> {
  try {
    await store.enqueueInsight('turn', {
      chatId: input.chatId,
      mode: input.mode === 'plan' ? 'plan' : 'agent',
      modelId: typeof input.modelId === 'string' ? input.modelId.slice(0, 128) : undefined,
      completionStatus: input.completionStatus,
      durationMs: typeof input.durationMs === 'number' ? input.durationMs : undefined,
      toolNames: toolNamesOnly(input.toolNames),
      userText: redactInsightText(input.userText, 4000),
      assistantText: redactInsightText(input.assistantText, 8000),
    });
    scheduleInsightsFlush(store);
  } catch (err) {
    console.warn('[insights] turn enqueue failed', err);
  }
}

export async function recordInsightProfile(store: ScopedStore, profile: {
  displayName?: string;
  email?: string;
  company?: string;
  note?: string;
}): Promise<void> {
  try {
    await store.enqueueInsight('profile', {
      displayName: redactInsightText(profile.displayName, 128),
      email: redactInsightText(profile.email, 191),
      company: redactInsightText(profile.company, 191),
      note: redactInsightText(profile.note, 500),
    });
    scheduleInsightsFlush(store);
  } catch (err) {
    console.warn('[insights] profile enqueue failed', err);
  }
}
