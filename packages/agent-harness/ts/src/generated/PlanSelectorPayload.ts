/**
 * Payload of a plan-selector card. The agent has drafted multiple candidate plans and is asking the user to pick one. Each plan carries effort / risk metrics and a pros/cons list. After selection the backend re-emits with selectedPlan filled in.
 */
export interface PlanSelectorPayload {
  /**
   * Short prose summarising how the candidate plans differ.
   */
  comparison: string;
  /**
   * Id of the user-chosen plan once decided.
   */
  selectedPlan?: string | null;
  goalAttribution: {
    type: "existing" | "new";
    existingGoalId?: string | null;
    existingGoalTitle?: string | null;
    newGoalTitle?: string | null;
    [k: string]: any;
  };
  plans: {
    id: string;
    name: string;
    summary: string;
    approach: string;
    bestFor: string;
    metrics: {
      duration: string;
      effortLevel: "low" | "medium" | "high";
      riskLevel: "low" | "medium" | "high";
      [k: string]: any;
    };
    pros: string[];
    cons: string[];
    [k: string]: any;
  }[];
  [k: string]: any;
}
