/**
 * `<QuizCard />`
 *
 * Renders a multi-question quiz: choice / fill / judge / short_answer types.
 * The card manages an internal draft, calls `onSubmit(answers)` once, and
 * switches to read-only mode if `isComplete` is true (or the backend re-emits
 * with `payload.submittedAnswers` filled in).
 *
 * Variants:
 *  - `choiceVariant`: 'pills' (default; chip-style buttons) | 'radio' (native
 *    radio/checkbox inputs in a vertical list).
 *  - `questionVariant`: 'flat' (default) | 'card' (each question gets its own
 *    bordered inner card).
 *
 * Slots:
 *  - `judgeLabels`: tuple for "true"/"false" buttons. Default `['对', '错']`.
 *  - `submitHint`, `submittedMessage`, `submittingLabel`: localisable
 *    status strings.
 *  - `renderQuestionMeta(question)`: append metadata under the stem (e.g.
 *    difficulty + points badges).
 */
import * as React from 'react';
import type { QuizPayload } from '@steerable/agent-protocol';
import { ClipboardIcon, LoaderIcon, CheckIcon } from './icons.js';
import { asReactNode, type RenderSlotResult } from './types.js';

type AnswerValue = string | string[];
type Question = QuizPayload['questions'][number];

export type QuizChoiceVariant = 'pills' | 'radio';
export type QuizQuestionVariant = 'flat' | 'card';

export interface QuizCardProps {
  payload: QuizPayload;
  isComplete?: boolean;
  onSubmit?: (answers: Record<string, AnswerValue>) => void;
  className?: string;
  choiceVariant?: QuizChoiceVariant;
  questionVariant?: QuizQuestionVariant;
  judgeLabels?: [string, string];
  submitHint?: React.ReactNode;
  submittedMessage?: React.ReactNode;
  submittingLabel?: React.ReactNode;
  renderQuestionMeta?: (question: Question) => RenderSlotResult;
}

function emptyAnswer(q: Question): AnswerValue {
  if (q.type === 'choice' && q.allowMultiple) return [];
  return '';
}

