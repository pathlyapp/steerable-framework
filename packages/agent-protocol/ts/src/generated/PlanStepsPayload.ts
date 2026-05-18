/**
 * Payload of a plan-steps card -- a compact horizontal checklist of skill names the agent intends to execute. Rendered as 'step1 / step2 / step3 ...' inline.
 */
export interface PlanStepsPayload {
  steps: string[];
  [k: string]: any;
}
