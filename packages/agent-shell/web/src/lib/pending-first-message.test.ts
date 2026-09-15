import { describe, expect, it } from 'vitest';
import {
  setPendingFirstMessage,
  takePendingFirstMessage,
} from './pending-first-message';

describe('pending-first-message（首页首条消息接力）', () => {
  it('按 chatId 取出一次后清空，错配的 chatId 不消费', () => {
    setPendingFirstMessage({ chatId: 'chat-a', content: '秋天到了，随便聊一句吧。' });
    expect(takePendingFirstMessage('chat-b')).toBeNull();
    expect(takePendingFirstMessage('chat-a')).toEqual({
      chatId: 'chat-a',
      content: '秋天到了，随便聊一句吧。',
    });
    expect(takePendingFirstMessage('chat-a')).toBeNull();
  });
});
