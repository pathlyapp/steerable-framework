/**
 * Payload of a quiz card. Carries one or more questions (choice / fill / judge / short_answer) and a submit button label. After the learner submits, the backend re-emits the same quizId with submittedAnswers filled in to switch the UI to read-only review mode.
 */
export interface QuizPayload {
  /**
   * Stable id, used for resubmit and review continuity.
   */
  quizId: string;
  title: string;
  description?: string | null;
  submitActionLabel: string;
  submittedAnswers?: {
    [k: string]: string | string[];
  } | null;
  questions: {
    id: string;
    type: "choice" | "fill" | "judge" | "short_answer";
    stem: string;
    options?: string[] | null;
    allowMultiple?: boolean | null;
    placeholder?: string | null;
    points?: number | null;
    knowledgePointId?: string | null;
    difficulty?: number | null;
    [k: string]: any;
  }[];
  [k: string]: any;
}
