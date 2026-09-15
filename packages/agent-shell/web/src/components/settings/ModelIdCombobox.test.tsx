import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ModelIdCombobox } from './ModelIdCombobox';
import type { ModelPickerRow } from './llm-vendors';

afterEach(cleanup);

const OPTIONS = ['deepseek-chat', 'deepseek-v4-flash', 'deepseek-v4-pro'];

const ROWS: ModelPickerRow[] = [
  {
    id: 'deepseek-v4-pro',
    entry: {
      id: 'deepseek-v4-pro',
      name: 'DeepSeek V4 Pro',
      window: 1_000_000,
      modalities: ['text'],
      reasoningLevels: ['high', 'max'],
      pricing: null,
      joinedFrom: 'deepseek/deepseek-v4-pro',
      capabilities: 'known',
    },
  },
  {
    id: 'deepseek-flash',
    entry: {
      id: 'deepseek-flash',
      name: null,
      window: 131_072,
      modalities: ['text'],
      reasoningLevels: [],
      pricing: null,
      joinedFrom: null,
      capabilities: 'unknown',
    },
  },
];

describe('ModelIdCombobox', () => {
  it('focus 打开贴齐输入框的列表，点击写入模型 id', () => {
    const onChange = vi.fn();
    render(
      <ModelIdCombobox
        value="deepseek-chat"
        options={OPTIONS}
        placeholder="deepseek-chat"
        onChange={onChange}
      />,
    );
    fireEvent.focus(screen.getByTestId('llm-model-input'));
    const list = screen.getByTestId('llm-model-options');
    expect(list.getAttribute('role')).toBe('listbox');
    fireEvent.click(screen.getByRole('option', { name: 'deepseek-v4-flash' }));
    expect(onChange).toHaveBeenCalledWith('deepseek-v4-flash');
    expect(screen.queryByTestId('llm-model-options')).toBeNull();
  });

  it('已选中某项时仍列出全部模型', () => {
    render(
      <ModelIdCombobox value="deepseek-v4-flash" options={OPTIONS} onChange={() => {}} />,
    );
    fireEvent.focus(screen.getByTestId('llm-model-input'));
    expect(screen.getByRole('option', { name: 'deepseek-chat' })).toBeTruthy();
    expect(screen.getByRole('option', { name: 'deepseek-v4-pro' })).toBeTruthy();
  });

  it('手填部分文字时按子串过滤，并允许自定义 id', () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <ModelIdCombobox value="deepseek-chat" options={OPTIONS} onChange={onChange} />,
    );
    fireEvent.change(screen.getByTestId('llm-model-input'), { target: { value: 'flash' } });
    expect(onChange).toHaveBeenCalledWith('flash');
    rerender(<ModelIdCombobox value="flash" options={OPTIONS} onChange={onChange} />);
    expect(screen.getByRole('option', { name: 'deepseek-v4-flash' })).toBeTruthy();
    expect(screen.queryByRole('option', { name: 'deepseek-chat' })).toBeNull();
  });

  it('shows verified capability chips and keeps option names as the model id', () => {
    render(<ModelIdCombobox value="deepseek-v4-pro" options={ROWS} onChange={() => {}} />);
    fireEvent.focus(screen.getByTestId('llm-model-input'));
    expect(screen.getByRole('option', { name: 'deepseek-v4-pro' })).toBeTruthy();
    expect(screen.getByText('思考')).toBeTruthy();
    expect(screen.getByText('1M')).toBeTruthy();
    expect(screen.getByText('未识别')).toBeTruthy();
    expect(screen.queryByText('思考（high / max）')).toBeNull();
  });
});
