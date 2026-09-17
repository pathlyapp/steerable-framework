import { describe, expect, it } from 'vitest';
import {
  createAskUserBridge,
  type AskUserPromptRequest,
} from '../../src/sidecar/reverse-ask-user.js';

describe('createAskUserBridge', () => {
  it('exposes a prompt until the renderer answers it', async () => {
    const sent: AskUserPromptRequest[] = [];
    const bridge = createAskUserBridge({
      hasWindow: () => true,
      broadcast: (_channel, prompt) => sent.push(prompt),
    });

    const result = bridge.handler({
      intro: '确认信息',
      questions: [{ id: 'env', text: '目标环境？', options: ['prod'] }],
    });

    expect(bridge.pending()).toEqual(sent);
    expect(
      bridge.answer({
        requestId: sent[0].requestId,
        answers: { env: 'prod' },
      }),
    ).toEqual({ ok: true });
    expect(bridge.pending()).toEqual([]);
    await expect(result).resolves.toEqual({ answers: { env: 'prod' } });
  });
});
