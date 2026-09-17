import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AskUserPromptRequest } from '@/lib/electron-bridge';
import { AskUserPromptProvider } from './AskUserPromptProvider';
import { ChatInput } from './ChatInput';

afterEach(() => {
  cleanup();
  delete (window as { electron?: unknown }).electron;
});

function installBridge(pendingRequests: AskUserPromptRequest[] = []) {
  const listeners = new Set<(request: AskUserPromptRequest) => void>();
  const answer = vi.fn().mockResolvedValue(undefined);
  const pending = vi.fn().mockResolvedValue(pendingRequests);
  (window as { electron?: unknown }).electron = {
    askUser: {
      onRequest: (callback: (request: AskUserPromptRequest) => void) => {
        listeners.add(callback);
        return () => listeners.delete(callback);
      },
      answer,
      pending,
    },
  };
  return {
    answer,
    pending,
    emit: (request: AskUserPromptRequest) => {
      for (const callback of listeners) callback(request);
    },
  };
}

const REQUEST: AskUserPromptRequest = {
  requestId: 'req-1',
  intro: '部署前确认',
  questions: [
    {
      id: 'env',
      text: '目标环境？',
      type: 'select',
      options: ['staging', 'prod'],
      multiSelect: false,
    },
  ],
};

function renderComposer() {
  render(
    <AskUserPromptProvider>
      <ChatInput value="" onChange={vi.fn()} onSubmit={vi.fn()} />
    </AskUserPromptProvider>,
  );
}

describe('AskUserPromptProvider', () => {
  it('restores a pending prompt after the renderer remounts', async () => {
    const bridge = installBridge([REQUEST]);
    renderComposer();

    expect(await screen.findByTestId('ask-user-composer')).toBeTruthy();
    expect(screen.queryByTestId('chat-composer')).toBeNull();
    expect(bridge.pending).toHaveBeenCalledOnce();
  });

  it('replaces the normal composer while a structured prompt is active', () => {
    const bridge = installBridge();
    renderComposer();
    expect(screen.getByTestId('chat-composer')).toBeTruthy();

    act(() => bridge.emit(REQUEST));

    expect(screen.queryByTestId('chat-composer')).toBeNull();
    expect(screen.getByTestId('ask-user-composer')).toBeTruthy();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText('部署前确认')).toBeTruthy();
  });

  it('answers the request and restores the normal composer', () => {
    const bridge = installBridge();
    renderComposer();
    act(() => bridge.emit(REQUEST));

    fireEvent.click(screen.getByRole('radio', { name: /staging/ }));

    expect(bridge.answer).toHaveBeenCalledWith({
      requestId: 'req-1',
      answers: { env: 'staging' },
    });
    expect(screen.getByTestId('chat-composer')).toBeTruthy();
    expect(screen.queryByTestId('ask-user-composer')).toBeNull();
  });

  it('keeps queued prompts in FIFO order inside the composer', () => {
    const bridge = installBridge();
    renderComposer();
    act(() => {
      bridge.emit(REQUEST);
      bridge.emit({ ...REQUEST, requestId: 'req-2', intro: '第二组问题' });
    });

    expect(screen.getByText(/还有 1 组问题待回答/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /交给我决定/ }));

    expect(screen.getByText('第二组问题')).toBeTruthy();
    expect(bridge.answer).toHaveBeenCalledWith({
      requestId: 'req-1',
      answers: {},
    });
  });
});
