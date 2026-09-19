import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { LuCheck, LuChevronLeft, LuPencil, LuWand } from 'react-icons/lu';

/**
 * AskUserQuestionMenu — W8 结构化提问的分步菜单卡片。
 *
 * 替代 `@steerable/agent-ui` 的 `AskUserQuestionsCard`：
 *  - 一次只展示一个问题，顶部显示进度（问题 1/3）；
 *  - select 题渲染为纵向菜单，模型给出的 options 是菜单项；
 *  - 菜单底部追加「其他 / 自定义」：选中后展开输入框，用户输入直接作为
 *    该题答案（不会用补充信息再创建新问题）；
 *  - 支持 ↑↓ 移动焦点，Enter 选中/切换；单选 Enter/点击 即提交并进入
 *    下一题（或提交全部），多选勾选后用「下一题 / 提交」按钮；
 *  - 提交时仍返回 `{ [questionId]: string | string[] }`，与主进程/
 *    sidecar 的 answers 映射契约保持一致。
 */

type AnswerValue = string | string[];
type RawQuestion = Record<string, unknown>;

interface AskOption {
  label: string;
  description?: string;
}

interface AskQuestion {
  id: string;
  text: string;
  type: 'select' | 'text' | 'password';
  options: AskOption[];
  placeholder?: string;
  multiSelect: boolean;
  detail?: string;
  header?: string;
}

function normalizeOption(raw: unknown): AskOption | null {
  if (typeof raw === 'string') return { label: raw };
  if (raw && typeof raw === 'object') {
    const obj = raw as Record<string, unknown>;
    if (typeof obj.label === 'string') {
      return {
        label: obj.label,
        description: typeof obj.description === 'string' ? obj.description : undefined,
      };
    }
  }
  return null;
}

function normalizeQuestion(raw: RawQuestion): AskQuestion | null {
  if (typeof raw.id !== 'string') return null;
  const text =
    typeof raw.text === 'string'
      ? raw.text
      : typeof raw.question === 'string'
        ? raw.question
        : '';
  if (!text) return null;

  const rawType = raw.type;
  const inputType: AskQuestion['type'] =
    rawType === 'text' || rawType === 'password' ? rawType : 'select';

  const options = Array.isArray(raw.options)
    ? (raw.options.map(normalizeOption).filter(Boolean) as AskOption[])
    : [];

  return {
    id: raw.id,
    text,
    type: options.length > 0 ? 'select' : inputType === 'select' ? 'text' : inputType,
    options,
    placeholder: typeof raw.placeholder === 'string' ? raw.placeholder : undefined,
    multiSelect: raw.multiSelect === true,
    detail: typeof raw.detail === 'string' ? raw.detail : undefined,
    header: typeof raw.header === 'string' ? raw.header : undefined,
  };
}

function normalizeQuestions(raw: Array<Record<string, unknown>>): AskQuestion[] {
  return raw.map(normalizeQuestion).filter(Boolean) as AskQuestion[];
}

function initialDraft(question: AskQuestion, answer: AnswerValue | undefined): {
  selected: string[];
  customMode: boolean;
  customText: string;
} {
  const optionLabels = question.options.map((o) => o.label);
  if (Array.isArray(answer)) {
    const selected = answer.filter((v) => optionLabels.includes(v));
    const customBits = answer.filter((v) => !optionLabels.includes(v));
    return {
      selected,
      customMode: customBits.length > 0,
      customText: customBits.join('、'),
    };
  }
  if (typeof answer === 'string' && answer.length > 0) {
    if (optionLabels.includes(answer)) {
      return { selected: [answer], customMode: false, customText: '' };
    }
    return { selected: [], customMode: true, customText: answer };
  }
  return { selected: [], customMode: false, customText: '' };
}

function Footer({
  canGoBack,
  onBack,
  onAutoContinue,
  hint,
  children,
}: {
  canGoBack: boolean;
  onBack?: () => void;
  onAutoContinue?: () => void;
  hint?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="mt-3 flex items-center justify-between gap-2 border-t border-agent-border px-3 py-2">
      <div className="flex min-w-0 items-center gap-2">
        {canGoBack && onBack && (
          <button
            type="button"
            onClick={onBack}
            className="inline-flex items-center gap-1 rounded-agent-md border border-agent-border px-2.5 py-1.5 text-xs font-medium text-agent-foreground transition hover:bg-agent-muted"
          >
            <LuChevronLeft size={12} />
            上一题
          </button>
        )}
        {onAutoContinue && (
          <button
            type="button"
            onClick={onAutoContinue}
            className="inline-flex items-center gap-1 rounded-agent-md px-2.5 py-1.5 text-xs font-medium text-agent-muted-foreground transition hover:bg-agent-muted hover:text-agent-foreground"
          >
            <LuWand size={12} />
            交给我决定
          </button>
        )}
      </div>
      <div className="flex min-w-0 items-center gap-3">
        <span className="hidden truncate text-[11px] text-agent-muted-foreground sm:inline">
          {hint}
        </span>
        {children}
      </div>
    </div>
  );
}