export const QuizCard: React.FC<QuizCardProps> = ({
  payload,
  isComplete,
  onSubmit,
  className,
  choiceVariant = 'pills',
  questionVariant = 'flat',
  judgeLabels = ['对', '错'],
  submitHint,
  submittedMessage,
  submittingLabel = '提交中...',
  renderQuestionMeta,
}) => {
  const isReadOnly = Boolean(
    isComplete ||
      (payload.submittedAnswers && Object.keys(payload.submittedAnswers).length > 0),
  );
  const initial = React.useMemo(() => {
    const seed: Record<string, AnswerValue> = {};
    for (const q of payload.questions ?? []) {
      seed[q.id] = (payload.submittedAnswers?.[q.id] ?? emptyAnswer(q)) as AnswerValue;
    }
    return seed;
  }, [payload.questions, payload.submittedAnswers]);
  const [draft, setDraft] = React.useState(initial);
  const [submitting, setSubmitting] = React.useState(false);

  React.useEffect(() => {
    setDraft(initial);
  }, [initial]);

  const setSingle = (qId: string, value: string) => setDraft((p) => ({ ...p, [qId]: value }));
  const toggleMulti = (qId: string, opt: string) =>
    setDraft((p) => {
      const cur = (p[qId] ?? []) as string[];
      return { ...p, [qId]: cur.includes(opt) ? cur.filter((v) => v !== opt) : [...cur, opt] };
    });

  const handleSubmit = async () => {
    if (!onSubmit) return;
    setSubmitting(true);
    try {
      onSubmit(draft);
    } finally {
      setSubmitting(false);
    }
  };

  const renderChoice = (q: Question, value: AnswerValue) => {
    const options = q.options ?? [];
    if (choiceVariant === 'radio') {
      return (
        <div className="space-y-1.5">
          {options.map((opt) => {
            const checked = Array.isArray(value) ? value.includes(opt) : value === opt;
            return (
              <label key={opt} className="flex cursor-pointer items-start gap-2 text-sm">
                <input
                  type={q.allowMultiple ? 'checkbox' : 'radio'}
                  name={q.id}
                  checked={checked}
                  disabled={isReadOnly}
                  className="mt-[3px] h-4 w-4 shrink-0"
                  onChange={() => (q.allowMultiple ? toggleMulti(q.id, opt) : setSingle(q.id, opt))}
                />
                <span className="min-w-0 break-words leading-relaxed">{opt}</span>
              </label>
            );
          })}
        </div>
      );
    }
    return (
      <div className="flex flex-wrap gap-2">
        {options.map((opt) => {
          const selected = q.allowMultiple
            ? (Array.isArray(value) ? value : []).includes(opt)
            : value === opt;
          return (
            <button
              key={opt}
              type="button"
              disabled={isReadOnly}
              onClick={() => (q.allowMultiple ? toggleMulti(q.id, opt) : setSingle(q.id, opt))}
              className={[
                'rounded-full border px-3 py-1 text-xs transition-colors duration-150',
                selected
                  ? 'border-[var(--agent-primary,#18181b)] bg-[var(--agent-primary,#18181b)] text-[var(--agent-primary-foreground,#fff)]'
                  : 'border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#fff)] hover:bg-[var(--agent-accent,#f3f4f6)]',
                isReadOnly ? 'cursor-default opacity-80' : '',
              ].filter(Boolean).join(' ')}
            >
              {opt}
            </button>
          );
        })}
      </div>
    );
  };

  const renderJudge = (q: Question, value: AnswerValue) => {
    if (choiceVariant === 'radio') {
      return (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
          {judgeLabels.map((opt) => (
            <label key={opt} className="flex cursor-pointer items-center gap-2">
              <input
                type="radio"
                name={q.id}
                checked={value === opt}
                disabled={isReadOnly}
                onChange={() => setSingle(q.id, opt)}
                className="h-4 w-4 shrink-0"
              />
              <span>{opt}</span>
            </label>
          ))}
        </div>
      );
    }
    return (
      <div className="flex gap-2">
        {judgeLabels.map((opt) => (
          <button
            key={opt}
            type="button"
            disabled={isReadOnly}
            onClick={() => setSingle(q.id, opt)}
            className={[
              'rounded-full border px-3 py-1 text-xs transition-colors duration-150',
              value === opt
                ? 'border-[var(--agent-primary,#18181b)] bg-[var(--agent-primary,#18181b)] text-[var(--agent-primary-foreground,#fff)]'
                : 'border-[var(--agent-border,#e5e7eb)] hover:bg-[var(--agent-accent,#f3f4f6)]',
            ].filter(Boolean).join(' ')}
          >
            {opt}
          </button>
        ))}
      </div>
    );
  };

  const renderTextarea = (q: Question, value: AnswerValue) => (
    <textarea
      rows={q.type === 'short_answer' ? 4 : 2}
      value={typeof value === 'string' ? value : ''}
      disabled={isReadOnly}
      placeholder={q.placeholder ?? '请输入你的答案'}
      onChange={(e) => setSingle(q.id, e.target.value)}
      className="w-full rounded-md border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#fff)] px-3 py-2 text-sm text-[var(--agent-foreground,#111827)] outline-none transition-all duration-200 focus:border-[var(--agent-primary,#18181b)]"
    />
  );

  const defaultMeta = (q: Question) => {
    if (!q.difficulty && !q.points) return null;
    return (
      <>
        {q.difficulty ? <span>难度 {q.difficulty}</span> : null}
        {q.points ? <span>{q.points} 分</span> : null}
      </>
    );
  };

  return (
    <div
      className={[
        'steerable-quiz overflow-hidden rounded-lg border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-card-bg,#fff)] text-sm text-[var(--agent-foreground,#111827)]',
        className,
      ].filter(Boolean).join(' ')}
    >
      <div className="space-y-3 p-3">
        <div className="flex items-center gap-2">
          <ClipboardIcon size={14} className="shrink-0 text-blue-500" />
          <span className="break-words font-medium">{payload.title}</span>
          {isReadOnly && <CheckIcon size={12} className="text-emerald-600" />}
        </div>
        {payload.description && (
          <p className="break-words text-xs leading-relaxed text-[var(--agent-muted-foreground,#6b7280)]">
            {payload.description}
          </p>
        )}

        <div className={questionVariant === 'card' ? 'space-y-3' : 'space-y-4'}>
          {(payload.questions ?? []).map((q, idx) => {
            const value = draft[q.id] ?? emptyAnswer(q);
            const metaNode = renderQuestionMeta
              ? asReactNode(renderQuestionMeta(q))
              : defaultMeta(q);
            const inner = (
              <>
                <div className="mb-2">
                  <p className="break-words text-sm font-medium leading-relaxed">
                    {idx + 1}. {q.stem}
                  </p>
                  {metaNode && (
                    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-[var(--agent-muted-foreground,#6b7280)]">
                      {metaNode}
                    </div>
                  )}
                </div>
                {q.type === 'choice' && q.options ? renderChoice(q, value) : null}
                {q.type === 'judge' ? renderJudge(q, value) : null}
                {q.type === 'fill' || q.type === 'short_answer' ? renderTextarea(q, value) : null}
              </>
            );
            return questionVariant === 'card' ? (
              <div
                key={q.id}
                className="rounded-lg border border-[var(--agent-border,#e5e7eb)] bg-[var(--agent-background,#fff)] p-3"
              >
                {inner}
              </div>
            ) : (
              <div key={q.id} className="space-y-1.5">
                {inner}
              </div>
            );
          })}
        </div>

        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 pt-1">
          {isReadOnly ? (
            <div className="inline-flex min-w-0 items-center gap-1.5 text-xs text-green-600 dark:text-green-400">
              <CheckIcon size={12} />
              <span className="truncate">{submittedMessage ?? '已提交'}</span>
            </div>
          ) : (
            submitHint && (
              <span className="min-w-0 flex-1 break-words text-xs text-[var(--agent-muted-foreground,#6b7280)]">
                {submitHint}
              </span>
            )
          )}

          {!isReadOnly && onSubmit && (
            <button
              type="button"
              onClick={handleSubmit}
              disabled={submitting || isReadOnly}
              className="ml-auto inline-flex h-[28px] shrink-0 items-center gap-1 rounded-full bg-[var(--agent-primary,#18181b)] px-3 text-xs font-medium text-[var(--agent-primary-foreground,#fff)] transition-all duration-200 hover:opacity-90 disabled:opacity-50"
            >
              {submitting ? (
                <>
                  <LoaderIcon size={10} className="animate-spin" />
                  {submittingLabel}
                </>
              ) : (
                payload.submitActionLabel || '提交'
              )}
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

export default QuizCard;
