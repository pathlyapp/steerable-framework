/**
 * Payload of an ask-user-questions card. The agent has paused mid-run and needs structured input from the user before continuing. Each question is one of: option buttons (select), free-form text, or password. After the user answers, the backend re-emits the same card with answers filled in to switch the UI to read-only.
 */
export interface AskUserQuestionsPayload {
  intro: string;
  outro?: string | null;
  answers?: {
    [k: string]: string | string[];
  } | null;
  questions: {
    id: string;
    text: string;
    type?: "select" | "text" | "password";
    options?: string[];
    placeholder?: string | null;
    multiSelect?: boolean;
    [k: string]: any;
  }[];
  [k: string]: any;
}
