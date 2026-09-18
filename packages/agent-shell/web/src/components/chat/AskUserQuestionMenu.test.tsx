/**
 * AskUserQuestionMenu 分步提问卡片契约：
 *  - 一次一题 + 进度显示；单选点击即提交并推进，多选勾选后按按钮提交；
 *  - 「其他 / 自定义」把用户输入直接作为该题答案（不创建新问题）；
 *  - 提交映射保持 { [questionId]: string | string[] } 契约；
 *  - 上一题回退保留已答；空问题列表直接提交空映射；
 *  - text/password 题型的输入与 Enter 提交。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AskUserQuestionMenu } from './AskUserQuestionMenu';

afterEach(cleanup);

const TWO_SELECTS = [
  {
    id: 'q1',
    text: '选一个问题域',
    options: [{ label: '甲方案' }, { label: '乙方案', description: '带说明' }],
  },
  {
    id: 'q2',
    text: '选一个范围',
    options: ['仅本地', '全部'],
  },
];

describe('AskUserQuestionMenu · 分步与提交契约', () => {
  it('空问题列表：不渲染卡片，直接提交空映射', () => {
    const onSubmit = vi.fn();
    const { container } = render(<AskUserQuestionMenu intro="" questions={[]} onSubmit={onSubmit} />);
    expect(container.firstChild).toBeNull();
    expect(onSubmit).toHaveBeenCalledWith({});
  });

  it('单选：点击即提交并推进到下一题，进度从 1/2 变 2/2', () => {
    const onSubmit = vi.fn();
    render(<AskUserQuestionMenu intro="需要你的输入" questions={TWO_SELECTS} onSubmit={onSubmit} />);

    expect(screen.getByText('问题 1 / 2')).toBeTruthy();
    expect(screen.getByText('选一个问题域')).toBeTruthy();
    // 上一题在第一题不出现
    expect(screen.queryByText('上一题')).toBeNull();

    fireEvent.click(screen.getByText('甲方案'));
    expect(screen.getByText('问题 2 / 2')).toBeTruthy();
    expect(screen.getByText('选一个范围')).toBeTruthy();
    // 还没到提交全部的时候
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('最后一题提交：answers 映射带齐两题', () => {
    const onSubmit = vi.fn();
    render(<AskUserQuestionMenu intro="" questions={TWO_SELECTS} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText('乙方案'));
    fireEvent.click(screen.getByText('全部'));
    expect(onSubmit).toHaveBeenCalledWith({ q1: '乙方案', q2: '全部' });
  });

  it('上一题回退：已选答案保留（aria-checked），可改选后重新推进', () => {
    const onSubmit = vi.fn();
    render(<AskUserQuestionMenu intro="" questions={TWO_SELECTS} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText('甲方案'));
    fireEvent.click(screen.getByText('上一题'));

    expect(screen.getByText('问题 1 / 2')).toBeTruthy();
    const prev = screen.getByText('甲方案').closest('button');
    expect(prev?.getAttribute('aria-checked')).toBe('true');

    fireEvent.click(screen.getByText('乙方案'));
    fireEvent.click(screen.getByText('仅本地'));
    expect(onSubmit).toHaveBeenCalledWith({ q1: '乙方案', q2: '仅本地' });
  });

  it('多选：勾选切换 aria-checked，空选时按钮禁用，提交数组合并', () => {
    const onSubmit = vi.fn();
    const questions = [
      {
        id: 'q1',
        text: '多选',
        multiSelect: true,
        options: ['红', '绿', '蓝'],
      },
    ];
    render(<AskUserQuestionMenu intro="" questions={questions} onSubmit={onSubmit} />);

    const submit = screen.getByText('提交').closest('button')!;
    expect(submit.disabled).toBe(true);

    const red = screen.getByText('红').closest('button')!;
    fireEvent.click(red);
    expect(red.getAttribute('aria-checked')).toBe('true');
    expect(submit.disabled).toBe(false);

    fireEvent.click(red);
    expect(red.getAttribute('aria-checked')).toBe('false');

    fireEvent.click(screen.getByText('红'));
    fireEvent.click(screen.getByText('蓝'));
    fireEvent.click(submit);
    expect(onSubmit).toHaveBeenCalledWith({ q1: ['红', '蓝'] });
  });

  it('「其他 / 自定义」：输入文本直接作为该题答案', () => {
    const onSubmit = vi.fn();
    render(<AskUserQuestionMenu intro="" questions={[TWO_SELECTS[0]]} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText('其他 / 自定义'));
    const input = screen.getByPlaceholderText('输入你的回答...');
    fireEvent.change(input, { target: { value: '丙方案' } });
    fireEvent.click(screen.getByText('提交'));
    expect(onSubmit).toHaveBeenCalledWith({ q1: '丙方案' });
  });

  it('自定义模式可返回选项列表（答案不丢）', () => {
    const onSubmit = vi.fn();
    render(<AskUserQuestionMenu intro="" questions={[TWO_SELECTS[0]]} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText('其他 / 自定义'));
    fireEvent.click(screen.getByText('← 返回选项'));
    expect(screen.getByText('甲方案')).toBeTruthy();
  });

  it('text 题型：Enter 提交；空输入按钮禁用', () => {
    const onSubmit = vi.fn();
    const questions = [{ id: 'q1', text: '叫什么名字', type: 'text', placeholder: '输入名字' }];
    render(<AskUserQuestionMenu intro="" questions={questions} onSubmit={onSubmit} />);

    const submit = screen.getByText('提交').closest('button')!;
    expect(submit.disabled).toBe(true);

    const input = screen.getByPlaceholderText('输入名字');
    fireEvent.change(input, { target: { value: '  小测  ' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    // 提交前去空白
    expect(onSubmit).toHaveBeenCalledWith({ q1: '小测' });
  });

  it('password 题型：input type=password 且角标为密码输入', () => {
    const questions = [{ id: 'q1', text: '口令', type: 'password' }];
    const { container } = render(
      <AskUserQuestionMenu intro="" questions={questions} onSubmit={vi.fn()} />,
    );
    expect(container.querySelector('input[type="password"]')).not.toBeNull();
    expect(screen.getByText('密码输入')).toBeTruthy();
  });

  it('「交给我决定」按钮透传 onAutoContinue', () => {
    const onAutoContinue = vi.fn();
    render(
      <AskUserQuestionMenu
        intro=""
        questions={[TWO_SELECTS[0]]}
        onSubmit={vi.fn()}
        onAutoContinue={onAutoContinue}
      />,
    );
    fireEvent.click(screen.getByText('交给我决定'));
    expect(onAutoContinue).toHaveBeenCalledOnce();
  });
});

describe('AskUserQuestionMenu · 归一化韧性', () => {
  it('question 字段别名、字符串/对象选项、坏条目过滤', () => {
    const onSubmit = vi.fn();
    const questions = [
      { id: 'bad-no-text' },
      { id: 'q1', question: '用别名的问题', options: ['x'] },
      'not-an-object',
    ];
    render(<AskUserQuestionMenu intro="" questions={questions as never} onSubmit={onSubmit} />);
    expect(screen.getByText('用别名的问题')).toBeTruthy();
    expect(screen.getByText('问题 1 / 1')).toBeTruthy();
  });

  it('有选项的题强制 select 形态（即便声明了 text）', () => {
    const questions = [{ id: 'q1', text: 't', type: 'text', options: ['a'] }];
    render(<AskUserQuestionMenu intro="" questions={questions} onSubmit={vi.fn()} />);
    expect(screen.getByText('菜单选择')).toBeTruthy();
  });

  it('已有答案是选项外的自定义值：进入自定义模式并预填', () => {
    const onSubmit = vi.fn();
    // 第一题先答自定义值，再回退验证 initialDraft 的自定义恢复
    render(<AskUserQuestionMenu intro="" questions={TWO_SELECTS} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByText('其他 / 自定义'));
    fireEvent.change(screen.getByPlaceholderText('输入你的回答...'), { target: { value: '自定义甲' } });
    fireEvent.click(screen.getByText('下一题'));
    fireEvent.click(screen.getByText('上一题'));
    expect(screen.getByDisplayValue('自定义甲')).toBeTruthy();
  });
});
