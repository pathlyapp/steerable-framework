/**
 * `<AskUserQuestionsCard />`
 *
 * Mid-run interrupt: the agent has paused and is asking the user to answer a
 * structured set of questions before continuing. Supports select / text /
 * password question types with optional multi-select and an opt-in "custom
 * text" escape per question.
 *
 * Submission is controlled: the parent passes `onSubmit(answers)` and may
 * re-emit the same card with `payload.answers` filled in to flip the UI into
 * read-only review mode.
 *
 * Features:
 *  - `allowCustomText`: each `select` question gets a "自定义" pill that
 *    flips it into a free-form input (deeppath relies on this for
 *    "the option I want is not listed").
 *  - `onAutoContinue` + `autoContinueLabel`: surfaces an escape button in the
 *    intro row ("交给我决定") that delegates the decision back to the agent.
 *  - `requireAllAnswered`: disables the submit button until every question
 *    has a non-empty answer.
 *  - Localisable strings: `submitLabel`, `multiSelectLabel`, `customLabel`,
 *    `bottomHint`.
 */
import * as React from 'react';
import type { AskUserQuestionsPayload } from '@steerable/agent-protocol';
import {
  MessageQuestionIcon,
  CheckIcon,
  LoaderIcon,
  PencilIcon,
} from './icons.js';

type AnswerValue = string | string[];
type Question = AskUserQuestionsPayload['questions'][number];

export interface AskUserQuestionsCardProps {
  payload: AskUserQuestionsPayload;
  isComplete?: boolean;
  onSubmit?: (answers: Record<string, AnswerValue>) => void;
  onAutoContinue?: () => void;
  className?: string;
  allowCustomText?: boolean;
  requireAllAnswered?: boolean;
  submitLabel?: React.ReactNode;
  multiSelectLabel?: React.ReactNode;
  customLabel?: React.ReactNode;
  autoContinueLabel?: React.ReactNode;
  bottomHint?: React.ReactNode;
}

function emptyAnswerFor(q: Question): AnswerValue {
  if (q.multiSelect) return [];
  return '';
}

function isInputOnly(q: Question): boolean {
  return q.type === 'text' || q.type === 'password';
}

