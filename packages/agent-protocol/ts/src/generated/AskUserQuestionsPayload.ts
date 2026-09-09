/**
 * Payload of an ask-user-questions card. The agent has paused mid-run and needs structured input from the user before continuing. Each question is one of: option buttons (select), free-form text, or password. After the user answers, the backend re-emits the same card with answers filled in to switch the UI to read-only.
 */
export interface AskUserQuestionsPayload {
  intro: string;
  outro?: string | null;
  answers?: {
    [k: string]: string | string[];
  } | null;
  /**
   * @minItems 1
   * @maxItems 4
   */
  questions:
    | [
        {
          id: string;
          text: string;
          /**
           * Short chip label shown above the question (Claude Code AskUserQuestion parity). Optional; hosts derive one from `text` when absent.
           */
          header?: string;
          type?: "select" | "text" | "password";
          /**
           * Choices for a select question (2-4, CC parity). The host auto-appends an 'Other' free-text escape; the model does not list it.
           *
           * @minItems 2
           * @maxItems 4
           */
          options?: [string, string] | [string, string, string] | [string, string, string, string];
          placeholder?: string | null;
          /**
           * Whether the user may pick several options. Required (CC parity): the model commits to single vs multi explicitly.
           */
          multiSelect: boolean;
          [k: string]: any;
        }
      ]
    | [
        {
          id: string;
          text: string;
          /**
           * Short chip label shown above the question (Claude Code AskUserQuestion parity). Optional; hosts derive one from `text` when absent.
           */
          header?: string;
          type?: "select" | "text" | "password";
          /**
           * Choices for a select question (2-4, CC parity). The host auto-appends an 'Other' free-text escape; the model does not list it.
           *
           * @minItems 2
           * @maxItems 4
           */
          options?: [string, string] | [string, string, string] | [string, string, string, string];
          placeholder?: string | null;
          /**
           * Whether the user may pick several options. Required (CC parity): the model commits to single vs multi explicitly.
           */
          multiSelect: boolean;
          [k: string]: any;
        },
        {
          id: string;
          text: string;
          /**
           * Short chip label shown above the question (Claude Code AskUserQuestion parity). Optional; hosts derive one from `text` when absent.
           */
          header?: string;
          type?: "select" | "text" | "password";
          /**
           * Choices for a select question (2-4, CC parity). The host auto-appends an 'Other' free-text escape; the model does not list it.
           *
           * @minItems 2
           * @maxItems 4
           */
          options?: [string, string] | [string, string, string] | [string, string, string, string];
          placeholder?: string | null;
          /**
           * Whether the user may pick several options. Required (CC parity): the model commits to single vs multi explicitly.
           */
          multiSelect: boolean;
          [k: string]: any;
        }
      ]
    | [
        {
          id: string;
          text: string;
          /**
           * Short chip label shown above the question (Claude Code AskUserQuestion parity). Optional; hosts derive one from `text` when absent.
           */
          header?: string;
          type?: "select" | "text" | "password";
          /**
           * Choices for a select question (2-4, CC parity). The host auto-appends an 'Other' free-text escape; the model does not list it.
           *
           * @minItems 2
           * @maxItems 4
           */
          options?: [string, string] | [string, string, string] | [string, string, string, string];
          placeholder?: string | null;
          /**
           * Whether the user may pick several options. Required (CC parity): the model commits to single vs multi explicitly.
           */
          multiSelect: boolean;
          [k: string]: any;
        },
        {
          id: string;
          text: string;
          /**
           * Short chip label shown above the question (Claude Code AskUserQuestion parity). Optional; hosts derive one from `text` when absent.
           */
          header?: string;
          type?: "select" | "text" | "password";
          /**
           * Choices for a select question (2-4, CC parity). The host auto-appends an 'Other' free-text escape; the model does not list it.
           *
           * @minItems 2
           * @maxItems 4
           */
          options?: [string, string] | [string, string, string] | [string, string, string, string];
          placeholder?: string | null;
          /**
           * Whether the user may pick several options. Required (CC parity): the model commits to single vs multi explicitly.
           */
          multiSelect: boolean;
          [k: string]: any;
        },
        {
          id: string;
          text: string;
          /**
           * Short chip label shown above the question (Claude Code AskUserQuestion parity). Optional; hosts derive one from `text` when absent.
           */
          header?: string;
          type?: "select" | "text" | "password";
          /**
           * Choices for a select question (2-4, CC parity). The host auto-appends an 'Other' free-text escape; the model does not list it.
           *
           * @minItems 2
           * @maxItems 4
           */
          options?: [string, string] | [string, string, string] | [string, string, string, string];
          placeholder?: string | null;
          /**
           * Whether the user may pick several options. Required (CC parity): the model commits to single vs multi explicitly.
           */
          multiSelect: boolean;
          [k: string]: any;
        }
      ]
    | [
        {
          id: string;
          text: string;
          /**
           * Short chip label shown above the question (Claude Code AskUserQuestion parity). Optional; hosts derive one from `text` when absent.
           */
          header?: string;
          type?: "select" | "text" | "password";
          /**
           * Choices for a select question (2-4, CC parity). The host auto-appends an 'Other' free-text escape; the model does not list it.
           *
           * @minItems 2
           * @maxItems 4
           */
          options?: [string, string] | [string, string, string] | [string, string, string, string];
          placeholder?: string | null;
          /**
           * Whether the user may pick several options. Required (CC parity): the model commits to single vs multi explicitly.
           */
          multiSelect: boolean;
          [k: string]: any;
        },
        {
          id: string;
          text: string;
          /**
           * Short chip label shown above the question (Claude Code AskUserQuestion parity). Optional; hosts derive one from `text` when absent.
           */
          header?: string;
          type?: "select" | "text" | "password";
          /**
           * Choices for a select question (2-4, CC parity). The host auto-appends an 'Other' free-text escape; the model does not list it.
           *
           * @minItems 2
           * @maxItems 4
           */
          options?: [string, string] | [string, string, string] | [string, string, string, string];
          placeholder?: string | null;
          /**
           * Whether the user may pick several options. Required (CC parity): the model commits to single vs multi explicitly.
           */
          multiSelect: boolean;
          [k: string]: any;
        },
        {
          id: string;
          text: string;
          /**
           * Short chip label shown above the question (Claude Code AskUserQuestion parity). Optional; hosts derive one from `text` when absent.
           */
          header?: string;
          type?: "select" | "text" | "password";
          /**
           * Choices for a select question (2-4, CC parity). The host auto-appends an 'Other' free-text escape; the model does not list it.
           *
           * @minItems 2
           * @maxItems 4
           */
          options?: [string, string] | [string, string, string] | [string, string, string, string];
          placeholder?: string | null;
          /**
           * Whether the user may pick several options. Required (CC parity): the model commits to single vs multi explicitly.
           */
          multiSelect: boolean;
          [k: string]: any;
        },
        {
          id: string;
          text: string;
          /**
           * Short chip label shown above the question (Claude Code AskUserQuestion parity). Optional; hosts derive one from `text` when absent.
           */
          header?: string;
          type?: "select" | "text" | "password";
          /**
           * Choices for a select question (2-4, CC parity). The host auto-appends an 'Other' free-text escape; the model does not list it.
           *
           * @minItems 2
           * @maxItems 4
           */
          options?: [string, string] | [string, string, string] | [string, string, string, string];
          placeholder?: string | null;
          /**
           * Whether the user may pick several options. Required (CC parity): the model commits to single vs multi explicitly.
           */
          multiSelect: boolean;
          [k: string]: any;
        }
      ];
  [k: string]: any;
}
