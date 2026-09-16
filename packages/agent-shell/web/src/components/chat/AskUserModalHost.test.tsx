/**
 * AskUserModalHost 的接线契约（W8）：
 *   - `ask-user:request` 广播到达时渲染分步菜单卡片（intro + 问题菜单）；
 *   - 单选点击/Enter 直接提交并进入下一题；文本题 Enter 进入下一题；
 *   - 「其他 / 自定义」展开输入框，输入直接作为该题答案（不产生新问题）；
 *   - 提交把 requestId + 答案映射经 bridge.askUser.answer 送回主进程；
 *   - 「交给我决定」应答空映射（fail-open：模型自行推进）；
 *   - 应答后卡片关闭，队列中的下一组问题顶上来。
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AskUserPromptRequest } from '@/lib/electron-bridge';
import { AskUserModalHost } from './AskUserModalHost';

afterEach(() => {
  cleanup();
  delete (window as { electron?: unknown }).electron;
});

function installBridge() {
  const listeners = new Set<(request: AskUserPromptRequest) => void>();
  const answer = vi.fn().mockResolvedValue(undefined);
  (window as { electron?: unknown }).electron = {
    askUser: {
      onRequest: (callback: (request: AskUserPromptRequest) => void) => {
        listeners.add(callback);
        return () => listeners.delete(callback);
      },
      answer,
    },
  };
  return {
    answer,
    emit: (request: AskUserPromptRequest) => {
      for (const callback of Array.from(listeners)) callback(request);
    },
  };
}

const REQUEST: AskUserPromptRequest = {
  requestId: 'req-1',
  intro: '部署前确认',
  questions: [
    { id: 'env', text: '目标环境？', type: 'select', options: ['staging', 'prod'], multiSelect: false },
  ],
};

const REQUEST_TWO: AskUserPromptRequest = {
  requestId: 'req-two',
  intro: '部署前确认',
  questions: [
    { id: 'env', text: '目标环境？', type: 'select', options: ['staging', 'prod'] },
    { id: 'note', text: '备注', type: 'text' },
  ],
};

describe('AskUserModalHost（W8）', () => {
  it('无请求时不渲染；请求到达后渲染问题卡片', () => {
    const bridge = installBridge();
    render(<AskUserModalHost />);
    expect(screen.queryByRole('dialog')).toBeNull();

    act(() => bridge.emit(REQUEST));
    expect(screen.getByRole('dialog', { name: 'Agent 提问' })).toBeTruthy();
    expect(screen.getByText('部署前确认')).toBeTruthy();
    expect(screen.getByText(/目标环境？/)).toBeTruthy();
  });

  it('单选点击选项即提交：answer 收到 requestId 与答案映射，卡片关闭', () => {
    const bridge = installBridge();
    render(<AskUserModalHost />);
    act(() => bridge.emit(REQUEST));

    fireEvent.click(screen.getByRole('radio', { name: /staging/ }));

    expect(bridge.answer).toHaveBeenCalledWith({
      requestId: 'req-1',
      answers: { env: 'staging' },
    });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('「交给我决定」应答空映射', () => {
    const bridge = installBridge();
    render(<AskUserModalHost />);
    act(() => bridge.emit(REQUEST));

    fireEvent.click(screen.getByRole('button', { name: /交给我决定/ }));
    expect(bridge.answer).toHaveBeenCalledWith({ requestId: 'req-1', answers: {} });
  });

  it('FIFO：应答第一组后队列中的第二组顶上来', () => {
    const bridge = installBridge();
    render(<AskUserModalHost />);
    act(() => {
      bridge.emit(REQUEST);
      bridge.emit({ ...REQUEST, requestId: 'req-2', intro: '第二组问题' });
    });
    expect(screen.getByText('部署前确认')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /交给我决定/ }));
    expect(screen.getByText('第二组问题')).toBeTruthy();
    expect(bridge.answer).toHaveBeenCalledTimes(1);
  });

  it('bridge 没有 askUser 能力时不渲染也不报错', () => {
    (window as { electron?: unknown }).electron = {};
    render(<AskUserModalHost />);
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

describe('AskUserModalHost 分步菜单（一次一题）', () => {
  it('第一题选择后进入第二题，文本输入后一次性提交全部答案', () => {
    const bridge = installBridge();
    render(<AskUserModalHost />);
    act(() => bridge.emit(REQUEST_TWO));

    expect(screen.getByText(/问题 1 \/ 2/)).toBeTruthy();
    expect(screen.getByText(/目标环境？/)).toBeTruthy();

    fireEvent.click(screen.getByRole('radio', { name: /staging/ }));

    // 不提交，而是进入同一请求的下一题
    expect(bridge.answer).not.toHaveBeenCalled();
    expect(screen.getByText(/问题 2 \/ 2/)).toBeTruthy();
    expect(screen.getByText('备注')).toBeTruthy();

    const input = screen.getByPlaceholderText('输入你的回答...');
    fireEvent.change(input, { target: { value: '周三窗口' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(bridge.answer).toHaveBeenCalledWith({
      requestId: 'req-two',
      answers: { env: 'staging', note: '周三窗口' },
    });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('「其他 / 自定义」输入直接作为该题答案，不会生成新的问题', () => {
    const bridge = installBridge();
    render(<AskUserModalHost />);
    act(() => bridge.emit(REQUEST));

    fireEvent.click(screen.getByRole('radio', { name: /其他/ }));
    const input = screen.getByPlaceholderText('输入你的回答...');
    fireEvent.change(input, { target: { value: '蓝绿发布' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(bridge.answer).toHaveBeenCalledWith({
      requestId: 'req-1',
      answers: { env: '蓝绿发布' },
    });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('↑↓ 在菜单项间移动焦点，Enter 选中并提交', () => {
    const bridge = installBridge();
    render(<AskUserModalHost />);
    act(() => bridge.emit(REQUEST));

    const staging = screen.getByRole('radio', { name: /staging/ });
    const prod = screen.getByRole('radio', { name: /prod/ });
    const group = screen.getByRole('radiogroup');

    act(() => staging.focus());
    fireEvent.keyDown(group, { key: 'ArrowDown' });
    expect(document.activeElement).toBe(prod);

    fireEvent.keyDown(prod, { key: 'Enter' });
    expect(bridge.answer).toHaveBeenCalledWith({
      requestId: 'req-1',
      answers: { env: 'prod' },
    });
  });
});