export const AskUserQuestionsCard: React.FC<AskUserQuestionsCardProps> = ({
  payload,
  isComplete,
  onSubmit,
  onAutoContinue,
  className,
  allowCustomText = false,
  requireAllAnswered = false,
  submitLabel = '提交',
  multiSelectLabel = '（可多选）',
  customLabel = '自定义',
  autoContinueLabel = '交给我决定',
  bottomHint,
}) => {
  const isReadOnly = Boolean(isComplete || payload.answers);

  const initial = React.useMemo(() => {
    const seed: Record<string, AnswerValue> = {};
    const custom: Record<string, boolean> = {};
    const text: Record<string, string> = {};
    for (const q of payload.questions ?? []) {
      const fromBackend = payload.answers?.[q.id];
      if (fromBackend === undefined) {
        seed[q.id] = emptyAnswerFor(q);
        continue;
      }
      if (isInputOnly(q)) {
        text[q.id] = typeof fromBackend === 'string' ? fromBackend : '';
        seed[q.id] = fromBackend;
        continue;
      }
      const options = q.options ?? [];
      if (Array.isArray(fromBackend)) {
        const allInOptions = fromBackend.every((a) => options.includes(a));
        if (allInOptions) {
          seed[q.id] = fromBackend;
        } else {
          custom[q.id] = true;
          text[q.id] = fromBackend.join('、');
          seed[q.id] = fromBackend;
        }
      } else {
        if (options.includes(fromBackend)) {
          seed[q.id] = fromBackend;
        } else {
          custom[q.id] = true;
          text[q.id] = fromBackend;
          seed[q.id] = fromBackend;
        }
      }
    }
    return { seed, custom, text };
  }, [payload.questions, payload.answers]);

  const [draft, setDraft] = React.useState<Record<string, AnswerValue>>(initial.seed);
  const [customMode, setCustomMode] = React.useState<Record<string, boolean>>(initial.custom);
  const [customText, setCustomText] = React.useState<Record<string, string>>(initial.text);
  const [submitting, setSubmitting] = React.useState(false);

  React.useEffect(() => {
    setDraft(initial.seed);
    setCustomMode(initial.custom);
    setCustomText(initial.text);
  }, [initial]);

  const toggleMulti = (qId: string, opt: string) =>
    setDraft((p) => {
      const cur = (p[qId] ?? []) as string[];
      return { ...p, [qId]: cur.includes(opt) ? cur.filter((v) => v !== opt) : [...cur, opt] };
    });
  const setSingle = (qId: string, value: string) => setDraft((p) => ({ ...p, [qId]: value }));
  const enterCustom = (qId: string) => {
    setCustomMode((p) => ({ ...p, [qId]: true }));
    setDraft((p) => {
      const next = { ...p };
      delete next[qId];
      return next;
    });
  };
  const cancelCustom = (qId: string) => {
    setCustomMode((p) => ({ ...p, [qId]: false }));
    setCustomText((p) => {
      const next = { ...p };
      delete next[qId];
      return next;
    });
  };

  const answerForQuestion = React.useCallback(
    (q: Question): string => {
      if (isInputOnly(q)) return (customText[q.id] || '').trim();
      if (allowCustomText && customMode[q.id]) return (customText[q.id] || '').trim();
      const val = draft[q.id];
      if (Array.isArray(val)) return val.length > 0 ? val.join('、') : '';
      return (val as string) || '';
    },
    [allowCustomText, customMode, customText, draft],
  );

  const allAnswered = React.useMemo(
    () => (payload.questions ?? []).every((q) => answerForQuestion(q).length > 0),
    [payload.questions, answerForQuestion],
  );

  const handleSubmit = async () => {
    if (!onSubmit) return;
    if (requireAllAnswered && !allAnswered) return;
    setSubmitting(true);
    try {
      const out: Record<string, AnswerValue> = {};
      for (const q of payload.questions ?? []) {
        if (isInputOnly(q) || (allowCustomText && customMode[q.id])) {
          out[q.id] = (customText[q.id] || '').trim();
        } else {
          out[q.id] = draft[q.id] ?? emptyAnswerFor(q);
        }
      }
      onSubmit(out);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className={[
        'steerable-ask-user overflow-hidden rounded-lg border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#fff)] text-sm text-[var(--agent-foreground,#111827)]',
        className,
      ].filter(Boolean).join(' ')}
    >
      <div className="space-y-4 p-3">
        <div className="flex items-start justify-between gap-3">
          <div className="flex flex-1 items-start gap-2">
            {!payload.intro && (
              <MessageQuestionIcon size={14} className="mt-0.5 text-[var(--agent-muted-foreground,#6b7280)]" />
            )}
            {payload.intro ? (
              <p className="flex-1 text-sm">{payload.intro}</p>
            ) : (
              <span className="font-medium">需要你的输入</span>
            )}
          </div>
          {!isReadOnly && onAutoContinue && (
            <button
              type="button"
              onClick={onAutoContinue}
              className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#fff)] px-3 py-1 text-xs font-medium text-[var(--agent-foreground,#111827)] transition-all duration-200 hover:bg-[var(--agent-muted,#f3f4f6)]"
            >
              <CheckIcon size={12} />
              {autoContinueLabel}
            </button>
          )}
        </div>

        <ol className="space-y-3">
          {(payload.questions ?? []).map((q, idx) => {
            const value = draft[q.id];
            const inputOnly = isInputOnly(q);
            const isCustom = allowCustomText && customMode[q.id];

            return (
              <li key={q.id} className="space-y-2">
                <p className="text-sm font-medium">
                  {idx + 1}. {q.text}
                  {!inputOnly && q.multiSelect && (
                    <span className="ml-1.5 text-xs font-normal text-[var(--agent-muted-foreground,#6b7280)]">
                      {multiSelectLabel}
                    </span>
                  )}
                </p>

                {inputOnly && (
                  <input
                    type={q.type === 'password' ? 'password' : 'text'}
                    disabled={isReadOnly}
                    placeholder={q.placeholder || (q.type === 'password' ? '输入密码' : '输入你的回答...')}
                    value={customText[q.id] || ''}
                    onChange={(e) => setCustomText((p) => ({ ...p, [q.id]: e.target.value }))}
                    className="w-full rounded-lg border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-background,#fff)] px-3 py-1.5 text-xs text-[var(--agent-foreground,#111827)] outline-none placeholder:text-[var(--agent-muted-foreground,#6b7280)] focus:border-[var(--agent-primary,#18181b)] focus:ring-1 focus:ring-[var(--agent-primary,#18181b)] disabled:cursor-not-allowed disabled:opacity-60"
                  />
                )}

                {!inputOnly && (
                  <>
                    <div className="flex flex-wrap gap-2">
                      {(q.options ?? []).map((opt) => {
                        const selected =
                          !isCustom &&
                          (q.multiSelect
                            ? (Array.isArray(value) ? value : []).includes(opt)
                            : value === opt);
                        return (
                          <button
                            key={opt}
                            type="button"
                            disabled={isReadOnly}
                            onClick={() =>
                              q.multiSelect ? toggleMulti(q.id, opt) : setSingle(q.id, opt)
                            }
                            className={[
                              'rounded-full border px-3 py-1.5 text-xs font-medium transition-all duration-200',
                              selected
                                ? 'border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-muted,#f3f4f6)] text-[var(--agent-foreground,#111827)] ring-1 ring-[var(--agent-border,#e5e7eb)]'
                                : 'border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#fff)] text-[var(--agent-muted-foreground,#6b7280)] hover:bg-[var(--agent-muted,#f3f4f6)] hover:text-[var(--agent-foreground,#111827)]',
                              isReadOnly ? 'cursor-not-allowed opacity-60' : 'cursor-pointer',
                            ].join(' ')}
                          >
                            {q.multiSelect && selected && (
                              <CheckIcon size={10} className="-ml-0.5 mr-1 inline" />
                            )}
                            {opt}
                          </button>
                        );
                      })}

                      {allowCustomText && !isCustom && (
                        <button
                          type="button"
                          disabled={isReadOnly}
                          onClick={() => enterCustom(q.id)}
                          className={[
                            'rounded-full border border-dashed border-[var(--agent-border,#e5e7eb)] px-3 py-1.5 text-xs font-medium text-[var(--agent-muted-foreground,#6b7280)] transition-all duration-200 hover:bg-[var(--agent-muted,#f3f4f6)] hover:text-[var(--agent-foreground,#111827)]',
                            isReadOnly ? 'cursor-not-allowed opacity-60' : 'cursor-pointer',
                          ].join(' ')}
                        >
                          <span className="inline-flex items-center gap-1">
                            <PencilIcon size={12} />
                            {customLabel}
                          </span>
                        </button>
                      )}
                    </div>

                    {isCustom && (
                      <div className="flex items-center gap-2">
                        <input
                          type="text"
                          disabled={isReadOnly}
                          placeholder="输入你的回答..."
                          value={customText[q.id] || ''}
                          onChange={(e) =>
                            setCustomText((p) => ({ ...p, [q.id]: e.target.value }))
                          }
                          className="flex-1 rounded-lg border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-background,#fff)] px-3 py-1.5 text-xs text-[var(--agent-foreground,#111827)] outline-none placeholder:text-[var(--agent-muted-foreground,#6b7280)] focus:border-[var(--agent-primary,#18181b)] focus:ring-1 focus:ring-[var(--agent-primary,#18181b)] disabled:cursor-not-allowed disabled:opacity-60"
                        />
                        <button
                          type="button"
                          disabled={isReadOnly}
                          onClick={() => cancelCustom(q.id)}
                          className="text-xs text-[var(--agent-muted-foreground,#6b7280)] transition-colors duration-200 hover:text-[var(--agent-foreground,#111827)]"
                        >
                          取消
                        </button>
                      </div>
                    )}
                  </>
                )}
              </li>
            );
          })}
        </ol>

        {payload.outro && (
          <p className="text-xs text-[var(--agent-muted-foreground,#6b7280)] whitespace-pre-line">
            {payload.outro}
          </p>
        )}

        {!isReadOnly && onSubmit && (
          <div className="flex items-center justify-between pt-1">
            <span className="text-xs text-[var(--agent-muted-foreground,#6b7280)]">
              {bottomHint}
            </span>
            <button
              type="button"
              onClick={handleSubmit}
              disabled={submitting || (requireAllAnswered && !allAnswered)}
              className={[
                'inline-flex items-center gap-1.5 rounded-full px-4 py-1.5 text-xs font-medium transition-all duration-200',
                submitting || (requireAllAnswered && !allAnswered)
                  ? 'cursor-not-allowed bg-[var(--agent-muted,#f3f4f6)] text-[var(--agent-muted-foreground,#6b7280)]'
                  : 'bg-[var(--agent-primary,#18181b)] text-[var(--agent-primary-foreground,#fff)] shadow-sm hover:opacity-90',
              ].join(' ')}
            >
              {submitting ? <LoaderIcon size={10} className="animate-spin" /> : <CheckIcon size={12} />}
              {submitLabel}
            </button>
          </div>
        )}
      </div>
    </div>
  );
};

export default AskUserQuestionsCard;