function PrimaryButton({
  disabled,
  isLast,
  onClick,
}: {
  disabled?: boolean;
  isLast: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={[
        'inline-flex items-center gap-1.5 rounded-agent-md px-4 py-1.5 text-xs font-medium transition',
        disabled
          ? 'cursor-not-allowed bg-agent-muted text-agent-muted-foreground'
          : 'bg-agent-foreground text-agent-canvas shadow-sm hover:opacity-90',
      ].join(' ')}
    >
      <LuCheck size={12} />
      {isLast ? '提交' : '下一题'}
    </button>
  );
}

function SelectQuestionStep({
  question,
  initialAnswer,
  isLast,
  onCommit,
  onBack,
  onAutoContinue,
}: {
  question: AskQuestion;
  initialAnswer: AnswerValue | undefined;
  isLast: boolean;
  onCommit: (value: AnswerValue) => void;
  onBack?: () => void;
  onAutoContinue?: () => void;
}) {
  const initial = useMemo(
    () => initialDraft(question, initialAnswer),
    [question, initialAnswer],
  );
  const [selected, setSelected] = useState<string[]>(initial.selected);
  const [customMode, setCustomMode] = useState(initial.customMode);
  const [customText, setCustomText] = useState(initial.customText);

  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const customInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (customMode) {
      customInputRef.current?.focus();
    }
  }, [customMode]);

  const commitOption = (label: string) => {
    if (question.multiSelect) {
      setSelected((prev) =>
        prev.includes(label) ? prev.filter((v) => v !== label) : [...prev, label],
      );
      return;
    }
    onCommit(label);
  };

  const commitCustom = () => {
    const text = customText.trim();
    if (!text) return;
    if (question.multiSelect) {
      const merged = [...selected];
      if (!merged.includes(text)) merged.push(text);
      onCommit(merged);
    } else {
      onCommit(text);
    }
  };

  const commitMulti = () => {
    const text = customText.trim();
    const merged = [...selected];
    if (customMode && text && !merged.includes(text)) merged.push(text);
    if (merged.length === 0) return;
    onCommit(merged);
  };

  const focusItem = (index: number) => {
    const item = optionRefs.current[index];
    if (item) {
      item.focus();
      return true;
    }
    return false;
  };

  const handleListKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const count = question.options.length + 1; // options + 「其他 / 自定义」
    const current = optionRefs.current.findIndex((el) => el === document.activeElement);
    const next = e.key === 'ArrowDown' ? (current + 1) % count : (current - 1 + count) % count;
    focusItem(next);
  };

  const renderOption = (opt: AskOption, index: number) => {
    const active = selected.includes(opt.label);
    return (
      <button
        key={opt.label}
        type="button"
        ref={(el) => {
          optionRefs.current[index] = el;
        }}
        onClick={() => commitOption(opt.label)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            commitOption(opt.label);
          }
        }}
        role={question.multiSelect ? 'checkbox' : 'radio'}
        aria-checked={active}
        className={[
          'flex w-full items-center gap-2 rounded-agent-md border px-3 py-2 text-left text-xs transition',
          active
            ? 'border-agent-foreground bg-agent-muted text-agent-foreground'
            : 'border-agent-border bg-agent-canvas text-agent-foreground hover:bg-agent-muted',
        ].join(' ')}
      >
        <span
          className={[
            'flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[10px]',
            active
              ? 'border-agent-foreground bg-agent-foreground text-agent-canvas'
              : 'border-agent-border text-transparent',
          ].join(' ')}
        >
          <LuCheck size={10} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate">{opt.label}</span>
          {opt.description && (
            <span className="mt-0.5 block text-xs text-agent-muted-foreground">
              {opt.description}
            </span>
          )}
        </span>
        {!question.multiSelect && (
          <kbd className="hidden rounded border border-agent-border px-1 text-[10px] text-agent-muted-foreground sm:inline">
            ↵
          </kbd>
        )}
      </button>
    );
  };

  const renderCustomOption = (index: number) => {
    const active = customMode;
    return (
      <button
        key="__custom__"
        type="button"
        ref={(el) => {
          optionRefs.current[index] = el;
        }}
        onClick={() => {
          setCustomMode((prev) => !prev);
          setCustomText('');
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            setCustomMode((prev) => !prev);
            setCustomText('');
          }
        }}
        role={question.multiSelect ? 'checkbox' : 'radio'}
        aria-checked={active}
        className={[
          'flex w-full items-center gap-2 rounded-agent-md border border-dashed px-3 py-2 text-left text-xs transition',
          active
            ? 'border-agent-foreground bg-agent-muted text-agent-foreground'
            : 'border-agent-border bg-agent-canvas text-agent-muted-foreground hover:bg-agent-muted hover:text-agent-foreground',
        ].join(' ')}
      >
        <span
          className={[
            'flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[10px]',
            active
              ? 'border-agent-foreground bg-agent-foreground text-agent-canvas'
              : 'border-agent-border text-transparent',
          ].join(' ')}
        >
          <LuCheck size={10} />
        </span>
        <LuPencil size={12} className="shrink-0" />
        <span className="min-w-0 flex-1">其他 / 自定义</span>
      </button>
    );
  };

  return (
    <div>
      <div className="space-y-2 px-3 py-2">
        {question.header && (
          <p className="text-[11px] font-medium uppercase tracking-wide text-agent-muted-foreground">
            {question.header}
          </p>
        )}
        <p className="text-xs font-medium text-agent-foreground">{question.text}</p>
        {question.detail && (
          <p className="whitespace-pre-line text-xs text-agent-muted-foreground">
            {question.detail}
          </p>
        )}

        {!customMode || question.multiSelect ? (
          <div
            className="space-y-1.5"
            role={question.multiSelect ? 'group' : 'radiogroup'}
            onKeyDown={handleListKeyDown}
          >
            {question.options.map((opt, index) => renderOption(opt, index))}
            {renderCustomOption(question.options.length)}
          </div>
        ) : null}

        {customMode && question.multiSelect && (
          <input
            ref={customInputRef}
            type="text"
            value={customText}
            onChange={(e) => setCustomText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                commitMulti();
              }
            }}
            placeholder={question.placeholder || '输入你的补充...'}
            className="w-full rounded-agent-md border border-agent-border bg-agent-canvas px-3 py-2 text-xs text-agent-foreground outline-none placeholder:text-agent-muted-foreground focus:border-agent-foreground focus:ring-1 focus:ring-agent-foreground"
          />
        )}

        {customMode && !question.multiSelect && (
          <div className="space-y-2 rounded-agent-md border border-agent-border bg-agent-muted/40 p-3">
            <input
              ref={customInputRef}
              type="text"
              value={customText}
              onChange={(e) => setCustomText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  commitCustom();
                }
              }}
              placeholder={question.placeholder || '输入你的回答...'}
              className="w-full rounded-agent-md border border-agent-border bg-agent-canvas px-3 py-2 text-xs text-agent-foreground outline-none placeholder:text-agent-muted-foreground focus:border-agent-foreground focus:ring-1 focus:ring-agent-foreground"
            />
            <button
              type="button"
              onClick={() => {
                setCustomMode(false);
                setCustomText('');
              }}
              className="text-xs text-agent-muted-foreground transition hover:text-agent-foreground"
            >
              ← 返回选项
            </button>
          </div>
        )}
      </div>

      <Footer
        canGoBack={Boolean(onBack)}
        onBack={onBack}
        onAutoContinue={onAutoContinue}
        hint={question.multiSelect ? '↑↓ 移动，Enter 勾选' : '↑↓ 选择，Enter 确认'}
      >
        {question.multiSelect ? (
          <PrimaryButton
            disabled={selected.length === 0 && !(customMode && customText.trim())}
            isLast={isLast}
            onClick={commitMulti}
          />
        ) : (
          customMode && (
            <PrimaryButton
              disabled={!customText.trim()}
              isLast={isLast}
              onClick={commitCustom}
            />
          )
        )}
      </Footer>
    </div>
  );
}

