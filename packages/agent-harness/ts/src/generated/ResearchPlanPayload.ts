/**
 * Payload of a research-plan card. Snapshots a research agent's current sub-question tree and its decision for the next round. The UI shows each sub-question with kind / evidence-strength badges.
 */
export interface ResearchPlanPayload {
  topic: string;
  round: number;
  /**
   * True when the research loop is complete and this snapshot is the last one.
   */
  final: boolean;
  subQuestions: {
    id: string;
    question: string;
    kind: "fact" | "compare" | "conclusion" | "risk";
    status:
      | "pending"
      | "searching"
      | "evidenced_strong"
      | "evidenced_medium"
      | "evidenced_weak"
      | "conflicted"
      | "exhausted";
    evidenceCount: number;
    note?: string | null;
    [k: string]: any;
  }[];
  decision: {
    next: "continue" | "expand" | "converge";
    reason?: string | null;
    [k: string]: any;
  };
  [k: string]: any;
}
