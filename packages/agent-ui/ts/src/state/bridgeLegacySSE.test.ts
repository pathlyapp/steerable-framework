/**
 * Tests for `bridgeLegacySSE`. The bridge is the union of the deeppath cloud
 * legacy normaliser and the deeppath-agent local-backend normaliser; we cover
 * the canonical shapes for each profile.
 */

import { describe, expect, it } from 'vitest';
import { bridgeLegacySSE } from './bridgeLegacySSE';

describe('bridgeLegacySSE', () => {
  describe('canonical SSEEvent', () => {
    it('passes through known typed events untouched', () => {
      const ev = bridgeLegacySSE({ type: 'content', content: 'hi' });
      expect(ev).toEqual({ type: 'content', content: 'hi' });
    });

    it('routes tool_call payloads as-is for useChatStream to unpack', () => {
      const ev = bridgeLegacySSE({
        type: 'tool_call',
        payload: { id: 'c1', name: 'get_weather', arguments: {} },
      });
      expect(ev?.type).toBe('tool_call');
      expect(ev?.payload).toEqual({ id: 'c1', name: 'get_weather', arguments: {} });
    });
  });

  describe('deeppath cloud legacy', () => {
    it('maps bare `{content}` deltas to type:content', () => {
      expect(bridgeLegacySSE({ content: 'Hello' })).toEqual({
        type: 'content',
        content: 'Hello',
      });
    });

    it('maps bare `{error}` to type:error', () => {
      expect(bridgeLegacySSE({ error: 'boom' })).toEqual({
        type: 'error',
        message: 'boom',
      });
    });

    it('coerces bare strings (rare legacy path)', () => {
      expect(bridgeLegacySSE('hi')).toEqual({ type: 'content', content: 'hi' });
    });

    it('drops empty bare strings', () => {
      expect(bridgeLegacySSE('')).toBeNull();
    });
  });

  describe('deeppath-agent local backend', () => {
    it('completion:completed → done', () => {
      expect(
        bridgeLegacySSE({ type: 'completion', status: 'completed' }),
      ).toEqual({ type: 'done', payload: { type: 'completion', status: 'completed' } });
    });

    it('completion:failed → done', () => {
      const ev = bridgeLegacySSE({ type: 'completion', status: 'failed' });
      expect(ev?.type).toBe('done');
    });

    it('completion:budget_exhausted → budget_exhausted', () => {
      const ev = bridgeLegacySSE({
        type: 'completion',
        status: 'budget_exhausted',
        reason: 'token limit',
      });
      expect(ev).toEqual({ type: 'budget_exhausted', message: 'token limit' });
    });

    it('completion:executing → agent/round_end', () => {
      const ev = bridgeLegacySSE({ type: 'completion', status: 'executing' });
      expect(ev?.type).toBe('agent');
      expect(ev?.event).toBe('round_end');
    });

    it('user_message is suppressed (echo from the backend)', () => {
      expect(
        bridgeLegacySSE({ type: 'user_message', message: { content: 'hi' } }),
      ).toBeNull();
    });

    it('executed_actions → agent/executed_actions', () => {
      const ev = bridgeLegacySSE({ type: 'executed_actions', actions: [] });
      expect(ev?.type).toBe('agent');
      expect(ev?.event).toBe('executed_actions');
    });

    it('message_id → agent/message_id', () => {
      const ev = bridgeLegacySSE({ type: 'message_id', messageId: 'm1' });
      expect(ev?.type).toBe('agent');
      expect(ev?.event).toBe('message_id');
    });

    it('typed error envelope (data.type === "error")', () => {
      const ev = bridgeLegacySSE({ type: 'error', message: 'kaboom' });
      expect(ev).toEqual({ type: 'error', message: 'kaboom' });
    });
  });

  describe('event: name overrides', () => {
    it('event=error wins over the inner payload type', () => {
      const ev = bridgeLegacySSE({ message: 'transport-level error' }, 'error');
      expect(ev).toEqual({ type: 'error', message: 'transport-level error' });
    });
  });

  describe('unknown envelope passthrough', () => {
    it('repackages unknown `type` as agent/<type> by default', () => {
      const ev = bridgeLegacySSE({ type: 'stage-complete', stage: 'planning' });
      expect(ev?.type).toBe('agent');
      expect(ev?.event).toBe('stage-complete');
      expect(ev?.payload).toEqual({ type: 'stage-complete', stage: 'planning' });
    });

    it('respects passthroughUnknownAsAgent=false', () => {
      const ev = bridgeLegacySSE(
        { type: 'wholly-unknown' },
        undefined,
        { passthroughUnknownAsAgent: false, profile: 'deeppathCloud' },
      );
      expect(ev).toBeNull();
    });
  });

  describe('null inputs', () => {
    it('returns null for non-object, non-string inputs', () => {
      expect(bridgeLegacySSE(null)).toBeNull();
      expect(bridgeLegacySSE(undefined)).toBeNull();
      expect(bridgeLegacySSE(42)).toBeNull();
    });
  });
});