function TextQuestionStep({
  question,
  initialAnswer,
  isLast,
  onCommit,
  onBack,
  onAutoContinue,
}: {
  question: AskQuestion;
  initialAnswer: AnswerValue | undefined;
  isLast: boolean;
  onCommit: (value: AnswerValue) => void;
  onBack?: () => void;
  onAutoContinue?: () => void;
}) {
  const [value, setValue] = useState(
    typeof initialAnswer === 'string' ? initialAnswer : '',
  );
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const commit = () => {
    const text = value.trim();
    if (!text) return;
    onCommit(text);
  };

  return (
    <div>
      <div className="space-y-2 px-3 py-2">
        {question.header && (
          <p className="text-[11px] font-medium uppercase tracking-wide text-agent-muted-foreground">
            {question.header}
          </p>
        )}
        <p className="text-xs font-medium text-agent-foreground">{question.text}</p>
        {question.detail && (
          <p className="whitespace-pre-line text-xs text-agent-muted-foreground">
            {question.detail}
          </p>
        )}
        <input
          ref={inputRef}
          type={question.type === 'password' ? 'password' : 'text'}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              commit();
            }
          }}
          placeholder={question.placeholder || '输入你的回答...'}
          className="w-full rounded-agent-md border border-agent-border bg-agent-canvas px-3 py-2 text-xs text-agent-foreground outline-none placeholder:text-agent-muted-foreground focus:border-agent-foreground focus:ring-1 focus:ring-agent-foreground"
        />
      </div>

      <Footer
        canGoBack={Boolean(onBack)}
        onBack={onBack}
        onAutoContinue={onAutoContinue}
        hint="Enter 确认"
      >
        <PrimaryButton disabled={!value.trim()} isLast={isLast} onClick={commit} />
      </Footer>
    </div>
  );
}

export interface AskUserQuestionMenuProps {
  intro: string;
  questions: Array<Record<string, unknown>>;
  onSubmit: (answers: Record<string, AnswerValue>) => void;
  onAutoContinue?: () => void;
  bottomHint?: string;
}

export function AskUserQuestionMenu({
  intro,
  questions,
  onSubmit,
  onAutoContinue,
  bottomHint,
}: AskUserQuestionMenuProps) {
  const normalized = useMemo(() => normalizeQuestions(questions), [questions]);
  const [step, setStep] = useState(0);
  const [answers, setAnswers] = useState<Record<string, AnswerValue>>({});

  useEffect(() => {
    if (normalized.length === 0) {
      onSubmit({});
    }
  }, [normalized.length, onSubmit]);

  const current = normalized[Math.min(step, normalized.length - 1)] ?? null;
  const isLast = normalized.length > 0 && step >= normalized.length - 1;

  const commit = useCallback(
    (value: AnswerValue) => {
      if (!current) return;
      const next = { ...answers, [current.id]: value };
      setAnswers(next);
      if (isLast) {
        onSubmit(next);
      } else {
        setStep((s) => s + 1);
      }
    },
    [answers, current, isLast, onSubmit],
  );

  const goBack = useCallback(() => {
    setStep((s) => Math.max(0, s - 1));
  }, []);

  if (!current) return null;

  return (
    <div className="overflow-hidden rounded-agent-lg border border-agent-border bg-agent-canvas text-xs text-agent-foreground shadow-xl">
      <div className="flex items-start justify-between gap-2 border-b border-agent-border px-3 py-2">
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium text-agent-foreground">
            {intro || '需要你的输入'}
          </p>
          <p className="mt-0.5 text-[11px] text-agent-muted-foreground">
            问题 {step + 1} / {normalized.length}
            {bottomHint ? ` · ${bottomHint}` : ''}
          </p>
        </div>
        <span className="rounded-full border border-agent-border px-2 py-0.5 text-[10px] font-medium text-agent-muted-foreground">
          {current.type === 'password' ? '密码输入' : current.multiSelect ? '可多选' : '菜单选择'}
        </span>
      </div>

      {current.type === 'select' ? (
        <SelectQuestionStep
          key={`${step}-${current.id}`}
          question={current}
          initialAnswer={answers[current.id]}
          isLast={isLast}
          onCommit={commit}
          onBack={step > 0 ? goBack : undefined}
          onAutoContinue={onAutoContinue}
        />
      ) : (
        <TextQuestionStep
          key={`${step}-${current.id}`}
          question={current}
          initialAnswer={answers[current.id]}
          isLast={isLast}
          onCommit={commit}
          onBack={step > 0 ? goBack : undefined}
          onAutoContinue={onAutoContinue}
        />
      )}
    </div>
  );
}

export default AskUserQuestionMenu;
